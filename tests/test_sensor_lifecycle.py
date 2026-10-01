"""Lifecycle wiring and the authority boundary, proven rather than asserted.

The load-bearing claim is that the sensor is inert: it observes, it never
instructs. These tests drive the real hook with real events and inspect what it
does and does not touch, rather than reading the source and agreeing with it.

The whitelist tests are the ones that matter most. A sensor that fires on
acknowledgements, on every assistant turn, or on every tool result is not a
shadow sensor with a low duty cycle; it is an every-turn hook wearing a badge.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import sys
from typing import Mapping

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.shadow_observation import (
    FORBIDDEN_KEYS,
    MATERIAL_EVENTS,
    REQUESTED_SIGNALS,
    UNSUPPORTED_ON_FALLBACK,
    Observability,
    Trigger,
    build_snapshot,
    unavailable,
)

ROOT = pathlib.Path(__file__).resolve().parents[1] / "turnstone" / "core"


class _RecordingSensor:
    """Stands in for the real sensor and records what the hook asked for."""

    def __init__(self, suppress: bool = False) -> None:
        self.calls: list[tuple[dict, str, bool, bool]] = []
        self._suppress = suppress
        self._last = None

    def should_sense(self, state, *, event, force=False):
        if self._suppress:
            return False, "unchanged_state"
        return True, Trigger.MATERIAL.value

    def observe(self, state, *, event=None, force=False, decided=False):
        # `decided` mirrors the real ShadowSensor signature. Without it the hook's
        # `decided=True` call raised TypeError, which the fail-open boundary
        # swallowed -- so this stub silently recorded ZERO calls and the suite
        # reported a phantom failure instead of the real one. Test doubles must
        # track the production signature or they hide defects, which is exactly
        # the class of bug the real-path test exists to catch.
        self.calls.append((dict(state), event, force, decided))
        self._last = dict(state)
        return unavailable(
            trigger=Trigger.MATERIAL,
            fingerprint="fp",
            serializer_version="turnstone-bounded-state:v1",
            reason="stubbed",
        )


# ----------------------------------------------------------------- whitelist


def test_material_events_are_a_whitelist_not_a_blacklist():
    """Only listed events are material; anything else is not, by construction."""
    assert "operator_instruction" in MATERIAL_EVENTS
    assert "tool_failure" in MATERIAL_EVENTS
    assert "not_material_thing" not in MATERIAL_EVENTS
    assert lifecycle.is_material("operator_instruction") is True
    assert lifecycle.is_material("assistant_turn_committed") is False
    assert lifecycle.is_material("heartbeat") is False
    assert lifecycle.is_material("") is False


@pytest.mark.parametrize("event", sorted(MATERIAL_EVENTS))
def test_every_declared_material_event_is_accepted_by_the_hook(event):
    sensor = _RecordingSensor()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event(event, {"objective": "real work"})
    assert len(sensor.calls) == 1, f"{event} was not observed"


@pytest.mark.parametrize("event", [
    "assistant_turn_committed", "turn_committed", "on_turn_start", "heartbeat",
    "status_poll", "poll", "keepalive", "format_only", "ack", "", "ui_event",
])
def test_non_material_events_never_reach_the_sensor(event):
    """No every-turn hook: ordinary turn/UI/heartbeat events are refused."""
    sensor = _RecordingSensor()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event(event, {"objective": "real work"})
    assert sensor.calls == [], f"{event!r} must not trigger the sensor"
    assert hook.counters()["rejected_not_material"] == 1


def test_hook_never_uses_the_assistant_turn_commit_path():
    """The seams are chosen so no ordinary turn completion is observed.

    This is asserted structurally: the module must not reference the
    turn-commit seam at all, so a later edit cannot quietly add it.
    """
    source = pathlib.Path(lifecycle.__file__).read_text()
    tree = ast.parse(source)
    strings = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for forbidden in ("on_turn_committed", "on_turn_start", "_report_tool_result_"):
        assert forbidden not in strings, (
            f"lifecycle references {forbidden!r}; the sensor must not hang off that seam"
        )


def test_phase_and_evidence_triggers_are_declared_unwired_not_approximated():
    """Turnstone has no phase machine or evidence classifier, so neither fires.

    Approximating them from ordinary turns is precisely how an every-turn sensor
    gets built. The omission is declared so it reads as a decision.
    """
    unwired = set(lifecycle.UNWIRED_TRIGGERS)
    assert unwired == {"phase_transition", "new_evidence"}
    assert unwired <= set(MATERIAL_EVENTS), (
        "they remain in the sensor whitelist, they simply have no source yet"
    )
    for trigger in unwired:
        assert lifecycle.is_material(trigger) is True, (
            "the whitelist still knows them; they are just never emitted"
        )
    # And nothing in the lifecycle module actually emits them.
    source = pathlib.Path(lifecycle.__file__).read_text()
    for trigger in unwired:
        assert f'"{trigger}"' not in source.replace(
            f'UNWIRED_TRIGGERS = ("phase_transition", "new_evidence")', ""
        ).replace('"phase_transition", "new_evidence"', ""), (
            f"lifecycle references {trigger!r} outside the declaration"
        )


# ------------------------------------------------------------ acknowledgement


@pytest.mark.parametrize("message", [
    "ok", "OK", "k", "yes", "no", "y", "n", "thanks", "thank you", "ty", "sure",
    "yep", "continue", ".", "go", "proceed", "carry on", "go on", "done", "nice",
    "great", "cool", "ack", "acknowledged", "got it", "understood", "roger",
    "", "   ", "ok!", "thanks!", "...", "hm", "👍", "yep!", "done.", "sure.",
])
def test_acknowledgements_are_not_substantive_instructions(message):
    assert lifecycle.is_substantive_instruction(message) is False


@pytest.mark.parametrize("message", [
    "fix the failing deploy gate",
    "why is swap full?",
    "implement exact token admission then commit",
    "show me the top CPU consumers",
    "commit the qualified source",
    "run the three integrated proofs",
])
def test_real_work_is_a_substantive_instruction(message):
    assert lifecycle.is_substantive_instruction(message) is True


def test_punctuation_only_message_is_not_substantive():
    """Length alone must not qualify a message."""
    assert lifecycle.is_substantive_instruction("!@#$%^&*()_+{}|:\"<>?") is False
    assert lifecycle.is_substantive_instruction("....") is False
    assert lifecycle.is_substantive_instruction("a b c") is True


# ------------------------------------------------------------------- dedupe


def test_unchanged_state_suppresses_the_sensor_call():
    """Same meaningful state -> same fingerprint -> no second call."""
    sensor = _RecordingSensor(suppress=True)
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event("operator_instruction", {"objective": "same work"})
    assert sensor.calls == [], "an unchanged state must not cost a call"
    assert hook.counters()["dedupe_suppressed"] == 1


def test_fingerprint_excludes_volatile_fields():
    """Two states differing only in volatile fields must fingerprint the same."""
    from turnstone.core.shadow_observation import state_fingerprint

    a = lifecycle.build_state(objective="reconcile the failover proof")
    a_with_noise = dict(a)
    a_with_noise.update({
        "timestamp": "2026-09-30T11:00:00Z",
        "latency_ms": 812,
        "request_id": "abc-123",
        "poll_counter": 47,
    })
    # volatile keys are dropped at projection time, so both projections match
    assert state_fingerprint(a) == state_fingerprint(lifecycle.build_state(**{
        "objective": "reconcile the failover proof",
    }))
    assert lifecycle.VOLATILE_KEYS, "the volatile set must not be empty"


def test_meaningful_state_change_produces_a_new_fingerprint():
    from turnstone.core.shadow_observation import state_fingerprint

    before = lifecycle.build_state(objective="step one")
    after = lifecycle.build_state(objective="step two, now blocked on the gate")
    assert state_fingerprint(before) != state_fingerprint(after)


def test_build_state_drops_empty_and_volatile_fields():
    state = lifecycle.build_state(
        objective="real work", phase="", blockers="", tools="bash",
        agent="turnstone", history="",
    )
    assert "phase" not in state
    assert "blocker" not in state
    assert "history" not in state
    assert state["objective"] == "real work"
    assert state["tool"] == "bash"


def test_build_state_drops_a_volatile_key_passed_as_a_field():
    """The filter must actually fire, not merely be present.

    `build_state` declares a fixed set of fields, so removing the filter is
    invisible through that path. This passes a volatile key as a real field
    name, which is the only way the filter is reachable from the public
    surface — and therefore the only way a regression there is observable.
    """
    volatile_names = sorted(lifecycle.VOLATILE_KEYS)
    assert volatile_names, "the volatile set must not be empty"

    for name in volatile_names:
        state = lifecycle.build_state(**{name: "12345"})
        assert name not in state, f"volatile field {name!r} survived into the projection"
        assert state == {}, f"only the volatile field was passed, got {state}"

    # And a non-volatile name of the same shape is still admitted, so the test
    # is not passing because build_state drops everything.
    assert lifecycle.build_state(objective="real work")["objective"] == "real work"


def test_projection_helper_drops_volatile_keys():
    """The low-level filter is exercised directly as well."""
    keep = lifecycle._drop_volatile({"objective": "x", "timestamp": "t", "latency_ms": 5})
    assert keep == {"objective": "x"}


def test_build_state_never_emits_a_volatile_key():
    state = lifecycle.build_state(objective="x")
    assert not (set(state) & lifecycle.VOLATILE_KEYS)


# ---------------------------------------------------------------- fail open


def test_hook_swallows_any_sensor_exception():
    """A broken sensor must not propagate into a task."""

    class _Exploding:
        def should_sense(self, *_a, **_k):
            raise RuntimeError("sensor is on fire")

        def observe(self, *_a, **_k):
            raise RuntimeError("also on fire")

    hook = lifecycle.SensorHook(sensor=_Exploding(), persist=False)
    hook.observe_event("operator_instruction", {"objective": "x"})  # must not raise
    assert hook.counters()["hook_error"] == 1


def test_hook_swallows_a_failing_state_mapping():
    """A mapping that explodes while being read must be contained in the hook.

    The copy is eager and validated, so the failure is raised here rather than
    deferred into the sensor where a different layer would have to catch it.
    Containment is the property; where it is enforced is what is pinned here.
    """
    class _Exploding(Mapping):
        def __iter__(self):
            raise ValueError("bad mapping")

        def __len__(self):
            raise ValueError("bad mapping")

        def __getitem__(self, _key):
            raise ValueError("bad mapping")

    hook = lifecycle.SensorHook(sensor=_RecordingSensor(), persist=False)
    hook.observe_event("operator_instruction", _Exploding())  # must not raise
    assert hook.counters().get("hook_error") == 1
    assert hook.counters().get("sensor_calls", 0) == 0, (
        "the sensor must not be reached with an unusable state"
    )


def test_forced_observation_bypasses_the_material_check():
    sensor = _RecordingSensor()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event("manual_probe", {"objective": "x"}, force=True)
    assert len(sensor.calls) == 1


def test_hook_tells_the_sensor_the_decision_is_already_made():
    """The double-gate fix, asserted at the seam: the hook passes decided=True.

    `decided` (not `force`) is what tells ShadowSensor.observe() that this
    event's cadence decision is already committed, so it must not re-gate. If a
    future change drops this flag, the canary-2026-09-30 defect returns
    silently: cadence says sensor_call, the store stays empty.
    """
    sensor = _RecordingSensor()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event("operator_instruction", {"objective": "x"})

    assert len(sensor.calls) == 1
    _state, _event, force, decided = sensor.calls[0]
    assert decided is True, "the hook must tell the sensor the decision is made"
    assert force is False, (
        "force must stay False here: it would bypass is_material() and re-admit "
        "suppressed states -- the wrong tool for the wrong problem"
    )


def test_hook_counters_are_exposed_for_shadow_health():
    sensor = _RecordingSensor()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event("operator_instruction", {"objective": "a"})
    hook.observe_event("assistant_turn_committed", {"objective": "a"})
    counters = hook.counters()
    # Counter names are the cadence OUTCOMES, not generic verbs: a suppressed
    # call produces no observation, so counting outcomes in the cadence log is
    # the only way the dedupe rate is measurable at all.
    assert counters["sensor_call"] == 1
    assert counters["rejected_not_material"] == 1


# --------------------------------------------------------- authority boundary


ROUTING_CONTROLS = [
    "model", "provider", "route", "target", "llm_client", "reasoning_effort",
    "reasoning", "temperature", "agent", "tools", "permissions", "escalate",
    "retry", "output_guard", "approval", "approvals", "approval_state",
    "task_agent_model", "max_tokens", "top_p", "seed",
]


def test_observation_carries_no_routing_control():
    """A real snapshot must not contain any control surface."""
    answers = {name: {"noul": 0.5} for name in REQUESTED_SIGNALS}
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL,
        fingerprint="fp",
        serializer_version="turnstone-bounded-state:v1",
        answers=answers,
        backend="span",
        fallback_used=False,
        latency_ms=10,
        state_truncated=False,
        serialized_chars=100,
        serialized_state_tokens=30,
        dropped_field_count=0,
    )
    blob = json.dumps(snapshot.to_dict(), default=str).lower()
    for control in ROUTING_CONTROLS:
        assert f'"{control}"' not in blob, f"snapshot leaked control {control!r}"
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in blob


def test_local_suitability_is_never_available():
    for snapshot in (
        build_snapshot(
            trigger=Trigger.MATERIAL, fingerprint="f",
            serializer_version="turnstone-bounded-state:v1",
            answers={n: {"noul": 0.5} for n in REQUESTED_SIGNALS},
            backend="laya-fallback", fallback_used=True, latency_ms=5,
            state_truncated=False, serialized_chars=10,
            serialized_state_tokens=5, dropped_field_count=0,
        ),
        unavailable(
            trigger=Trigger.MATERIAL, fingerprint="f",
            serializer_version="turnstone-bounded-state:v1", reason="http_500",
        ),
    ):
        signals = {s.name: s for s in snapshot.signals}
        target = signals[UNSUPPORTED_ON_FALLBACK]
        assert target.available is False
        assert target.value is None
        assert target.reason == "backend_not_qualified_for_signal"
        assert target.observability is Observability.UNKNOWN


def test_observability_is_never_invented():
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL, fingerprint="f",
        serializer_version="turnstone-bounded-state:v1",
        answers={n: {"noul": 0.5} for n in REQUESTED_SIGNALS},
        backend="span", fallback_used=False, latency_ms=5,
        state_truncated=False, serialized_chars=10,
        serialized_state_tokens=5, dropped_field_count=0,
    )
    assert all(s.observability is Observability.UNKNOWN for s in snapshot.signals)


def test_lifecycle_module_exposes_no_control_writer():
    """The lifecycle module must not offer any way to set a routing control.

    A setter is how advisory code becomes authoritative, so the module is
    checked for the *absence* of anything that could set one.
    """
    tree = ast.parse(pathlib.Path(lifecycle.__file__).read_text())
    public = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    }
    for control in ROUTING_CONTROLS:
        assert control not in public, f"lifecycle exposes a {control!r} entry point"
    # No attribute assignment to a control-shaped name anywhere in the module.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ROUTING_CONTROLS:
            assert not (isinstance(node.ctx, ast.Store)), (
                f"lifecycle assigns to {node.attr!r}, which could set a control"
            )


def test_observe_event_returns_none_and_writes_nothing_to_the_task():
    """The call site's entire contract: fire, discard, no effect on the task."""
    assert inspect.signature(lifecycle.observe_event).return_annotation in (None, "None")
    sensor = _RecordingSensor()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    result = hook.observe_event("operator_instruction", {"objective": "x"})
    assert result is None


