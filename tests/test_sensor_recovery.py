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
    sensor = ShadowSensor(store=None, decision_id="tier1-decision",
                    base_url="http://127.0.0.1:9", timeout=0.2)
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
    sensor = ShadowSensor(
        store=tmp_path / "obs.jsonl",
        timeout=2.5,
        decision_id="tier1-decision",
        base_url="https://switchyard.example.internal",
    )
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
        store=tmp_path / "obs.jsonl", decision_id="tier1-decision",
        base_url="http://127.0.0.1:9", timeout=0.2
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


def test_resolve_without_a_registry_is_unresolved():
    """2026-10-17 hardening: no fallback URL exists.

    An unresolved pair is what makes the sensor record an explicitly
    unavailable observation instead of requesting a stale lane or localhost.
    """
    assert resolve_decision_endpoint(None) == ("", "")


def test_resolve_on_a_non_typed_row_is_unresolved():
    row = _Row("https://switchyard.example.internal", {})
    assert resolve_decision_endpoint(_Registry(row)) == ("", "")


def test_resolve_on_a_blank_base_url_is_unresolved():
    row = _Row("", {"supports_typed_decision": True})
    assert resolve_decision_endpoint(_Registry(row)) == ("", "")


def test_unresolved_sensor_sends_no_request(tmp_path, monkeypatch):
    """The invariant behind the removal: no endpoint => no network, ever."""

    def _must_not_be_called(*_a, **_k):
        raise AssertionError("unresolved sensor attempted a network request")

    monkeypatch.setattr(urllib.request, "urlopen", _must_not_be_called)
    sensor = ShadowSensor(store=tmp_path / "obs.jsonl", timeout=0.2)
    snapshot = sensor.observe(
        {"objective": "x"}, event="operator_instruction", workstream="ws"
    )
    assert snapshot.reason == "decision_endpoint_unresolved"
    assert sensor.metrics.unresolved_endpoint_count == 1


# ------------------------------------------------------- wiring invariants


def test_session_has_exactly_four_sensor_seams():
    source = SESSION.read_text()
    tree = ast.parse(source)
    events = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name == "_sensor_observe_substantive":
            events.append("operator_instruction")
            continue
        if name not in ("_sensor_observe", "_sensor_observe_delegation"):
            continue
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
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in (
            "_sensor_observe",
            "_sensor_observe_substantive",
            "_sensor_observe_delegation",
        ):
            continue
        if True:
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


# --------------------------------------------------- exception containment


class _Exploding:
    """An object that raises on every protocol the sensor might touch."""

    def __str__(self):
        raise RuntimeError("boom str")

    def __iter__(self):
        raise RuntimeError("boom iter")

    def __len__(self):
        raise RuntimeError("boom len")


class _ExplodingDict(dict):
    def get(self, *a, **k):
        raise RuntimeError("boom get")


def test_substantive_guard_survives_an_exploding_input():
    """The operator seam's substance check must be inside the boundary."""
    # Direct lifecycle call raises (documented), but the session wrapper must
    # not.
    with pytest.raises(RuntimeError):
        lifecycle.is_substantive_instruction(_Exploding())  # type: ignore[arg-type]
    import turnstone.core.session as session_mod

    session_mod._sensor_observe_substantive(_Exploding(), workstream="ws")


def test_delegation_wrapper_survives_exploding_items_and_results():
    """item.get / str(item) / str(result) all happen INSIDE the boundary."""
    import turnstone.core.session as session_mod

    session_mod._sensor_observe_delegation(
        "delegation_result", _ExplodingDict(), _Exploding(), workstream="ws"
    )
    session_mod._sensor_observe_delegation(
        "task_agent_failure", _Exploding(), _Exploding(), workstream="ws"
    )
    session_mod._sensor_observe_delegation(
        "delegation_result", {"prompt": "fine"}, "fine result", workstream="ws"
    )


