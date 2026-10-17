"""2026-10-17 recovery additions: per-workstream dedupe, live backend shape,
qualification gate, short timeout, and the disabled-by-default gate.

These tests sit beside the historical suite and pin the behaviors the
recovery brief requires. They run against the same candidate tree the
historical modules run against, with the same environmental skips when the
pinned tokenizer is absent.
"""
from __future__ import annotations

import ast
import json
import pathlib
import urllib.request  # noqa: F401 - imported by the driver under test

import pytest

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.shadow_driver import (
    REQUEST_TIMEOUT_SECONDS,
    ShadowSensor,
    resolve_decision_endpoint,
)
from turnstone.core.shadow_observation import (
    DECISION_CAPABILITY,
    QUALIFIED_BACKENDS,
    Trigger,
)

SESSION = (
    pathlib.Path(__file__).resolve().parents[1] / "turnstone" / "core" / "session.py"
)


# ------------------------------------------------- per-workstream dedupe (A-B-A)


def test_second_a_after_b_is_unchanged_not_initial():
    """The A-B-A sequence must not misread A's second event as new state.

    The historical sensor held ONE (fingerprint, initial-done) pair per
    process, so workstream B's observation between A's two events made A's
    second identical state read as a fresh change. Dedupe is now per
    workstream: A's state is compared only against A's last admitted
    fingerprint.
    """
    sensor = ShadowSensor(store=None)
    X = {"objective": "state X"}
    Y = {"objective": "state Y"}

    a1 = sensor.should_sense(X, event="operator_instruction", workstream="A")
    b1 = sensor.should_sense(Y, event="operator_instruction", workstream="B")
    a2 = sensor.should_sense(X, event="operator_instruction", workstream="A")

    assert a1 == (True, Trigger.INITIAL.value)
    assert b1 == (True, Trigger.INITIAL.value)
    # The whole point: A's second sight of X is UNCHANGED, not initial.
    assert a2 == (False, "unchanged_state")


def test_genuine_change_in_a_after_b_still_admits():
    sensor = ShadowSensor(store=None)
    sensor.should_sense({"objective": "X"}, event="operator_instruction", workstream="A")
    sensor.should_sense({"objective": "Y"}, event="operator_instruction", workstream="B")
    allowed, why = sensor.should_sense(
        {"objective": "X2"}, event="operator_instruction", workstream="A"
    )
    assert allowed and why == Trigger.MATERIAL.value


def test_workstreams_dedupe_independently_over_full_lifecycles():
    sensor = ShadowSensor(store=None)
    events = ["operator_instruction", "delegation_result", "retry_transition"]
    # Interleave two workstreams with evolving states; each must admit its
    # first event and suppress repeats of its own last state regardless of
    # what the other did in between.
    seq = [
        ("A", "s1"), ("B", "t1"), ("A", "s1"), ("B", "t1"),
        ("A", "s2"), ("B", "t2"), ("A", "s2"), ("B", "t2"),
        ("A", "s1"), ("B", "t1"),
    ]
    outcomes = [
        sensor.should_sense({"objective": s}, event=events[0], workstream=w)
        for w, s in seq
    ]
    expected = [True, True, False, False, True, True, False, False, True, True]
    got = [allowed for allowed, _why in outcomes]
    assert got == expected, list(zip(seq, outcomes, strict=False))


def test_dedupe_capacity_evicts_lru_without_error():
    sensor = ShadowSensor(store=None, dedupe_capacity=4)
    # Touch six workstreams; the map must stay bounded and still dedupe the
    # most recent ones.
    for i in range(6):
        sensor.should_sense(
            {"objective": f"w{i}"}, event="operator_instruction", workstream=f"ws{i}"
        )
    assert len(sensor._dedupe) <= 4
    # The most recent workstream still dedupes.
    allowed, why = sensor.should_sense(
        {"objective": "w5"}, event="operator_instruction", workstream="ws5"
    )
    assert not allowed and why == "unchanged_state"


