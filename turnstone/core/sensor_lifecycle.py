"""Material-event wiring for the shadow sensor.

This module is the *only* place that knows how Turnstone's lifecycle maps onto
sensor triggers. It exists as a separate unit so the lifecycle code needs one
small, obviously-telemetry call rather than any sensor logic.

Four properties this is built around, and the seams were chosen for them:

* **No every-turn hook.** There is no call from the ordinary assistant-turn
  commit path. Sensing fires on a whitelist: an admitted substantive operator
  instruction, an accepted errored tool result, a published delegation result, a
  published delegation failure, and a real retry. An acknowledgement, a
  format-only turn, an unchanged poll and a routine heartbeat produce no call.
* **Never influences the task.** :func:`observe_event` cannot raise, its return
  value is discarded, and it writes nothing to session messages, UI events or
  the workstream config. It takes no routing, model, tool, approval or guard
  input and returns none.
* **Post-hoc only.** Every seam is an *accepted* or *published* event, so a
  rejected, superseded or stale-generation action is never observed as if it had
  happened.
* **Honest about what does not exist.** Turnstone has no typed semantic phase
  machine and no evidence classifier, so ``phase_transition`` and ``new_evidence``
  have no honest source here. They are declared as
  :data:`UNWIRED_TRIGGERS` rather than approximated from ordinary turns, because
  approximating them is exactly how an every-turn sensor gets built by accident.

Semantic state is a deliberately bounded projection, assembled by
:func:`build_state`. It carries the objective, phase, blockers, the tools in
play, the acting agent and a bounded slice of recent history — never a
timestamp, a counter, a request id or a latency, so an unchanged state
fingerprints identically and dedupe can suppress the call.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
import unicodedata
import uuid
import weakref
from collections.abc import Mapping
from typing import Any

from .shadow_driver import ShadowSensor, append_jsonl
from .shadow_observation import PROCESS_ID, is_material, state_fingerprint

#: Cadence rows go here, deliberately separate from the observation store. A
#: suppressed event writes no observation, so counting the calls that did NOT
#: happen is only possible in its own log.
#:
#: 2026-10-17 adaptation: service-writable location, env-overridable, aligned
#: with shadow_driver.DEFAULT_STORE so observations and cadence share a root.
DEFAULT_CADENCE_STORE = pathlib.Path(
    os.environ.get("TURNSTONE_SENSOR_STORE_DIR", "/var/lib/turnstone/sensor")
) / "shadow-cadence.jsonl"

#: 2026-10-17 adaptation: DISABLED BY DEFAULT.
#:
#: The sensor is recovery-stage software. Until an operator activates it, every
#: seam must be a no-op that costs nothing and emits nothing: no decision call,
#: no fingerprint commit, no telemetry file. The gate is read once per hook
#: construction (not per event) so a disabled sensor has zero per-event cost.
#: Activation is an explicit operator decision — see the recovery report's
#: privacy boundary: enabling this sends bounded operator/task text off-box.
ENABLED = os.environ.get("TURNSTONE_SENSOR_ENABLED", "0") not in {
    "", "0", "false", "False", "no", "off",
}

#: Triggers in the sensor's whitelist that have NO honest source in Turnstone's
#: current lifecycle, and are therefore deliberately not emitted:
#:
#: ``phase_transition``
#:     Turnstone's workstream states are running/idle/error. Neither an assistant
#:     commit nor a tool execution proves a plan became execution or that
#:     execution became verification. Guessing here would mean firing on ordinary
#:     turns.
#: ``new_evidence``
#:     No code classifies an artifact or tool result as material evidence.
#:     Observing every successful tool result is the per-tool-call hook this
#:     sensor is required not to have.
#:
#: Both are recorded so the omission is visible rather than looking forgotten.
UNWIRED_TRIGGERS = ("phase_transition", "new_evidence")

#: Upper bounds on the semantic projection. These keep the state small enough to
#: fingerprint cheaply and to stay inside the serializer's token budget.
MAX_HISTORY_ITEMS = 6
MAX_ITEM_CHARS = 400

#: How much operator/task TEXT may leave the VM when the sensor is enabled.
#:
#: ``sanitized`` (default) keeps the sentence but removes secrets, credentials,
#: absolute paths and e-mail addresses, so the graded questions still see task
#: shape. ``metadata`` sends no free text at all - only bounded descriptors
#: (lengths, word counts, error class, categorical phase/tool/agent values).
#: ``full`` reproduces the historical projection and exists only so the
#: qualification comparison can be run; it must be opted into explicitly.
#:
#: Activation requires an explicit operator decision on this exposure; the
#: default is the middle setting so an accidental enable is not a full-text
#: export.
CONTENT_MODE = os.environ.get("TURNSTONE_SENSOR_CONTENT_MODE", "sanitized").strip().lower()
if CONTENT_MODE not in {"full", "sanitized", "metadata"}:
    CONTENT_MODE = "sanitized"

#: Credential/secret shapes. Whole-value patterns come first, then key-prefix
#: forms, then long opaque runs. Matching is linear-time by construction: no
#: nested quantifiers and no backreferences.
_SECRET_PATTERNS = (
    # PEM blocks first: they span lines and must be removed whole.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S,
    ),
    # JWTs: three base64url segments. Without this the header segment survives
    # the long-token rule (dots split it) - measured in the S4 battery.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"\b(?:bearer)\s+\S+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Concatenated base64 values: '=' padding in the middle splits a single
    # long-run rule into two short runs, so neither is long enough to match.
    # Each run is bounded so a pathological input stays linear.
    re.compile(r"(?:[A-Za-z0-9+/]{16,512}={1,2}){2,}"),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
)

#: NAME=value assignments whose NAME carries a secret word. The leading
#: character class is deliberate: a plain \b fails on OPENAI_API_KEY=...
#: because "_" is a word character, which let the value through in the S4
#: battery. This matches the whole assignment, including the prefix.
#:
#: It is applied to a character-folded copy of the text, because a literal
#: keyword misses a diacritic spelling ("pässwörd") - the second S4 residual.
_ASSIGNMENT = re.compile(
    r"[\w.-]*(?:key|token|secret|password|passwd|credential)[\w.-]*"
    r"\s*[:=]\s*\S+",
    re.I,
)

#: Single-character fallback for letters NFKD does not decompose. Every entry
#: maps to exactly one character so folded offsets stay aligned with the input.
_FOLD_SINGLE = str.maketrans({
    "\u00df": "s", "\u00f8": "o", "\u00e6": "a", "\u0111": "d",
    "\u0142": "l", "\u00fe": "t", "\u00f0": "d",
})
_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_ABS_PATH = re.compile(r"(?<![\w/])/(?:[\w.@+-]+/)+[\w.@+-]+")

_ERROR_PREFIX = re.compile(r"^\s*Task error:\s*([A-Za-z_][\w.]*)")


def _fold_for_keywords(text: str) -> str:
    """One-character-per-input-character ASCII fold for keyword matching.

    Index preserving by construction: every character maps to exactly one
    character, so a match span on the folded string is a valid span on the
    original. Non-decomposable non-ASCII characters pass through unchanged;
    they simply cannot contribute to an ASCII keyword.
    """
    out = []
    for char in text:
        if char.isascii():
            out.append(char)
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", char)
            if not unicodedata.combining(c)
        )
        if len(base) == 1:
            out.append(base.translate(_FOLD_SINGLE))
        else:
            out.append(char)
    return "".join(out)


def _redact_assignments(text: str) -> str:
    """Redact secret-word assignments, tolerating diacritic keyword spellings."""
    spans = [m.span() for m in _ASSIGNMENT.finditer(_fold_for_keywords(text))]
    if not spans:
        return text
    chars = list(text)
    for start, end in reversed(spans):
        chars[start:end] = "[redacted]"
    return "".join(chars)


def _sanitize_text(text: str) -> str:
    """Remove secrets, absolute paths and e-mails; keep the task sentence."""
    cleaned = text
    cleaned = _URL_USERINFO.sub(r"\1[redacted]@", cleaned)
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = _redact_assignments(cleaned)
    cleaned = _EMAIL.sub("[email]", cleaned)
    cleaned = _ABS_PATH.sub("[path]", cleaned)
    return cleaned


def _metadata_text(name: str, text: str) -> str:
    """No free text: a bounded descriptor of the field instead."""
    if name == "blocker":
        match = _ERROR_PREFIX.match(text)
        if match:
            return f"[blocker error={match.group(1)}]"
        return f"[blocker chars={len(text)}]"
    words = len(text.split())
    return f"[{name} chars={len(text)} words={words}]"


def project_value(name: str, value: Any, *, mode: str | None = None) -> Any:
    """Apply the active content mode to one projected field.

    ``full`` is the historical behaviour. ``sanitized`` removes secrets but
    keeps the text. ``metadata`` removes all free text, leaving descriptors -
    the mode to choose when the exposure decision is "nothing leaves".
    Categorical fields (phase/tool/agent) pass through in every mode; they are
    closed-class values the caller chose, not operator content.
    """
    active = (mode or CONTENT_MODE).strip().lower()
    if not isinstance(value, str):
        return value
    # An absent field stays absent in every mode: a descriptor for "" would be
    # a fake fact ("chars=0") that also defeats the empty-field drop.
    if not value.strip():
        return ""
    if name in {"phase", "tool", "tools", "agent"}:
        return value
    if active == "full":
        return value
    if active == "metadata":
        return _metadata_text(name, value)
    return _sanitize_text(value)

#: Keys that must never enter the fingerprint. A volatile field here would make
#: identical work look new and defeat dedupe entirely.
VOLATILE_KEYS = frozenset({
    "timestamp", "time", "ts", "now", "elapsed", "latency_ms", "latency",
    "duration", "duration_ms", "request_id", "req_id", "call_id", "message_id",
    "turn_id", "generation", "send_id", "poll_count", "poll_counter",
    "attempt", "retry_count", "uptime", "counter",
})

#: Control surfaces that must never be observable through the sensor, even if a
#: caller passes them by mistake. This is defence in depth: the authority
#: boundary is not "callers are careful", it is "there is no way to express a
#: routing control here". A control leaking into the projection would be
#: telemetry about a control, and the next reader could act on it.
FORBIDDEN_PROJECTION_KEYS = frozenset({
    "model", "provider", "route", "target", "llm_client", "client", "lane",
    "reasoning", "reasoning_effort", "effort", "temperature", "top_p", "seed",
    "max_tokens", "max_output_tokens", "agent", "task_agent", "task_agent_model",
    "tools", "tool_permissions", "permissions", "approval", "approvals",
    "approval_state", "escalate", "escalation", "retry", "output_guard",
    "guard", "judge", "score_override",
})


def _clean(value: Any, *, limit: int = MAX_ITEM_CHARS) -> str:
    """Render one field as bounded single-line text.

    Truncation is a plain character cut on a word boundary; the serializer does
    the semantic tiering, this only keeps a value from becoming a paragraph.
    """
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def _drop_volatile(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return `mapping` without any volatile or control key.

    Separate from :func:`build_state` so the filter is a real, testable function
    rather than a condition that only runs on a fixed field list. A volatile
    field in the projection would make unchanged work fingerprint as new and
    defeat dedupe silently.

    Control keys are dropped for a different reason: the sensor is advisory and
    must not be able to observe a routing, provider, tool-permission, approval
    or Output Guard value even by accident.
    """
    return {
        str(name): value
        for name, value in dict(mapping).items()
        if str(name) not in VOLATILE_KEYS
        and str(name).casefold() not in FORBIDDEN_PROJECTION_KEYS
    }