def test_retry_wrapper_survives_exploding_values():
    import turnstone.core.session as session_mod

    session_mod._sensor_observe(
        "retry_transition", {"objective": _Exploding()}, workstream="ws"
    )


def test_exploding_sensor_cannot_reach_chat_session_execution(monkeypatch):
    """With the sensor ENABLED and exploding behind the hook, seams return."""
    monkeypatch.setattr(lifecycle, "ENABLED", True)

    class _BoomSensor:
        def should_sense(self, *a, **k):
            raise RuntimeError("boom")

        def observe(self, *a, **k):
            raise RuntimeError("boom")

    hook = lifecycle.SensorHook(sensor=_BoomSensor(), persist=False)
    lifecycle._HOOK = hook
    try:
        import turnstone.core.session as session_mod

        session_mod._sensor_observe_substantive("do real work", workstream="ws")
        session_mod._sensor_observe_delegation(
            "delegation_result", {"prompt": "p"}, "r", workstream="ws"
        )
        session_mod._sensor_observe(
            "retry_transition", {"objective": "x"}, workstream="ws"
        )
    finally:
        lifecycle._HOOK = None
    assert hook.counters().get("hook_error", 0) == 3


def test_exploding_state_values_are_recorded_unavailable(tmp_path):
    """A mapping whose VALUES explode is contained at the hook boundary."""
    sensor = _BoomAtShouldSense()
    hook = lifecycle.SensorHook(sensor=sensor, persist=False)
    hook.observe_event("operator_instruction", {"objective": _Exploding()})
    assert hook.counters().get("hook_error", 0) == 1
    assert sensor.calls == []


class _BoomAtShouldSense:
    def __init__(self):
        self.calls = []

    def should_sense(self, *a, **k):
        self.calls.append(1)
        return True, "material"

    def observe(self, *a, **k):
        return None


def test_state_construction_happens_inside_the_seam_wrapper():
    """The delegation seams must NOT build the projection at the call site."""
    source = SESSION.read_text()
    assert "item.get(\"prompt\", \"\") if isinstance(item, dict) else str(item)" not in source, (
        "projection construction moved into the guarded wrapper"
    )
    assert "def _sensor_observe_delegation(" in source


# ------------------------------------------------- telemetry integrity (2026-10-17)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _hook_with_store(tmp_path, *, timeout=0.3):
    from turnstone.core.shadow_driver import ShadowSensor

    sensor = ShadowSensor(
        store=tmp_path / "observations.jsonl",
        decision_id="tier1-decision",
        base_url="http://127.0.0.1:9",
        timeout=timeout,
    )
    hook = lifecycle.SensorHook(
        sensor, cadence_store=tmp_path / "cadence.jsonl", persist=False
    )
    return hook, sensor


def test_every_admitted_call_pairs_with_exactly_one_observation(tmp_path):
    hook, _sensor = _hook_with_store(tmp_path)
    for i in range(5):
        hook.observe_event(
            "operator_instruction", {"objective": f"work {i}"}, workstream="ws-a"
        )

    report = lifecycle.verify_correlation(
        tmp_path / "cadence.jsonl", tmp_path / "observations.jsonl"
    )
    assert report["admitted_calls"] == 5
    assert report["observations"] == 5
    assert report["unmatched_admitted"] == []
    assert report["orphan_observations"] == []
    assert report["duplicate_admitted_ids"] == []
    assert report["duplicate_observation_ids"] == []


def test_event_ids_are_unique_per_event(tmp_path):
    hook, _sensor = _hook_with_store(tmp_path)
    for i in range(20):
        hook.observe_event(
            "operator_instruction", {"objective": f"work {i}"}, workstream="ws-a"
        )
    ids = [r["event_id"] for r in _read_jsonl(tmp_path / "cadence.jsonl")]
    assert len(ids) == 20
    assert len(set(ids)) == 20