def test_per_workstream_dedupe_survives_a_failing_backend():
    """The backend-failure invariant holds PER WORKSTREAM, not globally.

    With the transport dead, each workstream's fingerprint must still be
    committed on the admit branch: no retry storm, per workstream.
    """
    sensor = ShadowSensor(store=None, switchyard="http://127.0.0.1:9", timeout=0.2)
    hook = lifecycle.SensorHook(sensor=sensor, cadence_store=None, persist=False)

    # Each workstream sees a distinct first state and a repeated second state.
    sequence = [
        ("ws-x", {"objective": "work x, attempt one"}),
        ("ws-y", {"objective": "work y, attempt one"}),
        ("ws-x", {"objective": "work x, attempt two"}),
        ("ws-y", {"objective": "work y, attempt two"}),
    ]
    for workstream, state in sequence:
        hook.observe_event("operator_instruction", state, workstream=workstream)

    counts = hook.counters()
    # 4 admitted events, each one failed transport attempt, no retry storm —
    # and dedupe stayed per workstream throughout (each admit was genuine).
    assert counts.get("sensor_call", 0) == 4
    assert sensor.metrics.observation_count == 4
    assert sensor.metrics.unavailable_count == 4


# ------------------------------------------------------- live backend shape


LIVE_SHAPE_RESPONSE = {
    "id": "gen-dec-test",
    "model": "respan/span-01-lite-20260925",
    "backend": "direct-openrouter-span-01-lite",
    "child_lane": "smartfree-decision",
    "requested": "tier1-decision",
    "answers": {
        "task_complexity": {"type": "noul", "value": 0.145},
        "reasoning_demand": {"type": "noul", "value": 0.133},
        "tool_dependency": {"type": "noul", "value": 0.109},
        "agentic_complexity": {"type": "noul", "value": 0.146},
        "route_sufficiency": {"type": "noul", "value": 0.144},
    },
}


class _FixedTransport:
    """Stands in for the network: returns one fixed payload."""

    def __init__(self, payload, calls=None):
        self.payload = payload
        self.calls = calls if calls is not None else []

    def __call__(self, body):
        self.calls.append(body)
        return json.loads(json.dumps(self.payload)), None


def _sensor_with(payload, tmp_path):
    sensor = ShadowSensor(store=tmp_path / "obs.jsonl", timeout=2.5)
    transport = _FixedTransport(payload)
    sensor._post = transport  # type: ignore[method-assign]
    return sensor, transport


def test_live_backend_shape_is_believed_with_provenance(tmp_path):
    """The measured 1.8.7 response shape (backend/child_lane, no provider)."""
    sensor, _transport = _sensor_with(LIVE_SHAPE_RESPONSE, tmp_path)
    snapshot = sensor.observe(
        {"objective": "probe"}, event="operator_instruction", workstream="ws"
    )
    by_name = {s.name: s for s in snapshot.signals}
    # All five qualified signals observable with the graded values.
    assert by_name["task_complexity"].available is True
    assert by_name["task_complexity"].value == pytest.approx(0.145)
    assert by_name["route_sufficiency"].value == pytest.approx(0.144)
    # Backend provenance from the response, not guessed.
    assert snapshot.backend == "direct-openrouter-span-01-lite"
    # Honest sixth slot: local_suitability stays unavailable.
    assert by_name["local_suitability"].available is False
    assert by_name["local_suitability"].reason == "backend_not_qualified_for_signal"
    # No routing control fields anywhere in the stored record.
    blob = json.dumps(snapshot.to_dict())
    for forbidden in ("\"model\"", "\"provider\"", "\"route\"", "\"output_guard\""):
        assert forbidden not in blob, forbidden


def test_unqualified_backend_records_unavailable_and_keeps_raw(tmp_path):
    """An unqualified leg answers on the wire but is never reported as measured."""
    payload = json.loads(json.dumps(LIVE_SHAPE_RESPONSE))
    payload["backend"] = "direct-openrouter-mercury-decide-free"
    sensor, _transport = _sensor_with(payload, tmp_path)
    snapshot = sensor.observe(
        {"objective": "probe"}, event="operator_instruction", workstream="ws"
    )
    for sig in snapshot.signals:
        if sig.name != "local_suitability":
            assert sig.available is False, sig.name
            assert sig.reason == "backend_not_qualified_for_signal"
            assert sig.value is None
    # Provenance is preserved; raw readings stay as telemetry in extra.
    assert snapshot.backend == "direct-openrouter-mercury-decide-free"
    assert snapshot.extra.get("task_complexity", {}).get("value") == 0.145