def build_state(
    *,
    objective: str = "",
    phase: str = "",
    blockers: Any = "",
    tools: Any = "",
    agent: str = "",
    history: Any = "",
    content_mode: str | None = None,
    **extra: Any,
) -> dict[str, str]:
    """Assemble the bounded semantic projection the sensor observes.

    Volatile keys are dropped by construction, including any passed through
    `extra`. An empty projection is valid and means "nothing meaningful
    changed", which suppresses the call.
    """
    raw: dict[str, Any] = {
        "objective": objective,
        "phase": phase,
        "blocker": blockers,
        "tool": tools,
        "agent": agent,
        "history": history,
        **extra,
    }
    state: dict[str, str] = {}
    for name, value in _drop_volatile(raw).items():
        text = _clean(project_value(name, value, mode=content_mode))
        if text:
            state[name] = text
    return state


#: Closed-class acknowledgements. Matched against the message with trailing
#: punctuation and case removed, because "thanks!" and "Sure." are the same
#: non-substantive message as "thanks" and "sure".
ACKNOWLEDGEMENTS = frozenset({
    "ok", "okay", "k", "yes", "no", "y", "n", "yeah", "yep", "yup", "nope",
    "thanks", "thank you", "thx", "ty", "sure", "np", "welcome", "no problem",
    "got it", "gotcha", "understood", "roger", "ack", "acknowledged", "done",
    "finished", "complete", "completed", "nice", "great", "cool", "awesome",
    "perfect", "sounds good", "looks good", "hi", "hello", "hey", "good",
    "morning", "evening", "cheers", "ta", "ok thanks", "thanks a lot",
    # continuation nudges: the operator wants the current work to proceed, which
    # carries no new state of its own. The whitelist has a separate
    # `escalation_transition` trigger for a real transition.
    "continue", "go", "proceed", "carry on", "go on", "keep going", "next", "resume", "carryon",
})