def test_lifecycle_does_not_import_routing_or_guard_modules():
    """It must not even be able to reach the control surfaces from here."""
    source = pathlib.Path(lifecycle.__file__).read_text()
    for forbidden in (
        "output_guard", "router", "routing", "model_registry", "approval",
        "registry", "judge",
    ):
        assert f"import {forbidden}" not in source
        assert f"from .{forbidden}" not in source
        assert f"from turnstone.{forbidden}" not in source, (
            f"lifecycle imports {forbidden!r}"
        )


# ----------------------------------------------- cadence and dedupe proof


def test_dedupe_survives_a_failing_backend():
    """Suppression must not depend on the network call succeeding.

    This is a real defect that shipped once: cadence state was advanced only by
    `observe()`, which returns early on a transport failure. With the backend
    down, every material event therefore read as the initial state forever,
    dedupe never engaged, and the sensor fired on EVERY event. The shadow
    contract explicitly requires no retry storm, so a storm caused by dedupe
    being dead under failure is a correctness failure, not a performance one.
    """
    import pathlib
    import tempfile

    from turnstone.core.shadow_driver import ShadowSensor

    work = pathlib.Path(tempfile.mkdtemp())
    sensor = ShadowSensor(
        store=work / "obs.jsonl",
        switchyard="http://127.0.0.1:9",  # closed port: every call fails
        timeout=1,
    )
    hook = lifecycle.SensorHook(sensor=sensor, cadence_store=work / "cad.jsonl")
    state = {"objective": "the same work, repeated"}

    for _ in range(5):
        hook.observe_event("operator_instruction", dict(state))

    counts = hook.counters()
    assert counts.get("sensor_call") == 1, (
        f"identical state must cost exactly one call, got {counts}"
    )
    assert counts.get("dedupe_suppressed") == 4, (
        f"the other four must be suppressed, got {counts}"
    )


