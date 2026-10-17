"""Shadow-mode sensing driver: cadence, dedupe, fail-open, metrics.

The driver is deliberately boring. It decides *whether* to sense, calls the one
capability it is allowed to name, and records what came back. It has no retry
loop, no queue, no background thread and no consumer: an observation is stored
and nothing reads it.

Fail-open is structural. Every call path returns an *unavailable* snapshot on
error, so no exception can propagate into a task. Sensing adds latency to a turn
and never a failure.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .bounded_state import (
    SERIALIZER_VERSION,
    SerializedState,
    serialize_state,
)
from .exact_tokens import enforce_budget
from .shadow_observation import (
    DECISION_CAPABILITY,
    QUALIFIED_BACKENDS,
    REQUESTED_SIGNALS,
    Trigger,
    build_snapshot,
    is_material,
    state_fingerprint,
    unavailable,
)

#: Storage is a local JSONL file: append-only, advisory, and trivially
#: inspectable. A storage failure is caught and swallowed — losing an observation
#: must never fail a task.
#:
#: 2026-10-17 adaptation: the historical default pointed into an operator home
#: directory the ``turnstone`` service user cannot write. The default is now a
#: service-writable directory, overridable by env so a test or a different
#: deployment can relocate it without touching production state.
DEFAULT_STORE = pathlib.Path(
    os.environ.get(
        "TURNSTONE_SENSOR_STORE_DIR",
        "/var/lib/turnstone/sensor",
    )
) / "shadow-observations.jsonl"

#: The live decision surface. The historical constant pointed at a local
#: Switchyard dev port; 1.8.7 reaches Switchyard through the registry-resolved
#: base_url of the ``tier1-decision`` row. The constant remains only as the
#: no-registry fallback so the module stays importable and testable in
#: isolation.
SWITCHYARD = "https://switchyard-vm.tailc8ef7b.ts.net"

#: 2026-10-17 adaptation: the historical 20-second synchronous timeout could
#: delay a production task for the full budget on a hung backend. Sensing is
#: advisory; a measured live call completes in well under a second, so the
#: default is now short and env-configurable. A timeout here must never alter
#: the task's outcome, permissions, routing, Output Guard behaviour or retry
#: policy — it only changes how long the sensing attempt is allowed to run
#: before being recorded as an unavailable observation.
REQUEST_TIMEOUT_SECONDS = float(
    os.environ.get("TURNSTONE_SENSOR_TIMEOUT_SECONDS", "2.5")
)

#: How many workstream dedupe states one process retains. Bounded so a long-
#: lived host process cannot grow without limit; eviction is LRU and only
#: costs dedupe precision for long-dormant workstreams, never correctness.
DEDUPE_CAPACITY = int(os.environ.get("TURNSTONE_SENSOR_DEDUPE_CAPACITY", "256"))


@dataclass
class ShadowMetrics:
    """Health counters. Truncation rate is the primary one."""

    observation_count: int = 0
    unavailable_count: int = 0
    truncated_count: int = 0
    fallback_count: int = 0
    deduplicated_count: int = 0
    suppressed_count: int = 0
    total_latency_ms: int = 0
    serialized_chars: list[int] = field(default_factory=list)
    serialized_state_tokens: list[int] = field(default_factory=list)
    dropped_field_count: list[int] = field(default_factory=list)
    backends: Counter = field(default_factory=Counter)

    @property
    def truncation_rate(self) -> float:
        return 0.0 if not self.observation_count else self.truncated_count / self.observation_count

    @property
    def fallback_rate(self) -> float:
        return 0.0 if not self.observation_count else self.fallback_count / self.observation_count

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.observation_count if self.observation_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        def stats(values: list[int]) -> dict[str, float]:
            if not values:
                return {"min": 0, "max": 0, "mean": 0.0}
            return {
                "min": min(values),
                "max": max(values),
                "mean": round(sum(values) / len(values), 2),
            }

        return {
            "observation_count": self.observation_count,
            "unavailable_count": self.unavailable_count,
            "truncated_count": self.truncated_count,
            "truncation_rate": round(self.truncation_rate, 4),
            "fallback_count": self.fallback_count,
            "fallback_rate": round(self.fallback_rate, 4),
            "deduplicated_count": self.deduplicated_count,
            "suppressed_count": self.suppressed_count,
            "mean_latency_ms": round(self.mean_latency_ms, 2),
            "serialized_chars": stats(self.serialized_chars),
            "serialized_state_tokens": stats(self.serialized_state_tokens),
            "dropped_field_count": stats(self.dropped_field_count),
            "backends": dict(self.backends),
        }


class ShadowSensor:
    """Cadence, dedupe, one capability call, and a stored record.

    Dedupe state is PER-WORKSTREAM: one (last admitted fingerprint, initial-
    done flag) pair per workstream id, in a bounded LRU map. The historical
    implementation held a single global pair per process, which made workstream
    A's dedupe state depend on whether workstream B happened to be observed in
    between — an A-B-A sequence was misread as new state in A. Each workstream
    now has its own first-observation and its own last-admitted fingerprint.

    The map is bounded (LRU eviction at DEDUPE_CAPACITY): evicting a dormant
    workstream only costs dedupe precision for that workstream (its next event
    reads as initial again, which is the honest state after a long gap), never
    correctness. This is deliberately NOT a distributed coordination service:
    each process dedupes its own workstreams independently, and cross-process
    duplicate suppression is unnecessary because duplicates are harmless
    advisory rows.
    """

    #: Per-workstream dedupe state. Kept small and copyable for tests.
    _WorkstreamState = tuple[str | None, bool]  # (last_fingerprint, initial_done)

    def __init__(
        self,
        *,
        store: pathlib.Path = DEFAULT_STORE,
        switchyard: str = SWITCHYARD,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        dedupe_capacity: int = DEDUPE_CAPACITY,
        decision_id: str = "",
        base_url: str = "",
    ) -> None:
        self.store = store
        # base_url and decision_id come from the registry-resolved
        # tier1-decision row when a registry is available; the module constants
        # are the no-registry fallback so the sensor stays importable and
        # testable in isolation.
        self.switchyard = base_url or switchyard
        self.timeout = timeout
        self.metrics = ShadowMetrics()
        self._decision_id = decision_id
        self._dedupe: OrderedDict[str, list] = OrderedDict()
        self._dedupe_capacity = max(1, int(dedupe_capacity))
        self._dedupe_lock = threading.Lock()

    # -- cadence ---------------------------------------------------------

    def _dedupe_state(self, workstream: str) -> list:
        """Return the mutable [last_fingerprint, initial_done] for a workstream.

        Must be called under ``_dedupe_lock``. A missing workstream starts
        un-initialised. LRU: the touched entry moves to the end.
        """
        entry = self._dedupe.pop(workstream, None)
        if entry is None:
            if len(self._dedupe) >= self._dedupe_capacity:
                self._dedupe.popitem(last=False)
            entry = [None, False]
        self._dedupe[workstream] = entry
        return entry

    def should_sense(
        self,
        state: Mapping[str, Any],
        *,
        event: str | None,
        force: bool = False,
        workstream: str = "",
    ) -> tuple[bool, str]:
        """Decide whether this state warrants a fresh observation.

        Order matters: an unchanged state is suppressed before anything else,
        because that is the common case and it must not cost a call.

        Dedupe is per workstream: an unchanged state is suppressed only when
        THIS workstream's last admitted fingerprint matches. A different
        workstream's observations between two of this workstream's events leave
        this workstream's dedupe state untouched.

        This method also **commits** the cadence decision. That is deliberate and
        was a real defect when it did not: dedupe state was only advanced by
        :meth:`observe`, which returns early on any transport failure. With the
        backend down, every event therefore read as ``initial_substantive_state``
        forever, dedupe never engaged, and the sensor fired on every single
        material event. Committing here means suppression depends only on the
        caller's own state, never on whether a network call happened to succeed.
        """
        fingerprint = state_fingerprint(state)
        with self._dedupe_lock:
            entry = self._dedupe_state(workstream)
            last_fingerprint, initial_done = entry[0], entry[1]
            if force:
                return True, "forced"
            if not initial_done:
                entry[0] = fingerprint
                entry[1] = True
                return True, Trigger.INITIAL.value
            if not is_material(event or ""):
                return False, f"not_material:{event}"
            if fingerprint == last_fingerprint:
                return False, "unchanged_state"
            # Commit on the admit branch too. Missing this was the second half of
            # the dedupe defect: only the INITIAL branch advanced the fingerprint,
            # so after a successful material observation the state was never
            # recorded and an identical state was admitted again on every
            # subsequent event.
            entry[0] = fingerprint
            return True, Trigger.MATERIAL.value

    # -- observation -----------------------------------------------------

    def observe(
        self,
        state: Mapping[str, Any],
        *,
        event: str | None = None,
        force: bool = False,
        decided: bool = False,
        workstream: str = "",
    ):
        """Take one advisory observation, or record why none was taken.

        Never raises. Every failure becomes an unavailable snapshot so the
        calling task is unaffected.

        ``decided=True`` means the CALLER has already made the cadence/dedupe
        decision for this exact event via :meth:`should_sense` and committed the
        fingerprint. The gate is then skipped, because re-running it would deny
        the very event the caller just admitted.

        That is not a hypothetical. It is the defect the first production canary
        found on 2026-09-30: `SensorHook.observe_event` calls `should_sense()`,
        which COMMITS ``_last_fingerprint``, and then called `observe()`, which
        gated on `should_sense()` again and denied itself `unchanged_state`.
        The hook logged `sensor_call` and the observation store never grew -- a
        sensor that looked alive and recorded nothing. 253 tests passed because
        every one of them drove `observe()` directly and none drove the hook
        with a real `ShadowSensor` behind it.

        The invariant this preserves, in one direction only:

            one material event -> ONE should_sense decision
                              -> one fingerprint commit
                              -> if admitted, ONE observation attempt

        ``observe()`` still gates on its own by default, so a direct caller keeps
        the full dedupe behaviour and the proofs that call it stay honest.
        """
        fingerprint = state_fingerprint(state)
        if decided:
            # The caller owns cadence and already committed the fingerprint.
            # Re-gating here is exactly the double-gate defect.
            allowed, why = True, Trigger.MATERIAL.value
        else:
            allowed, why = self.should_sense(
                state, event=event, force=force, workstream=workstream
            )
        if not allowed:
            if why == "unchanged_state":
                self.metrics.deduplicated_count += 1
            else:
                self.metrics.suppressed_count += 1
            return unavailable(
                trigger=Trigger.MATERIAL,
                fingerprint=fingerprint,
                serializer_version=SERIALIZER_VERSION,
                reason=why,
            )

        serialised: SerializedState | None = None
        exact_tokens: int | None = None
        started = time.monotonic()
        try:
            serialised = serialize_state(state)
            # Exact admission. The character ratio is not a token count and
            # cannot enforce the invariant; the pinned Laya tokenizer is. A count
            # that cannot be obtained is a refusal, not an estimate: the request
            # is not dispatched and the observation records why.
            admission = enforce_budget(serialised.text)
            exact_tokens = admission.serialized_state_tokens
            if not admission.admitted:
                snapshot = unavailable(
                    trigger=Trigger.MATERIAL,
                    fingerprint=fingerprint,
                    serializer_version=SERIALIZER_VERSION,
                    reason=admission.reason or "state_not_admitted",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    state_truncated=serialised.state_truncated,
                    evicted_by_tier=serialised.evicted_by_tier,
                    overflowed_budget=serialised.overflowed_budget,
                    serialized_chars=serialised.serialized_chars,
                    serialized_state_tokens=exact_tokens,
                    dropped_field_count=len(serialised.fields_dropped),
                )
                return self._record(snapshot)
        except Exception as error:  # noqa: BLE001 - fail open, by design
            snapshot = unavailable(
                trigger=Trigger.MATERIAL,
                fingerprint=fingerprint,
                serializer_version=SERIALIZER_VERSION,
                reason=f"serializer_error:{type(error).__name__}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return self._record(snapshot)

        try:
            body = self._request_body(serialised)
            payload, transport_error = self._post(body)
        except Exception as error:  # noqa: BLE001 - fail open, by design
            payload, transport_error = None, f"{type(error).__name__}"

        latency_ms = int((time.monotonic() - started) * 1000)

        if transport_error is not None or not isinstance(payload, Mapping):
            snapshot = unavailable(
                trigger=Trigger.MATERIAL,
                fingerprint=fingerprint,
                serializer_version=SERIALIZER_VERSION,
                reason=transport_error or "invalid_response",
                latency_ms=latency_ms,
                state_truncated=serialised.state_truncated,
                evicted_by_tier=serialised.evicted_by_tier,
                overflowed_budget=serialised.overflowed_budget,
                serialized_chars=serialised.serialized_chars,
                serialized_state_tokens=exact_tokens,
                dropped_field_count=len(serialised.fields_dropped),
            )
            return self._record(snapshot)

        answers = payload.get("answers")
        if not isinstance(answers, Mapping):
            snapshot = unavailable(
                trigger=Trigger.MATERIAL,
                fingerprint=fingerprint,
                serializer_version=SERIALIZER_VERSION,
                reason="invalid_response:no_answers",
                latency_ms=latency_ms,
                state_truncated=serialised.state_truncated,
                evicted_by_tier=serialised.evicted_by_tier,
                overflowed_budget=serialised.overflowed_budget,
                serialized_chars=serialised.serialized_chars,
                serialized_state_tokens=exact_tokens,
                dropped_field_count=len(serialised.fields_dropped),
            )
            return self._record(snapshot)

        # Which backend ANSWERED is a fact about the request, not a rank.
        # 2026-10-17 adaptation: the live Switchyard composition lane answers
        # with `backend`/`child_lane`/`served_model`/`requested` and NO legacy
        # `provider` field. The historical reader understood only `provider`
        # and would have labelled every live observation `unknown`. The new
        # shape is read first; the legacy field stays a fallback so the reader
        # stays truthful against either generation of the backend.
        fallback_used = bool(payload.get("fallback_used", False))
        reported = (
            payload.get("backend")
            or payload.get("provider")
        )
        if isinstance(reported, str) and reported.strip():
            backend = reported.strip()
        else:
            # No backend field on a failover leg: record the mechanism, not a
            # provider name, so the value stays truthful if the leg changes.
            backend = "failover_leg" if fallback_used else "unknown"
        # BELIEVED only from qualified backends. An unqualified leg answers on
        # the wire; its readings are kept as telemetry, never reported as
        # observations. Provenance is preserved either way.
        qualified = backend in QUALIFIED_BACKENDS
        snapshot = build_snapshot(
            trigger=Trigger.MATERIAL,
            fingerprint=fingerprint,
            serializer_version=SERIALIZER_VERSION,
            answers=answers,
            backend=backend,
            fallback_used=fallback_used,
            latency_ms=latency_ms,
            state_truncated=serialised.state_truncated,
            evicted_by_tier=serialised.evicted_by_tier,
            overflowed_budget=serialised.overflowed_budget,
            serialized_chars=serialised.serialized_chars,
            serialized_state_tokens=exact_tokens,
            dropped_field_count=len(serialised.fields_dropped),
            qualified=qualified,
            unqualified_raw=(None if qualified else {
                str(name): raw for name, raw in answers.items()
            }),
        )
        return self._record(snapshot)

    # -- internals -------------------------------------------------------

    def _request_body(self, serialised: SerializedState) -> dict[str, Any]:
        """Build the decision request locally.

        The question definitions are graded `noul` questions with explicit
        criteria, and the body is a plain three-key object. Capability
        discovery is a separate concern and is not needed to SEND a request.

        2026-10-17 adaptation: the historical note about avoiding
        ``sensor_layer`` no longer applies — 1.8.7 ships ``typed_decision`` —
        but the shadow path still builds its own body: the sensor's five
        questions are deliberately separate from the Output Guard's single
        categorical risk question, and ``build_request_body`` hardcodes the
        guard's. Sharing the transport would overwrite a security control's
        question.
        """
        from .sensor_questions import build_request

        return build_request(
            decision_id=self._decision_id or DECISION_CAPABILITY,
            state=serialised.text,
            signal_names=REQUESTED_SIGNALS,
        )

    def _post(self, body: Mapping[str, Any]) -> tuple[Any, str | None]:
        request = urllib.request.Request(
            f"{self.switchyard.rstrip('/')}/v1/decisions",
            data=json.dumps(body).encode(),
            method="POST",
        )
        request.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read()), None
        except urllib.error.HTTPError as error:
            return None, f"http_{error.code}"
        except Exception as error:  # noqa: BLE001 - timeout, refused, malformed
            return None, f"transport:{type(error).__name__}"

    def _record(self, snapshot):
        self.metrics.observation_count += 1
        self.metrics.total_latency_ms += snapshot.latency_ms
        self.metrics.serialized_chars.append(snapshot.serialized_chars)
        self.metrics.serialized_state_tokens.append(snapshot.serialized_state_tokens)
        self.metrics.dropped_field_count.append(snapshot.dropped_field_count)
        self.metrics.backends[snapshot.backend] += 1
        if snapshot.reason:
            self.metrics.unavailable_count += 1
        if snapshot.state_truncated:
            self.metrics.truncated_count += 1
        if snapshot.fallback_used:
            self.metrics.fallback_count += 1
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            with self.store.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot.to_dict()) + "\n")
        except Exception:  # noqa: BLE001 - storage failure must not fail a task
            pass
        return snapshot


def resolve_decision_endpoint(registry: Any = None) -> tuple[str, str]:
    """Resolve (decision_id, base_url) for the sensor's decision lane.

    2026-10-17 adaptation. The historical sensor hardcoded the lane name; the
    live surface is the ``tier1-decision`` registry row, the same row the
    Output Guard's typed path resolves - so the sensor follows the row rather
    than a constant, and a Switchyard-side change to the lane is picked up
    without a sensor code change.

    Reads ONLY the row's identity and typed-decision capabilities. It never
    reads or writes a routing, permission, approval or Output Guard value, and
    the result is used to send the sensor's own five graded-noul questions -
    not the guard's categorical risk question, whose contract stays untouched.

    ``registry`` may be None (tests, no registry available): the module's
    fallback constants apply. Any registry failure is a fallback, not an
    error: an advisory sensor must not make registry health its problem.
    """
    alias = DECISION_CAPABILITY
    try:
        if registry is None:
            raise LookupError("no registry")
        # Mirror the Output Guard's accessors: identity and capabilities both
        # come off the row via get_config. An unusable registry falls back
        # exactly like a missing one.
        get_config = getattr(registry, "get_config", None)
        if get_config is None:
            raise LookupError("registry exposes no get_config")
        cfg = get_config(alias)
        caps = dict(getattr(cfg, "capabilities", {}) or {})
        if not caps.get("supports_typed_decision"):
            raise LookupError(f"{alias!r} is not a typed-decision row")
        base_url = str(getattr(cfg, "base_url", "") or "").strip()
        if not base_url:
            raise LookupError(f"{alias!r} carries no base_url")
        return alias, base_url
    except Exception:  # noqa: BLE001 - fail back to constants, never raise
        return alias, SWITCHYARD