def is_substantive_instruction(user_input: str) -> bool:
    """Whether an operator message carries real work.

    Deliberately conservative. An acknowledgement, a greeting, a single
    character, or a pure formatting instruction does not justify a sensing call.
    Punctuation and case are stripped before the closed-class check, so
    "thanks!" and "Sure." are recognised rather than admitted as work.
    The threshold is on *content*, not length alone.
    """
    text = " ".join((user_input or "").split())
    if not text:
        return False
    bare = text.strip(" .!?…").casefold()
    if bare in ACKNOWLEDGEMENTS:
        return False
    letters = sum(character.isalnum() for character in text)
    if letters < 3:
        return False
    if letters / max(1, len(text)) < 0.25:
        return False
    return True


class SensorHook:
    """Owns the one sensor instance, the whitelist decision, and cadence metrics.

    Exactly one instance per process, created lazily. A failure anywhere inside
    is contained: sensing is advisory and must never affect a task.

    Cadence is recorded **separately** from the observation, in its own append-only
    JSONL. Two reasons:

    * A *suppressed* event produces no observation, so if cadence lived only in
      the observation record it would be permanently invisible and the dedupe
      rate could never be computed. Counting is the whole point of a shadow
      sensor: the calls it did NOT make are the measurement.
    * Observation records stay a clean signal contract. Cadence rows are
      operational bookkeeping and must not contaminate them.

    Cadence rows carry no state text, no signals, and no control value.
    """

    def __init__(
        self,
        sensor: ShadowSensor | None = None,
        *,
        cadence_store: pathlib.Path | None = None,
        persist: bool = True,
    ) -> None:
        self._sensor = sensor if sensor is not None else ShadowSensor()
        # 2026-10-17: when a record cannot be persisted, the ledger says so.
        # A sensor_call row then pairs with either exactly one observation or
        # exactly one observation_write_failed row - never with silence.
        if getattr(self._sensor, "on_record_failure", None) is None:
            self._sensor.on_record_failure = self._on_record_failure
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._workstreams: dict[str, int] = {}
        # `persist=False` (or an explicit path) is what a test should pass.
        # Defaulting every construction to the production log meant the test
        # suite wrote into it: 191 fake `sensor_call` rows and 69 `hook_error`
        # rows from a deliberately exploding sensor, all with no workstream and
        # synthetic fingerprints. Once aggregated they were indistinguishable
        # from real traffic -- a fake 887 calls/hour. Instrumentation that its
        # own test suite can pollute is not instrumentation.
        self._cadence_store = (
            DEFAULT_CADENCE_STORE
            if (persist and cadence_store is None)
            else cadence_store
        )
        self._started = time.time()

    def _on_record_failure(
        self, event_id: str, fingerprint: str, reason: str
    ) -> None:
        """Pair a lost observation with its cadence row (best effort)."""
        try:
            self._record_cadence(
                "", "observation_write_failed", "", fingerprint, event_id
            )
        except Exception:  # noqa: BLE001
            pass

    # -- counters -------------------------------------------------------

    def _bump(self, name: str) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def workstream_counts(self) -> dict[str, int]:
        """Sensor calls per workstream id. Empty for events with no workstream."""
        with self._lock:
            return dict(self._workstreams)

    def _record_cadence(
        self,
        event: str,
        outcome: str,
        workstream: str = "",
        fingerprint: str = "",
        event_id: str = "",
    ) -> None:
        """Append one cadence row. Best-effort: a storage failure is swallowed.

        Cadence bookkeeping must never become a failure mode of the task, and a
        dropped counter is strictly better than an exception here.
        """
        try:
            with self._lock:
                self._counts[outcome] = self._counts.get(outcome, 0) + 1
                if workstream:
                    self._workstreams[workstream] = self._workstreams.get(workstream, 0) + 1
                row = {
                    "ts": time.time(),
                    "event": event,
                    "outcome": outcome,
                    "workstream": workstream,
                    "fingerprint": fingerprint,
                    # Correlation + attribution: a sensor_call row pairs with
                    # exactly one observation carrying this event_id, and the
                    # node:pid says which of the six processes observed it.
                    "event_id": event_id,
                    "process_id": PROCESS_ID,
                }
            if self._cadence_store is None:
                return
            append_jsonl(self._cadence_store, row)
        except Exception:  # noqa: BLE001
            pass

    def cadence_report(self) -> dict[str, Any]:
        """Aggregate cadence since process start. Used by the review harness."""
        with self._lock:
            counts = dict(self._counts)
            workstreams = dict(self._workstreams)
        elapsed_hours = max((time.time() - self._started) / 3600.0, 1e-9)
        calls = counts.get("sensor_calls", 0)
        return {
            "counts": counts,
            "workstreams": workstreams,
            "distinct_workstreams": len(workstreams),
            "elapsed_hours": round(elapsed_hours, 4),
            "calls_per_hour": round(calls / elapsed_hours, 3),
            "dedupe_suppression_rate": (
                round(counts.get("dedupe_suppressed", 0) / max(1, calls), 4)
            ),
        }

    # -- the single call sites make ------------------------------------

    def observe_event(
        self,
        event: str,
        state: Mapping[str, Any] | None = None,
        *,
        force: bool = False,
        workstream: str = "",
    ) -> None:
        """Observe one material event. Never raises; the result is discarded."""
        # One id per admitted event, minted before anything can fail. It is the
        # join key between the cadence ledger and the observation store.
        event_id = uuid.uuid4().hex
        try:
            with self._lock:
                fingerprint = state_fingerprint(state) if state else ""
            if not force and not is_material(event):
                self._record_cadence(
                    event, "rejected_not_material", workstream, fingerprint, event_id
                )
                return
            # Eager, validated copy. `dict(state)` alone can defer an exploding
            # mapping into the sensor, where a different layer would have to
            # catch it; materialising here keeps containment in this function.
            projection = {str(name): value for name, value in dict(state or {}).items()}
            allowed, _why = self._sensor.should_sense(
                projection, event=event, force=force, workstream=workstream
            )
            if not allowed:
                self._record_cadence(
                    event, "dedupe_suppressed", workstream, fingerprint, event_id
                )
                return
            self._record_cadence(
                event, "sensor_call", workstream, fingerprint, event_id
            )
            # `decided=True`: the should_sense() above is the SINGLE cadence
            # decision and the single fingerprint commit for this event. Without
            # this flag observe() gated a second time, saw its own just-committed
            # fingerprint as `unchanged_state`, and returned without recording
            # anything -- cadence said `sensor_call` while the observation store
            # stayed empty. Proven live on the 2026-09-30 canary, where the
            # sensor ran for minutes and wrote zero observations.
            #
            # NOT force=True, deliberately. force is the caller's escape hatch
            # for an unqualified event; using it here would bypass is_material()
            # and re-admit suppressed states. `decided` expresses WHO owns the
            # decision, which is the actual defect. force would have hidden it
            # while leaving the double gate in place.
            # Cadence belongs to the hook, not to the network-facing call.
            # 2026-10-17: the workstream id is passed through so dedupe (and
            # the observation's own workstream attribution) is per workstream,
            # not per process.
            self._sensor.observe(
                projection,
                event=event,
                decided=True,
                workstream=workstream,
                event_id=event_id,
                content_mode=CONTENT_MODE,
            )
        except Exception:  # noqa: BLE001 - a sensor must never fail a task
            try:
                self._record_cadence(event, "hook_error", workstream, "", event_id)
            except Exception:  # noqa: BLE001
                pass