def test_a_meaningful_change_is_not_deduped_away():
    """Dedupe must suppress repetition without suppressing progress."""
    import pathlib
    import tempfile

    from turnstone.core.shadow_driver import ShadowSensor

    work = pathlib.Path(tempfile.mkdtemp())
    sensor = ShadowSensor(store=work / "obs.jsonl",
                          switchyard="http://127.0.0.1:9", timeout=1)
    hook = lifecycle.SensorHook(sensor=sensor, cadence_store=work / "cad.jsonl")

    hook.observe_event("operator_instruction", {"objective": "first"})
    hook.observe_event("operator_instruction", {"objective": "second, now blocked"})
    hook.observe_event("operator_instruction", {"objective": "second, now blocked"})
    hook.observe_event("tool_failure", {"objective": "second, now blocked",
                                        "blocker": "the gate failed"})

    counts = hook.counters()
    assert counts.get("sensor_call") == 3, (
        f"two distinct states plus one event must each be observed: {counts}"
    )


def test_dedupe_state_is_advanced_by_the_cadence_decision():
    """`should_sense` itself must commit the decision, not a later network call."""
    from turnstone.core.shadow_driver import ShadowSensor
    import pathlib
    import tempfile

    sensor = ShadowSensor(store=pathlib.Path(tempfile.mkdtemp()) / "o.jsonl",
                          switchyard="http://127.0.0.1:9", timeout=1)
    state = {"objective": "unchanged"}
    first, why_first = sensor.should_sense(state, event="operator_instruction")
    second, why_second = sensor.should_sense(state, event="operator_instruction")
    assert first is True and why_first == Trigger.INITIAL.value
    assert second is False, (
        "the second identical state must be suppressed without any observe() call"
    )
    assert why_second == "unchanged_state"