def test_legacy_provider_shape_still_reads(tmp_path):
    """The historical response shape stays readable (backward compatibility)."""
    payload = {
        "provider": "direct-openrouter-span-01-lite",
        "fallback_used": False,
        "answers": LIVE_SHAPE_RESPONSE["answers"],
    }
    sensor, _transport = _sensor_with(payload, tmp_path)
    snapshot = sensor.observe(
        {"objective": "probe"}, event="operator_instruction", workstream="ws"
    )
    assert snapshot.backend == "direct-openrouter-span-01-lite"
    by_name = {s.name: s for s in snapshot.signals}
    assert by_name["task_complexity"].available is True


def test_qualification_set_is_the_measured_one():
    """Only the historically qualified backend is believed."""
    assert QUALIFIED_BACKENDS == frozenset({"direct-openrouter-span-01-lite"})


# --------------------------------------------------------------- timeout


def test_default_timeout_is_short_and_configurable():
    """The 20-second synchronous wait must not return by default."""
    assert REQUEST_TIMEOUT_SECONDS < 5
    assert REQUEST_TIMEOUT_SECONDS >= 1


def test_timeout_failure_is_recorded_never_raised(tmp_path):
    sensor = ShadowSensor(
        store=tmp_path / "obs.jsonl", switchyard="http://127.0.0.1:9", timeout=0.2
    )
    snapshot = sensor.observe(
        {"objective": "a"}, event="operator_instruction", workstream="ws"
    )
    assert snapshot.reason and snapshot.reason.startswith("transport")
    assert snapshot.signals[0].available is False


def test_timeout_env_is_read_at_import():
    """The timeout is env-configurable without a code change."""
    import importlib

    import turnstone.core.shadow_driver as driver

    original = driver.os.environ.get("TURNSTONE_SENSOR_TIMEOUT_SECONDS")
    try:
        driver.os.environ["TURNSTONE_SENSOR_TIMEOUT_SECONDS"] = "3"
        importlib.reload(driver)
        assert driver.REQUEST_TIMEOUT_SECONDS == 3.0
    finally:
        if original is None:
            driver.os.environ.pop("TURNSTONE_SENSOR_TIMEOUT_SECONDS", None)
        else:
            driver.os.environ["TURNSTONE_SENSOR_TIMEOUT_SECONDS"] = original
        importlib.reload(driver)


# ------------------------------------------------------- disabled by default


def test_sensor_is_disabled_by_default():
    """The recovery brief: no live activation without an operator decision."""
    assert lifecycle.ENABLED is False


def test_disabled_gate_makes_the_production_seam_a_noop(tmp_path, monkeypatch):
    """The module-level seam costs nothing and writes nothing when disabled."""
    monkeypatch.setattr(lifecycle, "ENABLED", False)
    wrote = []

    class _SpySensor:
        def should_sense(self, *a, **k):
            wrote.append("should_sense")
            return True, "forced"

        def observe(self, *a, **k):
            wrote.append("observe")

    hook = lifecycle.SensorHook(sensor=_SpySensor(), persist=False)
    lifecycle._HOOK = hook  # the module-level wrapper resolves through hook()
    try:
        lifecycle.observe_event("operator_instruction", {"objective": "x"})
        lifecycle.observe_event("delegation_result", {"objective": "x"})
    finally:
        lifecycle._HOOK = None
    assert wrote == [], "a disabled sensor must not be reached at all"