#: Hook used when no registry is supplied — the UNRESOLVED case, plus the
#: override point the suite uses to drive the module-level wrapper. Created on
#: first use so importing this module has no side effect on a process that
#: never senses.
_HOOK: SensorHook | None = None

#: Registry-scoped hooks, keyed by the registry object's ``id`` and validated by
#: BOTH a weak reference and the registry's generation. This is a *cache*, not a
#: registry binding: no module global ever holds "the" registry, and a hook is
#: only ever returned for the exact object and generation it was built from.
#:
#: Two sessions backed by different registries therefore get independent
#: endpoints and independent dedupe state.
#:
#: The generation matters because ``ModelRegistry.reload()`` swaps models IN
#: PLACE on the same object. Validating identity alone served the endpoint
#: resolved before a reload - graded questions would have kept flowing to a lane
#: the operator had already changed, silently. A reload bumps ``generation``, so
#: the next event misses the cache and rebuilds. Dead entries are pruned while
#: the lock is held, so a long-lived process cannot accumulate them.
_HOOKS: dict[int, tuple[Any, Any, SensorHook]] = {}
_HOOK_LOCK = threading.Lock()


def _generation_of(registry: Any) -> Any:
    """The registry's reload generation, or ``None`` when it has no such notion.

    Read defensively: a registry that raises from this attribute (or does not
    define it) simply caches on identity alone, which is the historical
    behaviour and is correct for an object that cannot be mutated in place.
    """
    try:
        generation = getattr(registry, "generation", None)
    except Exception:  # noqa: BLE001 - a hostile/incomplete registry must not raise
        return None
    return generation if isinstance(generation, int) else None