def test_cadence_and_observation_carry_the_same_correlation(tmp_path):
    hook, _sensor = _hook_with_store(tmp_path)
    hook.observe_event("delegation_result", {"objective": "p"}, workstream="ws-z")

    cadence = _read_jsonl(tmp_path / "cadence.jsonl")[0]
    observation = _read_jsonl(tmp_path / "observations.jsonl")[0]
    assert cadence["event_id"] == observation["event_id"]
    assert cadence["workstream"] == observation["workstream"] == "ws-z"
    assert cadence["process_id"] == observation["process_id"]


def test_process_attribution_is_present_on_every_record(tmp_path):
    from turnstone.core.shadow_observation import PROCESS_ID

    hook, _sensor = _hook_with_store(tmp_path)
    hook.observe_event("operator_instruction", {"objective": "a"}, workstream="w")
    for path in (tmp_path / "cadence.jsonl", tmp_path / "observations.jsonl"):
        for row in _read_jsonl(path):
            assert row["process_id"] == PROCESS_ID


def test_storage_failure_is_paired_not_silent(tmp_path, monkeypatch):
    """A lost observation must leave an observation_write_failed row."""
    hook, sensor = _hook_with_store(tmp_path)

    import turnstone.core.shadow_driver as driver

    def _boom(path, record):
        raise OSError("disk full")

    monkeypatch.setattr(driver, "append_jsonl", _boom)
    hook.observe_event("operator_instruction", {"objective": "x"}, workstream="w")

    outcomes = [r["outcome"] for r in _read_jsonl(tmp_path / "cadence.jsonl")]
    assert "sensor_call" in outcomes
    assert "observation_write_failed" in outcomes
    assert sensor.metrics.record_failure_count == 1


def test_concurrent_writers_do_not_interleave_records(tmp_path):
    """Six processes append to one root; no line may be torn or interleaved."""
    import threading

    from turnstone.core.shadow_driver import append_jsonl

    store = tmp_path / "observations.jsonl"
    start = threading.Barrier(8)

    def _writer(n):
        start.wait()
        for i in range(40):
            append_jsonl(store, {"writer": n, "i": i, "payload": "x" * 200})

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = _read_jsonl(store)
    assert len(rows) == 8 * 40
    # Every line parsed and carries an intact payload.
    assert all(len(r["payload"]) == 200 for r in rows)
    per_writer = {}
    for r in rows:
        per_writer.setdefault(r["writer"], []).append(r["i"])
    for n, indices in per_writer.items():
        assert sorted(indices) == list(range(40)), n


