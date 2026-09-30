"""Tests for the shadow observation contract, driver, and authority guards.

The authority tests are the point of this suite. A sensor that can influence
routing is not a sensor, so the guarantee is proved structurally: the snapshot
shape has no routing field, the module imports nothing that could set one, and
no function that mutates a routing control exists.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib

import pytest

from turnstone.core import exact_tokens, shadow_driver, shadow_observation
from turnstone.core.shadow_driver import ShadowSensor
from turnstone.core.shadow_observation import (
    DECISION_CAPABILITY,
    FORBIDDEN_KEYS,
    REQUESTED_SIGNALS,
    SENSOR_VERSION,
    Trigger,
    build_snapshot,
    is_material,
    state_fingerprint,
    unavailable,
)

CORE = pathlib.Path(shadow_observation.__file__).parent

# Tests run with an isolated HOME, but the pinned Laya tokenizer lives in the
# real home. Without pinning it here, every driver test that reaches token
# admission reports "tokenizer interpreter absent" and asserts on the wrong
# failure -- which reads as a fail-open regression rather than a missing
# fixture.
_REAL_HOME = os.environ.get("TURNSTONE_SENSOR_HOME") or os.path.expanduser("~")
_REAL_TOKENIZER = pathlib.Path(_REAL_HOME) / ".cache" / "turnstone-sensor-tokenizer"
if (_REAL_TOKENIZER / "tokenizer.json").is_file():
    exact_tokens.TOKENIZER_DIR = _REAL_TOKENIZER
    exact_tokens.TOKENIZER_JSON = _REAL_TOKENIZER / "tokenizer.json"
    exact_tokens.TOKENIZER_VENV_PYTHON = _REAL_TOKENIZER / "venv" / "bin" / "python"
TOKENIZER_INSTALLED = exact_tokens.TOKENIZER_JSON.is_file()


# ---------------------------------------------------------------- authority


def test_snapshot_carries_no_routing_fields():
    """No snapshot may contain a routing control, at any nesting depth."""
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL,
        fingerprint="fp",
        serializer_version="v1",
        answers={name: {"type": "noul", "noul": 0.5} for name in REQUESTED_SIGNALS},
        backend="span",
        fallback_used=False,
        latency_ms=10,
        state_truncated=False,
        serialized_chars=100,
        serialized_state_tokens=25,
        dropped_field_count=0,
    )
    blob = json.dumps(snapshot.to_dict()).lower()
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in blob, f"snapshot leaked routing field {key}"


def test_unavailable_snapshot_carries_no_routing_fields():
    snapshot = unavailable(
        trigger=Trigger.MATERIAL,
        fingerprint="fp",
        serializer_version="v1",
        reason="transport:timeout",
    )
    blob = json.dumps(snapshot.to_dict()).lower()
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in blob


def test_observation_module_imports_nothing_that_sets_routing():
    """Static proof: no import from a model/provider/routing module.

    A value cannot flow into a control that this module has no reference to.
    """
    tree = ast.parse(pathlib.Path(shadow_observation.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    banned = {"turnstone.llm", "turnstone.router", "turnstone.providers", "turnstone.agents"}
    assert not (imported & banned), f"observation module imports {imported & banned}"
    # The standard library only: no turnstone submodules at all except typing-ish.
    for name in imported:
        assert not name.startswith("turnstone."), f"unexpected import {name}"


def test_no_function_in_the_contract_mutates_anything():
    """There is no setter, applier or writer for a routing control."""
    tree = ast.parse(pathlib.Path(shadow_observation.__file__).read_text())
    mutating = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            lowered = node.name.lower()
            if any(
                verb in lowered
                for verb in ("set_model", "set_route", "set_provider", "apply",
                             "override", "force", "escalate", "select")
            ):
                mutating.append(node.name)
    assert not mutating, f"contract exposes mutators: {mutating}"


def test_driver_knows_only_the_one_capability():
    """The sensor must not contain provider or backend knowledge."""
    text = pathlib.Path(shadow_driver.__file__).read_text().lower()
    for secret in ("openrouter", "span-01", "100.105.20.36", "/v1/systemone", "laya"):
        # `laya-fallback` is a backend LABEL recorded from the response, not
        # knowledge of the backend's address or protocol. The address, endpoint
        # and provider must be absent.
        if secret == "laya":
            assert "100.105.20.36" not in text
            assert "/v1/systemone" not in text
            assert "laya-serve" not in text
            continue
        assert secret not in text, f"driver contains backend knowledge: {secret}"


def test_capability_constant_is_the_only_route_reference():
    # The composite lane (Span R1 -> Laya R2), NOT either direct lane: naming the
    # composite is what makes the sensor resilient without giving it any failover
    # knowledge. Pinning the value here means a rename cannot silently point the
    # sensor at a single-provider lane and quietly remove that resilience.
    assert DECISION_CAPABILITY == "switchyard-smartfree-aux-turnstone"
    assert DECISION_CAPABILITY != "switchyard-smart-aux-turnstone"
    assert DECISION_CAPABILITY != "switchyard-smartlocal-aux-turnstone"


# ------------------------------------------------------------------ contract


def test_local_suitability_is_never_available():
    """It is not requested, not inferred, and not carried forward."""
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL,
        fingerprint="fp",
        serializer_version="v1",
        # A response that even volunteers the signal must not make it available.
        answers={
            **{n: {"type": "noul", "noul": 0.5} for n in REQUESTED_SIGNALS},
            "local_suitability": {"type": "noul", "noul": 0.9},
        },
        backend="laya-fallback",
        fallback_used=True,
        latency_ms=10,
        state_truncated=False,
        serialized_chars=10,
        serialized_state_tokens=5,
        dropped_field_count=0,
    )
    signal = next(s for s in snapshot.signals if s.name == "local_suitability")
    assert signal.available is False
    assert signal.value is None
    assert signal.reason == "backend_not_qualified_for_signal"


def test_observability_stays_unknown():
    """An upstream confidence field must not switch observability on."""
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL,
        fingerprint="fp",
        serializer_version="v1",
        answers={n: {"type": "noul", "noul": 0.5, "confidence": 0.99} for n in REQUESTED_SIGNALS},
        backend="span",
        fallback_used=False,
        latency_ms=10,
        state_truncated=False,
        serialized_chars=10,
        serialized_state_tokens=5,
        dropped_field_count=0,
    )
    assert all(s.observability.value == "unknown" for s in snapshot.signals)


def test_absent_signal_is_unavailable_not_zero():
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL, fingerprint="fp", serializer_version="v1",
        answers={"task_complexity": {"type": "noul", "noul": 0.5}},
        backend="span", fallback_used=False, latency_ms=1,
        state_truncated=False, serialized_chars=1, serialized_state_tokens=1,
        dropped_field_count=0,
    )
    absent = [
        s for s in snapshot.signals
        if s.name in REQUESTED_SIGNALS and s.name != "task_complexity"
    ]
    assert absent, "every unreturned requested signal must be recorded"
    assert all(s.available is False for s in absent)
    assert all(s.value is None for s in absent), "absence must never read as zero"
    assert all(s.reason == "signal_absent_from_response" for s in absent)
    # local_suitability keeps its own fixed reason and is not conflated with a
    # signal that merely went missing.
    unsupported = next(s for s in snapshot.signals if s.name == "local_suitability")
    assert unsupported.reason == "backend_not_qualified_for_signal"


# -------------------------------------------------------------------- dedupe


def test_fingerprint_is_deterministic_and_order_independent():
    a = state_fingerprint({"objective": "x", "progress": "y"})
    b = state_fingerprint({"progress": "y", "objective": "x"})
    assert a == b
    assert a == state_fingerprint({"objective": " x ", "progress": "y"})


def test_fingerprint_ignores_no_volatile_input_by_construction():
    """A volatile value must not be able to sneak into the fingerprint.

    The fingerprint takes the state mapping as given; a caller who puts a
    timestamp in their state gets a different fingerprint, which is why the
    driver is documented to fingerprint only serializer-relevant state. What is
    asserted here is that the function itself is a pure function of the mapping.
    """
    base = {"objective": "ship it"}
    assert state_fingerprint(base) == state_fingerprint(dict(base))
    assert state_fingerprint(base) != state_fingerprint({**base, "t": "1"})


# ------------------------------------------------------------------- cadence


@pytest.mark.parametrize(
    "event",
    ["operator_instruction", "delegation_result", "tool_failure", "new_evidence"],
)
def test_material_events_are_recognised(event):
    assert is_material(event)


@pytest.mark.parametrize("event", ["ack", "formatting", "", "status_poll", None])
def test_non_material_events_are_suppressed(event):
    assert not is_material(event or "")


def test_first_observation_is_always_taken():
    sensor = ShadowSensor(store=pathlib.Path("/tmp/does-not-matter.jsonl"))
    allowed, why = sensor.should_sense({"objective": "a"}, event=None)
    assert allowed and why == Trigger.INITIAL.value


def test_identical_state_is_deduplicated():
    sensor = ShadowSensor(store=pathlib.Path("/tmp/does-not-matter.jsonl"))
    sensor._initial_done = True
    sensor._last_fingerprint = state_fingerprint({"objective": "a"})
    allowed, why = sensor.should_sense({"objective": "a"}, event="new_evidence")
    assert not allowed and why == "unchanged_state"


def test_non_material_event_is_suppressed_before_dedupe():
    sensor = ShadowSensor(store=pathlib.Path("/tmp/does-not-matter.jsonl"))
    sensor._initial_done = True
    sensor._last_fingerprint = None
    allowed, why = sensor.should_sense({"objective": "a"}, event="ack")
    assert not allowed and why.startswith("not_material")


def test_changed_material_state_is_sensed():
    sensor = ShadowSensor(store=pathlib.Path("/tmp/does-not-matter.jsonl"))
    sensor._initial_done = True
    sensor._last_fingerprint = state_fingerprint({"objective": "a"})
    allowed, why = sensor.should_sense({"objective": "b"}, event="new_evidence")
    assert allowed and why == Trigger.MATERIAL.value


# ----------------------------------------------------------------- fail open


def test_switchyard_failure_fails_open(tmp_path):
    sensor = ShadowSensor(store=tmp_path / "o.jsonl", switchyard="http://127.0.0.1:9")
    snapshot = sensor.observe({"objective": "a"}, event=None)
    assert snapshot.reason and snapshot.reason.startswith("transport")
    assert all(s.available is False for s in snapshot.signals)
    # The task continues: the call returns normally rather than raising.
    assert snapshot.sensor_version == SENSOR_VERSION


def test_serializer_failure_fails_open(tmp_path):
    sensor = ShadowSensor(store=tmp_path / "o.jsonl")
    snapshot = sensor.observe({"transcript": {"messages": ["secret"]}}, event=None)
    assert snapshot.reason and snapshot.reason.startswith("serializer_error")


def test_storage_failure_does_not_propagate(tmp_path):
    sensor = ShadowSensor(store=tmp_path / "nodir" / "x" / "\0bad")
    snapshot = sensor.observe({"objective": "a"}, event=None)
    assert snapshot is not None  # returned, not raised


def test_no_retry_storm_on_failure(tmp_path):
    """One observe() call means at most one HTTP attempt."""
    sensor = ShadowSensor(store=tmp_path / "o.jsonl", switchyard="http://127.0.0.1:9")
    snapshot = sensor.observe({"objective": "a"}, event=None)
    assert snapshot.latency_ms < 5000, "a failed call must not retry internally"


def test_metrics_report_truncation_rate(tmp_path):
    sensor = ShadowSensor(store=tmp_path / "o.jsonl", switchyard="http://127.0.0.1:9")
    for i in range(4):
        sensor.observe(
            {"objective": f"task {i}", "history": "x " * 400},
            event="new_evidence",
            force=True,
        )
    metrics = sensor.metrics.to_dict()
    assert metrics["observation_count"] == 4
    assert 0.0 <= metrics["truncation_rate"] <= 1.0
    assert metrics["truncation_rate"] > 0, "oversized states must register as truncated"


# ------------------------------------------------------------------ parity


def test_question_set_matches_the_qualified_sensor_layer():
    """The self-contained question set must be byte-identical to the qualified one.

    These questions were qualified against Span and the Laya fallback. The
    shadow path carries its own copy so it does not depend on `typed_decision`,
    which the live package does not ship - but a reworded copy would silently
    invalidate that qualification while still working. This pins the two.
    """
    from turnstone.core import sensor_layer, sensor_questions

    assert sensor_questions.SENSOR_QUESTIONS == sensor_layer._SENSOR_QUESTIONS

    signals = tuple(REQUESTED_SIGNALS)
    state = "objective: parity"
    a = sensor_questions.build_request(
        decision_id=DECISION_CAPABILITY, state=state, signal_names=signals
    )
    b = sensor_layer.build_sensor_request(
        state=state, decision_id=DECISION_CAPABILITY, signal_names=signals
    )
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ------------------------------------------------- provider-neutral telemetry


def test_backend_label_comes_from_the_response_not_the_driver():
    """The backend label must be a fact about who answered, not a rank.

    The driver used to compute `backend = "laya-fallback" if fallback_used`,
    which hardcoded a provider into the contract and read as a quality
    judgement. Laya and Span are qualified peer providers; the label must
    report the provider that actually answered.

    Comments and docstrings are stripped so the module's own explanation of the
    old label cannot satisfy the check. String LITERALS are kept, because that
    is precisely where a hardcoded provider would live -- an earlier version
    blanked them too, which made the check pass against the exact mutant it
    existed to catch.
    """
    import io
    import tokenize as _tokenize

    source = pathlib.Path(shadow_driver.__file__).read_text()
    # Drop comments and docstrings, keep every other literal.
    import ast as _ast

    tree = _ast.parse(source)
    docstrings = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                            _ast.ClassDef)):
            body = _ast.get_docstring(node, clean=False)
            if body is not None:
                docstrings.add(body)
    kept: list[str] = []
    for token in _tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == _tokenize.COMMENT:
            continue
        if token.type == _tokenize.STRING and token.string.strip("\"'rbn") in docstrings:
            continue
        kept.append(token.string)
    code = "".join(kept)

    for provider in ("laya", "span"):
        assert provider not in code.lower(), (
            f"executable code must not hardcode the provider name {provider!r} "
            "into the backend label"
        )


def test_failover_leg_is_labelled_as_a_mechanism_not_a_provider():
    """A failover leg with no provider field is labelled by mechanism."""
    snapshot = build_snapshot(
        trigger=Trigger.MATERIAL, fingerprint="f",
        serializer_version="turnstone-bounded-state:v1",
        answers={n: {"noul": 0.5} for n in REQUESTED_SIGNALS},
        backend="failover_leg", fallback_used=True, latency_ms=5,
        state_truncated=False, serialized_chars=10,
        serialized_state_tokens=5, dropped_field_count=0,
    )
    assert snapshot.backend == "failover_leg"
    assert snapshot.fallback_used is True
    # A mechanism label carries no provider identity at all.
    assert "laya" not in snapshot.backend.lower()
    assert "span" not in snapshot.backend.lower()


def test_any_provider_name_is_carried_through_verbatim():
    """A provider the driver has never heard of must still be reported."""
    for provider in ("Respan", "some-future-provider", "local-laya"):
        snapshot = build_snapshot(
            trigger=Trigger.MATERIAL, fingerprint="f",
            serializer_version="turnstone-bounded-state:v1",
            answers={n: {"noul": 0.5} for n in REQUESTED_SIGNALS},
            backend=provider, fallback_used=False, latency_ms=5,
            state_truncated=False, serialized_chars=10,
            serialized_state_tokens=5, dropped_field_count=0,
        )
        assert snapshot.backend == provider, (
            f"backend label must not be rewritten: {provider!r} -> "
            f"{snapshot.backend!r}"
        )


def test_fallback_flag_describes_mechanism_not_rank():
    """`fallback_used` stays: it is a truthful fact about the request path."""
    fields = set(shadow_observation.SensorSnapshot.__dataclass_fields__)
    assert "fallback_used" in fields
    # But there is no field that encodes a provider hierarchy.
    for banned in ("primary", "secondary", "tier", "rank", "preferred_provider"):
        assert banned not in fields, f"contract encodes a ranking: {banned}"
