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
import pathlib
import threading
import time
from typing import Any, Mapping

from .shadow_driver import ShadowSensor
from .shadow_observation import is_material, state_fingerprint

#: Cadence rows go here, deliberately separate from the observation store. A
#: suppressed event writes no observation, so counting the calls that did NOT
#: happen is only possible in its own log.
DEFAULT_CADENCE_STORE = pathlib.Path(
    "/home/vincent/shared-workspace/operations/switchyard-sensor-layer-20260929/"
    "shadow-cadence.jsonl"
)

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
        text = _clean(value)
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
    "continue", "go", "proceed", "carry on", "go on", "keep going", "continue",
    "next", "resume", "carryon",
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
                }
            if self._cadence_store is None:
                return
            self._cadence_store.parent.mkdir(parents=True, exist_ok=True)
            with self._cadence_store.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
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
        try:
            with self._lock:
                fingerprint = state_fingerprint(state) if state else ""
            if not force and not is_material(event):
                self._record_cadence(event, "rejected_not_material", workstream, fingerprint)
                return
            # Eager, validated copy. `dict(state)` alone can defer an exploding
            # mapping into the sensor, where a different layer would have to
            # catch it; materialising here keeps containment in this function.
            projection = {str(name): value for name, value in dict(state or {}).items()}
            allowed, _why = self._sensor.should_sense(projection, event=event, force=force)
            if not allowed:
                self._record_cadence(event, "dedupe_suppressed", workstream, fingerprint)
                return
            self._record_cadence(event, "sensor_call", workstream, fingerprint)
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
            self._sensor.observe(projection, event=event, decided=True)
        except Exception:  # noqa: BLE001 - a sensor must never fail a task
            try:
                self._record_cadence(event, "hook_error", workstream, "")
            except Exception:  # noqa: BLE001
                pass


#: Process-wide hook. Created on first use so importing this module has no side
#: effect on a process that never senses.
_HOOK: SensorHook | None = None
_HOOK_LOCK = threading.Lock()


def hook() -> SensorHook:
    global _HOOK
    if _HOOK is None:
        with _HOOK_LOCK:
            if _HOOK is None:
                _HOOK = SensorHook()
    return _HOOK


def observe_event(
    event: str,
    state: Mapping[str, Any] | None = None,
    *,
    force: bool = False,
    workstream: str = "",
) -> None:
    """Module-level convenience wrapper around :func:`hook`."""
    hook().observe_event(event, state, force=force, workstream=workstream)