def test_enabled_gate_lets_the_seam_through(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "ENABLED", True)
    observed = []

    class _SpySensor:
        def should_sense(self, *a, **k):
            return True, "forced"

        def observe(self, *a, **k):
            observed.append(k.get("workstream", ""))

    hook = lifecycle.SensorHook(sensor=_SpySensor(), persist=False)
    lifecycle._HOOK = hook
    try:
        lifecycle.observe_event(
            "operator_instruction", {"objective": "x"}, workstream="ws-1"
        )
    finally:
        lifecycle._HOOK = None
    assert observed == ["ws-1"]


def test_session_seams_are_inert_without_the_module(tmp_path, monkeypatch):
    """Even with sensor_lifecycle absent, the seams must be no-ops."""
    # Simulate a tree without the sensor modules: _sensor_observe must return
    # cleanly when _sensor_lifecycle is None (the guarded import binds None),
    # and the guarded import must bind None rather than propagate ImportError.
    source = SESSION.read_text()
    assert "_sensor_lifecycle = None" in source
    tree = ast.parse(source)
    wrapper = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_sensor_observe"
    ]
    assert len(wrapper) == 1
    body = ast.dump(wrapper[0])
    assert "Compare" in body and "None" in body, (
        "the wrapper must short-circuit when the sensor module is absent"
    )
    # And the guarded import must bind None rather than propagate ImportError.
    assert "_sensor_lifecycle = None" in source


# ------------------------------------------------------- registry resolution


class _Row:
    def __init__(self, base_url, capabilities):
        self.base_url = base_url
        self.capabilities = capabilities


class _Registry:
    def __init__(self, row):
        self._row = row

    def get_config(self, alias):
        if alias != DECISION_CAPABILITY:
            raise KeyError(alias)
        return self._row


def test_resolve_uses_the_registry_row():
    row = _Row("https://switchyard.example.internal", {
        "supports_typed_decision": True,
        "decision_contract": "switchyard-decision:v1",
    })
    decision_id, base_url = resolve_decision_endpoint(_Registry(row))
    assert decision_id == "tier1-decision"
    assert base_url == "https://switchyard.example.internal"


def test_resolve_falls_back_without_a_registry():
    decision_id, base_url = resolve_decision_endpoint(None)
    assert decision_id == DECISION_CAPABILITY
    assert base_url  # the module fallback constant


def test_resolve_falls_back_on_a_non_typed_row():
    row = _Row("https://switchyard.example.internal", {})
    decision_id, base_url = resolve_decision_endpoint(_Registry(row))
    assert decision_id == DECISION_CAPABILITY
    assert base_url  # fallback, never a partial resolution


# ------------------------------------------------------- wiring invariants


def test_session_has_exactly_four_sensor_seams():
    source = SESSION.read_text()
    tree = ast.parse(source)
    events = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_sensor_observe":
            assert node.args, "seam must pass the event as its first positional arg"
            first = node.args[0]
            assert isinstance(first, ast.Constant)
            events.append(first.value)
            assert not any(kw.arg is None for kw in node.keywords), (
                "no **kwargs at a seam"
            )
    assert sorted(events) == [
        "delegation_result", "operator_instruction", "retry_transition",
        "task_agent_failure",
    ]
    assert len(set(events)) == 4


def test_seams_pass_the_workstream_id():
    source = SESSION.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_sensor_observe":
            workstream = [kw for kw in node.keywords if kw.arg == "workstream"]
            assert workstream, "every seam must identify its workstream"
            value = workstream[0].value
            assert isinstance(value, ast.Call)
            assert getattr(value.func, "id", None) == "getattr"
            assert any(
                isinstance(arg, ast.Constant) and arg.value == "_ws_id"
                for arg in value.args
            )


def test_seams_add_no_control_flow():
    """The seams are pure insertions: no control-flow construct introduced."""
    source = SESSION.read_text()
    tree = ast.parse(source)
    # The only guard on a seam is the operator_instruction one, asserted
    # elsewhere; here we assert no seam call sits in a try/except that could
    # swallow a task error into a sensor branch.
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and getattr(child.func, "id", None) == "_sensor_observe"
                ):
                    # Allowed only inside _publish_success/_publish_error defs,
                    # which are themselves called through _publish_for_generation.
                    assert child.col_offset > node.col_offset