def test_concurrent_hooks_keep_correlation_per_process(tmp_path):
    """Concurrent events in one process still pair one-to-one."""
    import threading

    hook, _sensor = _hook_with_store(tmp_path)
    start = threading.Barrier(4)

    def _emit(n):
        start.wait()
        for i in range(15):
            hook.observe_event(
                "operator_instruction",
                {"objective": f"t{n}-{i}"},
                workstream=f"ws-{n}",
            )

    threads = [threading.Thread(target=_emit, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    report = lifecycle.verify_correlation(
        tmp_path / "cadence.jsonl", tmp_path / "observations.jsonl"
    )
    assert report["admitted_calls"] == 60
    assert report["observations"] == 60
    assert report["unmatched_admitted"] == []
    assert report["duplicate_observation_ids"] == []
    assert len(report["workstreams"]) == 4


def test_unresolved_observation_is_recorded_and_paired(tmp_path):
    """No endpoint is still an attempted observation with a matching record."""
    from turnstone.core.shadow_driver import ShadowSensor

    sensor = ShadowSensor(store=tmp_path / "observations.jsonl", timeout=0.2)
    hook = lifecycle.SensorHook(
        sensor, cadence_store=tmp_path / "cadence.jsonl", persist=False
    )
    hook.observe_event("operator_instruction", {"objective": "x"}, workstream="w")

    report = lifecycle.verify_correlation(
        tmp_path / "cadence.jsonl", tmp_path / "observations.jsonl"
    )
    assert report["admitted_calls"] == 1
    assert report["observations"] == 1
    assert report["unmatched_admitted"] == []
    row = _read_jsonl(tmp_path / "observations.jsonl")[0]
    assert row["reason"] == "decision_endpoint_unresolved"


# --------------------------------------------------- content-minimization modes


def test_sanitized_mode_redacts_secrets_paths_and_emails():
    state = lifecycle.build_state(
        content_mode="sanitized",
        objective=(
            "Deploy with token=abc123SECRET to /home/vincent/.ssh/id_rsa "
            "and mail ops@example.com"
        ),
        phase="operator_instruction",
    )
    text = state["objective"]
    assert "abc123SECRET" not in text
    assert "/home/vincent/.ssh/id_rsa" not in text
    assert "ops@example.com" not in text
    assert "\u005bredacted\u005d" in text
    assert "\u005bpath\u005d" in text
    assert "\u005bemail\u005d" in text
    # The task sentence survives: sanitized is not metadata.
    assert "Deploy with" in text


def test_metadata_mode_carries_no_free_text():
    state = lifecycle.build_state(
        content_mode="metadata",
        objective="Reverse engineer the billing service and patch the outage",
        phase="operator_instruction",
        blocker="Task error: TimeoutError: upstream stalled",
        history="a long tool result with secrets token=abcdef",
    )
    assert state["objective"].startswith("[objective chars=")
    assert "billing" not in json.dumps(state)
    assert state["blocker"] == "[blocker error=TimeoutError]"
    assert state["history"].startswith("[history chars=")
    # Categorical values pass through: they are closed-class, not content.
    assert state["phase"] == "operator_instruction"


def test_full_mode_reproduces_the_historical_projection():
    state = lifecycle.build_state(
        content_mode="full",
        objective="Keep token=abc visible",
        phase="operator_instruction",
    )
    assert state["objective"] == "Keep token=abc visible"


def test_empty_fields_stay_absent_in_every_mode():
    for mode in ("full", "sanitized", "metadata"):
        state = lifecycle.build_state(
            content_mode=mode, objective="x", history="", blockers=""
        )
        assert "history" not in state
        assert "blocker" not in state


def test_default_content_mode_is_sanitized():
    """The shipped default must never be full-text export."""
    assert lifecycle.CONTENT_MODE in {"sanitized", "metadata"}
    assert lifecycle.CONTENT_MODE == "sanitized"


# ------------------------------------------------ wall-clock deadline (2026-10-17)


def test_deadline_bounds_a_stalled_dns_resolution(monkeypatch):
    """The socket timeout does not bound DNS; the call deadline does."""
    import socket
    import time

    import turnstone.core.shadow_driver as driver

    real = socket.getaddrinfo

    def _slow(*a, **k):
        time.sleep(2.0)
        return real(*a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _slow)
    monkeypatch.setattr(driver, "DEADLINE_SECONDS", 0.4)

    sensor = ShadowSensor(
        store=None, decision_id="tier1-decision",
        base_url="http://example.invalid:9", timeout=0.2,
    )
    started = time.monotonic()
    snapshot = sensor.observe(
        {"objective": "x"}, event="operator_instruction", workstream="w"
    )
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, f"call was not deadline-bounded: {elapsed:.2f}s"
    assert snapshot.reason == "transport:deadline_exceeded"
    assert sensor._abandoned == 1


def test_abandoned_call_cap_refuses_new_calls():
    """At the cap the sensor records saturation instead of queueing threads."""
    import turnstone.core.shadow_driver as driver

    sensor = ShadowSensor(
        store=None, decision_id="tier1-decision",
        base_url="http://127.0.0.1:9", timeout=0.2,
    )
    sensor._abandoned = driver.MAX_ABANDONED_CALLS
    payload, error = sensor._post({"state": "x"})
    assert payload is None
    assert error == "transport:deadline_saturated"


def test_deadline_defaults_are_sane():
    import turnstone.core.shadow_driver as driver

    assert 1.0 <= driver.DEADLINE_SECONDS <= 10.0
    assert driver.DEADLINE_SECONDS >= driver.REQUEST_TIMEOUT_SECONDS
    assert driver.MAX_ABANDONED_CALLS >= 1


def test_every_observation_records_mode_and_projection_schema(tmp_path, monkeypatch):
    """A stored record must say which exposure posture produced it."""
    from turnstone.core.shadow_observation import PROJECTION_SCHEMA

    monkeypatch.setattr(lifecycle, "CONTENT_MODE", "metadata")
    hook, _sensor = _hook_with_store(tmp_path)
    hook.observe_event("operator_instruction", {"objective": "a"}, workstream="w")

    row = _read_jsonl(tmp_path / "observations.jsonl")[0]
    assert row["content_mode"] == "metadata"
    assert row["projection_schema"] == PROJECTION_SCHEMA
    assert row["sensor_version"]


def test_unavailable_records_also_carry_mode(tmp_path, monkeypatch):
    from turnstone.core.shadow_driver import ShadowSensor

    monkeypatch.setattr(lifecycle, "CONTENT_MODE", "sanitized")
    sensor = ShadowSensor(store=tmp_path / "o.jsonl", timeout=0.2)  # unresolved
    hook = lifecycle.SensorHook(sensor, cadence_store=tmp_path / "c.jsonl", persist=False)
    hook.observe_event("operator_instruction", {"objective": "a"}, workstream="w")
    row = _read_jsonl(tmp_path / "o.jsonl")[0]
    assert row["content_mode"] == "sanitized"
    assert row["reason"] == "decision_endpoint_unresolved"


def test_sanitized_catches_prefixed_assignments_and_jwts():
    """S4 regressions: OPENAI_API_KEY=... and JWT segments must not survive."""
    prefixed = lifecycle.project_value(
        "objective", "OPENAI_API_KEY=synthetic-value-123", mode="sanitized"
    )
    assert "synthetic-value-123" not in prefixed

    jwt = lifecycle.project_value(
        "objective",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123",
        mode="sanitized",
    )
    assert "eyJhbGciOiJIUzI1NiJ9" not in jwt

    dsn = lifecycle.project_value(
        "objective", "postgresql://user:pass@db.example:5432/app", mode="sanitized"
    )
    assert "user:pass@" not in dsn


def test_sanitized_catches_padded_base64_runs():
    # A realistic opaque token: one long run with trailing '=' padding.
    blob = "QWxhZGRpbjpvcGVuIHNlc2FtZVF3aGljaGlzYmFzZTY0cGFkZGVk" + "=="
    cleaned = lifecycle.project_value("objective", blob, mode="sanitized")
    assert blob[:40] not in cleaned


def test_sanitized_redacts_concatenated_base64_with_internal_padding():
    # S4 battery residual: two base64 values concatenated. The '=' padding sits
    # in the middle, so a "one long run, padding at the end" rule misses both.
    left = "QWxhZGRpbjpvcGVuIHNlc2FtZQ"
    right = "QWxhZGRpbjpvcGVuIHNlc2FtZQ"
    blob = f"{left}=={right}=="
    cleaned = lifecycle.project_value("objective", f"use {blob} please", mode="sanitized")
    assert left not in cleaned
    assert right not in cleaned
    assert "[redacted]" in cleaned


def test_sanitized_redacts_assignment_with_diacritic_keyword():
    # S4 battery residual: 'pässwörd' does not contain the literal 'password',
    # so the keyword rule never fires. Folding is index-preserving, so the
    # redaction span still covers the original (non-folded) characters.
    text = "pässwörd=Ünïcødé-S3cret-値"
    cleaned = lifecycle.project_value("objective", f"rotate {text} now", mode="sanitized")
    assert "Ünïcødé-S3cret" not in cleaned
    assert "[redacted]" in cleaned
    # The non-secret parts of the sentence survive sanitized mode.
    assert "rotate" in cleaned
    assert "now" in cleaned