def _build_hook(registry: Any) -> SensorHook:
    """Build a hook whose sensor resolves the decision lane FROM ``registry``.

    A failed resolution yields ``("", "")`` — an UNRESOLVED sensor whose
    observations are explicitly unavailable with no network request of any
    kind. There is deliberately no fallback URL and no fallback alias here:
    silence would be the dishonest option, and inventing an endpoint would be
    worse.
    """
    from .shadow_driver import ShadowSensor, resolve_decision_endpoint

    decision_id, base_url = resolve_decision_endpoint(registry)
    return SensorHook(ShadowSensor(decision_id=decision_id, base_url=base_url))


def hook(registry: Any = None) -> SensorHook:
    """Return the sensor hook for ``registry``.

    ``registry`` is the session's own ``ModelRegistry`` (or any object exposing
    ``get_config``). Passing it is what makes the decision lane resolvable: the
    historical wiring called the resolver with no registry at all, so every
    activated observation was recorded as ``decision_endpoint_unresolved`` and
    no graded signal was ever produced.

    With ``registry=None`` this returns the unresolved process hook, preserving
    the historical fail-closed behaviour and the suite's override point.
    """
    global _HOOK
    if registry is None:
        if _HOOK is None:
            with _HOOK_LOCK:
                if _HOOK is None:
                    _HOOK = _build_hook(None)
        return _HOOK

    key = id(registry)
    generation = _generation_of(registry)
    with _HOOK_LOCK:
        entry = _HOOKS.get(key)
        if entry is not None:
            ref, cached_generation, existing = entry
            if ref() is registry and cached_generation == generation:
                return existing
        # Prune entries whose registry has been collected, then (re)build. The
        # weakref check above makes an id reused after GC safe: identity, not
        # the integer, is what decides a cache hit.
        for stale_key in [k for k, (ref, _gen, _hook) in _HOOKS.items() if ref() is None]:
            del _HOOKS[stale_key]
        try:
            ref = weakref.ref(registry)
        except TypeError:  # registry is not weak-referenceable
            ref = _strong_ref(registry)
        built = _build_hook(registry)
        _HOOKS[key] = (ref, generation, built)
        return built