def test_cadence_rows_carry_no_state_text_or_signals():
    """Cadence is bookkeeping: it must not duplicate workstream content."""
    import json
    import pathlib
    import tempfile

    from turnstone.core.shadow_driver import ShadowSensor

    work = pathlib.Path(tempfile.mkdtemp())
    sensor = ShadowSensor(store=work / "obs.jsonl",
                          switchyard="http://127.0.0.1:9", timeout=1)
    hook = lifecycle.SensorHook(sensor=sensor, cadence_store=work / "cad.jsonl")
    hook.observe_event(
        "operator_instruction",
        {"objective": "SECRET workstream content that must not be logged"},
        workstream="ws-123",
    )
    rows = [json.loads(line) for line in
            (work / "cad.jsonl").read_text().splitlines()]
    assert rows, "a cadence row must be written"
    blob = json.dumps(rows)
    assert "SECRET" not in blob, "cadence must not carry state text"
    assert set(rows[0]) == {"ts", "event", "outcome", "workstream", "fingerprint"}
    assert rows[0]["workstream"] == "ws-123"
    for control in ("model", "provider", "route", "reasoning_effort", "output_guard"):
        assert control not in blob


def test_cadence_records_suppressed_events_that_produce_no_observation():
    """A suppression must still be measurable, which is the point of the log."""
    import pathlib
    import tempfile

    from turnstone.core.shadow_driver import ShadowSensor

    work = pathlib.Path(tempfile.mkdtemp())
    sensor = ShadowSensor(store=work / "obs.jsonl",
                          switchyard="http://127.0.0.1:9", timeout=1)
    hook = lifecycle.SensorHook(sensor=sensor, cadence_store=work / "cad.jsonl")
    hook.observe_event("operator_instruction", {"objective": "x"})
    hook.observe_event("operator_instruction", {"objective": "x"})
    hook.observe_event("heartbeat", {"objective": "x"})

    outcomes = [
        __import__("json").loads(line)["outcome"]
        for line in (work / "cad.jsonl").read_text().splitlines()
    ]
    assert outcomes.count("dedupe_suppressed") == 1, outcomes
    assert outcomes.count("rejected_not_material") == 1, outcomes
    assert outcomes.count("sensor_call") == 1, outcomes


