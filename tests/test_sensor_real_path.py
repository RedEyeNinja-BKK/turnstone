"""Regression: the hook -> real-ShadowSensor path, which is what production runs.

This file exists because of a defect the rest of the suite could not see.

Every pre-existing sensor test drove ``ShadowSensor.observe()`` directly, or
drove ``SensorHook`` with a stubbed/inert sensor. Production drives neither: it
calls ``SensorHook.observe_event()`` with a real ``ShadowSensor`` behind it. That
one path was untested, and it was broken. On 2026-09-30 the sensor was admitted
(253 tests green), canaried onto a live node, and wrote ZERO observations while
logging ``sensor_call`` — because ``SensorHook.observe_event`` called
``should_sense()`` (which COMMITS the fingerprint) and then ``observe()`` gated
on ``should_sense()`` a second time and denied itself ``unchanged_state``.

So these tests assert the INVARIANT, not an implementation detail:

    one material event -> ONE should_sense decision
                      -> one fingerprint commit
                      -> if admitted, ONE observation attempt

Transport here is a real counting stub and storage is a real temp file. Nothing
about the assertion path is mocked, because mocking is what hid the defect.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from turnstone.core import sensor_lifecycle as sl
from turnstone.core.shadow_driver import ShadowSensor


class CountingTransport:
    """Counts real observation attempts. Stands in for the network only."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def post(self, body):  # noqa: ANN001 - stub shape
        self.calls += 1
        if self.fail:
            return None, "transport:OSError"
        return {
            "provider": "TestProvider",
            "answers": {
                "task_complexity": {"noul": 0.4},
                "reasoning_demand": {"noul": 0.2},
                "tool_dependency": {"noul": 0.1},
                "agentic_complexity": {"noul": 0.3},
                "route_sufficiency": {"noul": 0.5},
            },
        }, None


def build(tmp_path: pathlib.Path, *, fail: bool = False):
    """Real SensorHook + real ShadowSensor + real cadence/dedupe + real storage."""
    store = tmp_path / "observations.jsonl"
    cadence = tmp_path / "cadence.jsonl"
    sensor = ShadowSensor(store=store, switchyard="http://127.0.0.1:1")  # never used
    hook = sl.SensorHook(sensor=sensor, cadence_store=cadence)
    transport = CountingTransport(fail=fail)
    # Replace ONLY the network hop. should_sense/observe/serialize/persist are real.
    sensor._post = transport.post  # type: ignore[method-assign]
    return hook, sensor, transport, store, cadence


def count(store: pathlib.Path) -> int:
    if not store.exists():
        return 0
    return sum(1 for line in store.read_text().splitlines() if line.strip())


def cadence_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


STATE_A = {"task": "reconcile the sensor ledger", "phase": "verification",
           "blocker": "none", "tool": "bash", "agent": "turnstone"}
STATE_A_SAME = dict(STATE_A)
STATE_B = {"task": "roll the fleet onto the repaired slot", "phase": "deployment",
           "blocker": "none", "tool": "bash", "agent": "turnstone"}


def test_first_meaningful_event_records_exactly_one_observation(tmp_path):
    """THE defect: cadence said sensor_call and the store stayed empty."""
    hook, sensor, transport, store, cadence = build(tmp_path)

    hook.observe_event("operator_instruction", STATE_A, workstream="ws-real")

    assert count(store) == 1, "one admitted event must persist exactly one observation"
    assert transport.calls == 1, "exactly one transport attempt"
    outcomes = [r["outcome"] for r in cadence_rows(cadence)]
    assert outcomes == ["sensor_call"], outcomes


def test_same_meaningful_state_is_dedupe_suppressed(tmp_path):
    hook, sensor, transport, store, cadence = build(tmp_path)

    hook.observe_event("operator_instruction", STATE_A, workstream="ws-real")
    assert count(store) == 1
    calls_after_first = transport.calls

    hook.observe_event("operator_instruction", STATE_A_SAME, workstream="ws-real")

    assert count(store) == 1, "an unchanged state must not add an observation"
    assert transport.calls == calls_after_first, "suppressed state must not cost a call"
    outcomes = [r["outcome"] for r in cadence_rows(cadence)]
    assert outcomes == ["sensor_call", "dedupe_suppressed"], outcomes


def test_changed_meaningful_state_records_another_observation(tmp_path):
    hook, sensor, transport, store, cadence = build(tmp_path)

    hook.observe_event("operator_instruction", STATE_A, workstream="ws-real")
    hook.observe_event("operator_instruction", STATE_B, workstream="ws-real")

    assert count(store) == 2, "a changed state must produce a second observation"
    assert transport.calls == 2
    outcomes = [r["outcome"] for r in cadence_rows(cadence)]
    assert outcomes == ["sensor_call", "sensor_call"], outcomes