def _strong_ref(value: Any) -> Any:
    """Fallback reference for objects that cannot be weakly referenced."""
    return lambda: value


def reset_hooks() -> None:
    """Drop every cached hook. Test/reset helper; not a production path."""
    global _HOOK
    with _HOOK_LOCK:
        _HOOK = None
        _HOOKS.clear()


def observe_event(
    event: str,
    state: Mapping[str, Any] | None = None,
    *,
    force: bool = False,
    workstream: str = "",
    registry: Any = None,
) -> None:
    """Module-level convenience wrapper around :func:`hook`.

    This is the seam production code calls, so the disabled-by-default gate
    lives HERE: when ENABLED is False (the default) it is a no-op before any
    gate runs — no cadence row, no fingerprint commit, no file I/O. The
    SensorHook method above stays ungated so the suite can exercise the hook
    logic directly.

    ``registry`` is the calling session's registry, passed explicitly on every
    event. It is not stored anywhere: it only selects (or builds) that
    registry's hook.
    """
    if not ENABLED:
        return
    hook(registry).observe_event(event, state, force=force, workstream=workstream)


def verify_correlation(
    cadence_store: pathlib.Path,
    observations_store: pathlib.Path,
) -> dict[str, Any]:
    """Audit the ledger/store pairing for one telemetry root.

    Operational tool, not a production path. Returns counts only - no state
    text is read into the result. The invariants it checks:

    * every admitted ``sensor_call`` has exactly one observation or exactly one
      ``observation_write_failed`` row with the same ``event_id``;
    * no observation exists without a matching admitted cadence row;
    * no ``event_id`` is duplicated on either side.
    """
    def _rows(path: pathlib.Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001 - a torn line is reported, not fatal
                rows.append({"__unparseable__": True})
        return rows

    cadence = _rows(cadence_store)
    observations = _rows(observations_store)

    admitted = [r for r in cadence if r.get("outcome") == "sensor_call"]
    failed = [r for r in cadence if r.get("outcome") == "observation_write_failed"]
    admitted_ids = [r.get("event_id", "") for r in admitted]
    observed_ids = [r.get("event_id", "") for r in observations]
    failed_ids = {r.get("event_id", "") for r in failed}

    observed_set = set(observed_ids)
    unmatched = [
        eid for eid in admitted_ids
        if eid and eid not in observed_set and eid not in failed_ids
    ]
    orphans = [eid for eid in observed_ids if eid and eid not in set(admitted_ids)]

    return {
        "admitted_calls": len(admitted),
        "observations": len(observations),
        "write_failures": len(failed),
        "unparseable_cadence": sum(1 for r in cadence if r.get("__unparseable__")),
        "unparseable_observations": sum(
            1 for r in observations if r.get("__unparseable__")
        ),
        "unmatched_admitted": unmatched,
        "orphan_observations": orphans,
        "duplicate_admitted_ids": sorted(
            {eid for eid in admitted_ids if admitted_ids.count(eid) > 1}
        ),
        "duplicate_observation_ids": sorted(
            {eid for eid in observed_ids if observed_ids.count(eid) > 1}
        ),
        "processes": sorted(
            {
                str(r.get("process_id", ""))
                for r in cadence + observations
                if r.get("process_id")
            }
        ),
        "workstreams": sorted(
            {
                str(r.get("workstream", ""))
                for r in cadence + observations
                if r.get("workstream")
            }
        ),
    }