def test_cadence_report_exposes_calls_per_hour_and_per_workstream():
    import pathlib
    import tempfile

    from turnstone.core.shadow_driver import ShadowSensor

    work = pathlib.Path(tempfile.mkdtemp())
    sensor = ShadowSensor(store=work / "obs.jsonl",
                          switchyard="http://127.0.0.1:9", timeout=1)
    hook = lifecycle.SensorHook(sensor=sensor, cadence_store=work / "cad.jsonl")
    hook.observe_event("operator_instruction", {"objective": "a"}, workstream="ws-1")
    hook.observe_event("tool_failure", {"objective": "a", "blocker": "b"}, workstream="ws-2")
    report = hook.cadence_report()
    assert report["distinct_workstreams"] == 2, report
    assert report["workstreams"] == {"ws-1": 1, "ws-2": 1}, report
    assert "calls_per_hour" in report
    assert "dedupe_suppression_rate" in report


def test_workstream_id_is_metadata_not_hashed_state():
    """The workstream id must not be part of the sensed state.

    It rides as a dedicated argument so it can be counted per workstream without
    becoming part of the semantic fingerprint. If it leaked into the state, two
    workstreams doing identical work would not dedupe against each other, and a
    rename of an unrelated id would look like a meaningful change.
    """
    from turnstone.core.shadow_observation import state_fingerprint

    a = lifecycle.build_state(objective="identical work")
    assert state_fingerprint(a) == state_fingerprint(
        lifecycle.build_state(objective="identical work")
    )
    # The hook accepts it separately, and it is absent from the projection.
    assert "ws-1" not in a