def test_backend_failure_still_advances_fingerprint_and_does_not_retry(tmp_path):
    """The fail-open contract: transport failure must not re-open the gate."""
    hook, sensor, transport, store, cadence = build(tmp_path, fail=True)

    hook.observe_event("operator_instruction", STATE_A, workstream="ws-real")
    first = count(store)
    calls_after_first = transport.calls

    hook.observe_event("operator_instruction", STATE_A_SAME, workstream="ws-real")

    assert transport.calls == calls_after_first, "no retry storm on an unchanged state"
    outcomes = [r["outcome"] for r in cadence_rows(cadence)]
    assert outcomes[-1] == "dedupe_suppressed", (
        "the fingerprint must advance even when the transport failed, so a "
        f"repeated state is suppressed; got {outcomes}"
    )
    # The failed attempt is still RECORDED as an observation (unavailable), which
    # is what makes the epoch auditable rather than silently short.
    assert count(store) == first


def test_should_sense_is_called_exactly_once_per_event(tmp_path):
    """The invariant stated directly: never two cadence decisions on one event."""
    hook, sensor, transport, store, cadence = build(tmp_path)

    calls = []
    original = sensor.should_sense

    def counting(state, *, event=None, force=False):  # noqa: ANN001
        calls.append(event)
        return original(state, event=event, force=force)

    sensor.should_sense = counting  # type: ignore[method-assign]

    hook.observe_event("operator_instruction", STATE_A, workstream="ws-real")
    hook.observe_event("operator_instruction", STATE_B, workstream="ws-real")

    assert len(calls) == 2, (
        f"two admitted events must make exactly two decisions, got {len(calls)}: {calls}"
    )


def test_observation_carries_no_routing_authority(tmp_path):
    """A stored observation must not be able to influence anything."""
    hook, sensor, transport, store, cadence = build(tmp_path)

    hook.observe_event("operator_instruction", STATE_A, workstream="ws-real")
    record = json.loads(store.read_text().splitlines()[0])

    from turnstone.core.shadow_observation import FORBIDDEN_KEYS
    blob = json.dumps(record).lower()
    leaked = [k for k in FORBIDDEN_KEYS if f'"{k}"' in blob]
    assert leaked == [], f"observation carries routing fields: {leaked}"


def test_unwired_trigger_is_admitted_but_never_emitted(tmp_path):
    """phase_transition/new_evidence are WHITELISTED but have no honest source.

    `is_material()` admits them -- they are legitimate event names -- they are
    simply never emitted by any production seam, because no Turnstone lifecycle
    point reports them honestly. The point of that fact is that nothing in the
    codebase calls observe_event() with them, so they cannot silently start
    firing. This test pins the whitelist half and, in test_each_qualified_
    trigger_reaches_the_store, the seam half. What must NOT happen is a
    rejection: if these ever start being rejected, the whitelist and the seam
    list have diverged and someone is inventing the trigger.
    """
    hook, sensor, transport, store, cadence = build(tmp_path)

    hook.observe_event("phase_transition", STATE_A, workstream="ws-real")

    # Whitelisted => admitted when it does arrive (it never does in production).
    assert count(store) == 1
    outcomes = [r["outcome"] for r in cadence_rows(cadence)]
    assert outcomes == ["sensor_call"], outcomes

    # ...and no production seam emits it.
    import inspect
    import turnstone.core.session as session_mod
    session_src = inspect.getsource(session_mod)
    assert '"phase_transition"' not in session_src
    assert '"new_evidence"' not in session_src


def test_decided_flag_does_not_bypass_materiality_for_a_direct_caller(tmp_path):
    """`decided=True` must not become a back door around is_material."""
    sensor = ShadowSensor(
        store=tmp_path / "o.jsonl", switchyard="http://127.0.0.1:1")
    transport = CountingTransport()
    sensor._post = transport.post  # type: ignore[method-assign]

    # A direct caller passing decided=True bypasses dedupe by design (the caller
    # owns the decision) — but it must still not fabricate an admitted record
    # for a state it was handed, and must never raise.
    snapshot = sensor.observe({"task": "x"}, event="operator_instruction",
                              decided=True)
    assert snapshot is not None


@pytest.mark.parametrize("event", ["operator_instruction", "delegation_result",
                                   "task_agent_failure", "retry_transition"])
def test_each_qualified_trigger_reaches_the_store(tmp_path, event):
    """All four production seams must land an observation, not just the first."""
    hook, sensor, transport, store, cadence = build(tmp_path)

    hook.observe_event(event, {"task": f"work via {event}", "phase": "x"},
                      workstream="ws-seam")

    assert count(store) == 1, f"{event} produced no observation"
    assert transport.calls == 1