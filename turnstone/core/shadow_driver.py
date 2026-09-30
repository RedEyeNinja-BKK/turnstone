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
import pathlib
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

from .bounded_state import (
    SERIALIZER_VERSION,
    SerializedState,
    estimate_tokens,
    serialize_state,
)
from .shadow_observation import (
    DECISION_CAPABILITY,
    REQUESTED_SIGNALS,
    SENSOR_VERSION,
    Trigger,
    build_snapshot,
    is_material,
    state_fingerprint,
    unavailable,
)

#: Storage is a local JSONL file: append-only, advisory, and trivially
#: inspectable. A storage failure is caught and swallowed — losing an observation
#: must never fail a task.
DEFAULT_STORE = pathlib.Path(
    "/home/vincent/shared-workspace/operations/switchyard-sensor-layer-20260929/shadow-observations.jsonl"
)

SWITCHYARD = "http://127.0.0.1:4000"
REQUEST_TIMEOUT_SECONDS = 20


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
    """Cadence, dedupe, one capability call, and a stored record."""

    def __init__(
        self,
        *,
        store: pathlib.Path = DEFAULT_STORE,
        switchyard: str = SWITCHYARD,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self.switchyard = switchyard
        self.timeout = timeout
        self.metrics = ShadowMetrics()
        self._last_fingerprint: str | None = None
        self._initial_done = False

    # -- cadence ---------------------------------------------------------

    def should_sense(
        self,
        state: Mapping[str, Any],
        *,
        event: str | None,
        force: bool = False,
    ) -> tuple[bool, str]:
        """Decide whether this state warrants a fresh observation.

        Order matters: an unchanged state is suppressed before anything else,
        because that is the common case and it must not cost a call.
        """
        fingerprint = state_fingerprint(state)
        if force:
            return True, "forced"
        if not self._initial_done:
            return True, Trigger.INITIAL.value
        if not is_material(event or ""):
            return False, f"not_material:{event}"
        if fingerprint == self._last_fingerprint:
            return False, "unchanged_state"
        return True, Trigger.MATERIAL.value

    # -- observation -----------------------------------------------------

    def observe(
        self,
        state: Mapping[str, Any],
        *,
        event: str | None = None,
        force: bool = False,
    ):
        """Take one advisory observation, or record why none was taken.

        Never raises. Every failure becomes an unavailable snapshot so the
        calling task is unaffected.
        """
        fingerprint = state_fingerprint(state)
        allowed, why = self.should_sense(state, event=event, force=force)
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
        started = time.monotonic()
        try:
            serialised = serialize_state(state)
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
                serialized_chars=serialised.serialized_chars,
                serialized_state_tokens=serialised.estimated_tokens,
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
                serialized_chars=serialised.serialized_chars,
                serialized_state_tokens=serialised.estimated_tokens,
                dropped_field_count=len(serialised.fields_dropped),
            )
            return self._record(snapshot)

        fallback_used = bool(payload.get("fallback_used", False))
        backend = "laya-fallback" if fallback_used else str(
            payload.get("provider") or "span"
        )
        snapshot = build_snapshot(
            trigger=Trigger.MATERIAL,
            fingerprint=fingerprint,
            serializer_version=SERIALIZER_VERSION,
            answers=answers,
            backend=backend,
            fallback_used=fallback_used,
            latency_ms=latency_ms,
            state_truncated=serialised.state_truncated,
            serialized_chars=serialised.serialized_chars,
            serialized_state_tokens=serialised.estimated_tokens,
            dropped_field_count=len(serialised.fields_dropped),
        )
        self._last_fingerprint = fingerprint
        self._initial_done = True
        return self._record(snapshot)

    # -- internals -------------------------------------------------------

    def _request_body(self, serialised: SerializedState) -> dict[str, Any]:
        """Build the decision request locally.

        Deliberately NOT routed through `sensor_layer.build_sensor_request`:
        that module imports `typed_decision`, which the live Turnstone package
        does not ship, so importing it would make the whole shadow path
        unimportable in production. The question definitions are the same
        graded `noul` questions with the same criteria, and the body is a plain
        three-key object, so the wire request is identical. Capability discovery
        is a separate concern and is not needed to SEND a request.
        """
        from .sensor_questions import build_request

        return build_request(
            decision_id=DECISION_CAPABILITY,
            state=serialised.text,
            signal_names=REQUESTED_SIGNALS,
        )

    def _post(self, body: Mapping[str, Any]) -> tuple[Any, str | None]:
        request = urllib.request.Request(
            f"{self.switchyard}/v1/decisions",
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