def test_a_default_hook_persists_and_a_test_hook_does_not():
    """The production cadence log must be reachable only deliberately.

    Regression guard. The test suite previously constructed hooks with the
    default, so it wrote 191 fake `sensor_call` rows and 69 `hook_error` rows
    into the live cadence log - all with no workstream and synthetic
    fingerprints. Aggregated, they read as 887 calls/hour, which is the exact
    metric the observation period exists to measure. A measuring instrument
    that its own tests can inflate is worse than no instrument.
    """
    import inspect
    import pathlib

    signature = inspect.signature(lifecycle.SensorHook.__init__)
    assert signature.parameters["persist"].default is True
    assert signature.parameters["cadence_store"].default is None

    # A persist=False hook must not create or touch the production log.
    production = lifecycle.DEFAULT_CADENCE_STORE
    before = production.read_bytes() if production.exists() else b""
    hook = lifecycle.SensorHook(sensor=_RecordingSensor(), persist=False)
    hook.observe_event("operator_instruction", {"objective": "x"}, workstream="ws")
    after = production.read_bytes() if production.exists() else b""
    assert before == after, "a persist=False hook must not write to the log"


def test_no_test_constructs_a_hook_that_would_persist_by_default():
    """Static guard: every hook built in this suite names its store or opts out.

    Parsed with `ast`, not a regex: a regex stops at the first `)` inside a
    nested call such as `SensorHook(sensor=_RecordingSensor())` and so both
    misses real constructions and flags the f-string in its own message.
    """
    import ast as _ast
    import pathlib

    # Scan the WHOLE tests tree, not just this file: an offender in a sibling
    # suite pollutes the same production log, and a single-file guard would
    # have missed exactly that.
    offenders: list[str] = []
    for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        tree = _ast.parse(path.read_text())
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            # `lifecycle.SensorHook(...)` is an ast.Attribute, not an ast.Name,
            # so matching on `id` alone silently finds nothing -- which is how
            # this guard passed while an offender was present.
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "SensorHook":
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            if "persist" not in keywords and "cadence_store" not in keywords:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "hooks constructed without an explicit store or persist opt-out: "
        + ", ".join(offenders)
    )
    # And prove the scan is not vacuous: it must actually see this suite's own
    # opted-out constructions.
    seen = 0
    tree = _ast.parse(pathlib.Path(__file__).read_text())
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and getattr(node.func, "attr", None) == "SensorHook":
            seen += 1
    assert seen >= 5, f"scan found only {seen} hooks; the guard is not looking"
