# SPDX-License-Identifier: Apache-2.0
"""Tests for the advisory sensor layer.

Fixtures are REAL-SHAPED, not invented: the response shapes below are taken
from the deployed capability executor (``normalize_one_decision`` and the
decisions handler in the v0.3.0 candidate tree), including the two behaviours
that make a naive sensor dishonest - the generative leg's ``unparsed`` answers
and its fabricated ``1.0`` for an absent numeric.

Every test that asserts a negative needs a positive control, or it proves
nothing. The controls are marked CONTROL.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from turnstone.core.sensor_layer import (
    MAX_EVIDENCE_FIELDS,
    MAX_INTENT_CHARS,
    MAX_KEY_CHARS,
    MAX_STATE_CHARS,
    SENSOR_CONTRACT,
    SENSOR_SCHEMA_VERSION,
    Observation,
    SensorUnavailable,
    aggregate_confidence,
    build_sensor_request,
    normalize_sensor_answers,
    render_sensor_state,
    sense,
)
from turnstone.core.typed_decision import TypedDecisionSpec

SIGNALS = ("task_complexity", "reasoning_demand", "tool_dependency")

TYPED_CAPS = {
    "supports_typed_decision": True,
    "decision_contract": SENSOR_CONTRACT,
    "decision_types": ["noul"],
    "decision_endpoint": "/v1/decisions",
    "max_questions": 16,
}


def _spec() -> TypedDecisionSpec:
    return TypedDecisionSpec.from_capabilities(TYPED_CAPS)


def _primary_answers(**values: float) -> dict:
    """A real primary-leg body: answers keyed by the CALLER's question ids."""
    return {
        "answers": {
            name: {"type": "noul", "noul": value} for name, value in values.items()
        },
        "contract": SENSOR_CONTRACT,
        "served_model": "switchyard-smartfree-aux-turnstone",
        "provider": "Respan",
    }


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------


def test_request_asks_only_noul_questions():
    """CONTROL: the questions are built at all, and every type is noul."""
    body = build_sensor_request(
        state="tool calls: 12", decision_id="m", signal_names=SIGNALS
    )
    assert set(body["questions"]) == set(SIGNALS)
    assert all(q["type"] == "noul" for q in body["questions"].values())
    for q in body["questions"].values():
        # criteria are the question's meaning, not decoration.
        assert set(q["criteria"]) == {"true", "false"}


def test_request_never_reuses_the_guard_question():
    """The sensor must not overwrite the Output Guard's security question.

    ``typed_decision.build_request_body`` hardcodes a single categorical
    ``risk`` question. If the sensor ever emitted that shape it would either
    overwrite a security control or send an unintended body.
    """
    from turnstone.core.typed_decision import build_request_body

    guard = build_request_body(
        state="Tool read_file produced this output:",
        spec=_spec(),
        decision_id="m",
    )
    sensor = build_sensor_request(
        state="tool calls: 12", decision_id="m", signal_names=SIGNALS
    )
    assert "risk" not in sensor["questions"]
    assert "risk" in guard["questions"]
    # The guard's own builder is untouched by any of this module's work.
    assert guard["questions"]["risk"]["type"] == "noul"


def test_request_rejects_unknown_and_empty_signals():
    with pytest.raises(SensorUnavailable):
        build_sensor_request(state="x", decision_id="m", signal_names=("nope",))
    with pytest.raises(SensorUnavailable):
        build_sensor_request(state="x", decision_id="m", signal_names=())
    with pytest.raises(SensorUnavailable):
        build_sensor_request(state="   ", decision_id="m", signal_names=SIGNALS)


# --------------------------------------------------------------------------
# Content boundary
# --------------------------------------------------------------------------


def test_state_renders_bounded_scalars():
    state = render_sensor_state({"tool_calls": 12, "retries": 0, "escalated": True})
    assert "tool_calls: 12" in state
    assert "escalated: true" in state


def test_state_refuses_nested_task_content():
    """A transcript handed over by mistake must raise, not leak."""
    for bad in ({"t": {"a": 1}}, {"t": [1, 2]}, {"t": object()}):
        with pytest.raises(SensorUnavailable):
            render_sensor_state(bad)


def test_intent_is_truncated_at_the_hard_cap():
    state = render_sensor_state({}, intent="x" * (MAX_INTENT_CHARS + 500))
    assert len(state) <= MAX_INTENT_CHARS + 20  # + "Task intent: " prefix


def test_oversized_free_text_evidence_is_refused():
    with pytest.raises(SensorUnavailable):
        render_sensor_state({"note": "y" * (MAX_INTENT_CHARS + 1)})


def test_state_refuses_unbounded_keys_and_field_counts():
    """F3: keys are caller-chosen, so they are content too, and bounded here."""
    with pytest.raises(SensorUnavailable):
        render_sensor_state({"k" * (MAX_KEY_CHARS + 1): 1})
    too_many = {f"field_{i}": i for i in range(MAX_EVIDENCE_FIELDS + 1)}
    with pytest.raises(SensorUnavailable):
        render_sensor_state(too_many)


def test_state_refuses_a_total_overflow():
    """CONTROL for the per-field cap: many legal fields can still overflow."""
    payload = {
        f"field_{i}": "z" * MAX_INTENT_CHARS for i in range(MAX_EVIDENCE_FIELDS)
    }
    with pytest.raises(SensorUnavailable):
        render_sensor_state(payload)


def test_state_refuses_non_string_keys_and_non_mapping_evidence():
    with pytest.raises(SensorUnavailable):
        render_sensor_state({1: "a"})
    with pytest.raises(SensorUnavailable):
        render_sensor_state("not-a-mapping")  # type: ignore[arg-type]


def test_legal_field_count_is_accepted():
    """CONTROL: the field-count arm above is not passing because all counts fail."""
    ok = {f"field_{i}": i for i in range(MAX_EVIDENCE_FIELDS)}
    state = render_sensor_state(ok)
    assert len(state) <= MAX_STATE_CHARS


# --------------------------------------------------------------------------
# Normalization: real response shapes
# --------------------------------------------------------------------------


def test_primary_leg_normalizes_graded_signals():
    payload = _primary_answers(**{"task_complexity": 0.82, "tool_dependency": 0.2})
    obs = normalize_sensor_answers(payload, _spec(), SIGNALS)
    assert obs["task_complexity"].value == pytest.approx(0.82)
    assert obs["tool_dependency"].value == pytest.approx(0.2)
    # Absent from the response => explicitly unobservable, never a zero.
    assert obs["reasoning_demand"].value is None
    assert not obs["reasoning_demand"].observable


def test_absent_answer_is_unobservable_not_zero():
    payload = _primary_answers(**{"task_complexity": 0.0})
    obs = normalize_sensor_answers(payload, _spec(), ("task_complexity", "reasoning_demand"))
    # A real 0.0 IS observable ...
    assert obs["task_complexity"].observable
    # ... while a missing one is not, and must never read as 0.0.
    assert obs["reasoning_demand"].value is None
    assert obs["reasoning_demand"].value != 0.0


def test_generative_fallback_fabricated_one_is_refused():
    """The executor defaults an ABSENT numeric to 1.0 on the fallback leg.

    That default is correct for the guard (inflating a risk grade fails safe).
    On a sensor it would manufacture a maximal, confident observation out of
    nothing, so the leg is refused outright rather than believed.
    """
    payload = {
        "answers": {"task_complexity": {"type": "noul", "noul": 1.0}},
        "leg": "generative_fallback",
        "usage": None,
    }
    with pytest.raises(SensorUnavailable):
        normalize_sensor_answers(payload, _spec(), ("task_complexity",))


def test_missing_provider_is_refused_even_with_clean_looking_answers():
    """CONTROL for F2: a response with NO leg key at all must still be refused.

    On the real primary path the response is the upstream body plus
    ``contract``/``served_model`` and carries a ``provider`` but no ``leg``;
    a fabricated 1.0 on the fallback leg is carried by ``leg`` and no
    ``provider``. Refusing on a missing provider means a stripped/renamed
    ``leg`` cannot smuggle the fallback's fabricated default through.
    """
    payload = {
        "answers": {"task_complexity": {"type": "noul", "noul": 0.99}},
        "contract": SENSOR_CONTRACT,
    }
    with pytest.raises(SensorUnavailable):
        normalize_sensor_answers(payload, _spec(), ("task_complexity",))


def test_fallback_leg_with_mixed_case_or_padding_is_still_refused():
    for leg in ("generative_fallback", "Generative_Fallback", " generative_fallback "):
        payload = {
            "answers": {"task_complexity": {"type": "noul", "noul": 1.0}},
            "leg": leg,
        }
        with pytest.raises(SensorUnavailable):
            normalize_sensor_answers(payload, _spec(), ("task_complexity",))


def test_unparsed_answer_on_a_primary_leg_is_unobservable_not_guessed():
    """An ``unparsed`` answer is unobservable, on ANY leg.

    F2's refusal is keyed on the leg, so the per-answer type branch needs its
    own primary-leg case: the leg guard must not be what makes this pass.
    """
    payload = {
        "answers": {
            "task_complexity": {"type": "unparsed", "raw": "I think it is hard"}
        },
        "contract": SENSOR_CONTRACT,
        "provider": "Respan",
    }
    obs = normalize_sensor_answers(payload, _spec(), ("task_complexity",))
    assert obs["task_complexity"].value is None
    assert not obs["task_complexity"].observable


def test_unparsed_answers_on_the_fallback_leg_are_refused_entirely():
    payload = {
        "answers": {
            "task_complexity": {"type": "unparsed", "raw": "I think it is hard"}
        },
        "leg": "generative_fallback",
    }
    with pytest.raises(SensorUnavailable):
        normalize_sensor_answers(payload, _spec(), ("task_complexity",))


def test_untyped_answer_type_is_unobservable():
    """CONTROL: a type we do not understand must not be read as a score."""
    payload = {
        "answers": {
            "task_complexity": {"type": "probability", "value": 0.9},
        },
        "contract": SENSOR_CONTRACT,
        "provider": "Respan",
    }
    obs = normalize_sensor_answers(payload, _spec(), ("task_complexity",))
    assert obs["task_complexity"].value is None


def test_malformed_responses_raise_rather_than_degrade_to_empty():
    for bad in (
        "not-an-object",
        {},
        {"answers": []},
        {"answers": {}},
        {"error": {"message": "upstream"}},
        {"answers": {"q": {"type": "noul", "noul": 0.5}}, "contract": "other:v9"},
    ):
        with pytest.raises(SensorUnavailable):
            normalize_sensor_answers(bad, _spec(), ("q",))


def test_non_finite_graded_value_is_unobservable():
    payload = {
        "answers": {
            "task_complexity": {"type": "noul", "noul": "NaN"},
        },
        "contract": SENSOR_CONTRACT,
        "provider": "Respan",
    }
    obs = normalize_sensor_answers(payload, _spec(), ("task_complexity",))
    assert obs["task_complexity"].value is None


def test_out_of_range_graded_value_is_unobservable_not_clamped():
    """F4: 4.2 is NOT a confident 1.0, and a negative is NOT a confident 0.0.

    Clamping a grade the module cannot scale into a pole would manufacture the
    exact confident-reading-from-nothing this module forbids.
    """
    for bad in (4.2, -0.3, 1.0001):
        payload = {
            "answers": {"task_complexity": {"type": "noul", "noul": bad}},
            "contract": SENSOR_CONTRACT,
            "provider": "Respan",
        }
        obs = normalize_sensor_answers(payload, _spec(), ("task_complexity",))
        assert obs["task_complexity"].value is None, bad
        assert not obs["task_complexity"].observable, bad


def test_in_range_graded_value_is_kept_exactly():
    """CONTROL: the unobservable arm above is not passing because everything is."""
    payload = {
        "answers": {"task_complexity": {"type": "noul", "noul": 1.0}},
        "contract": SENSOR_CONTRACT,
        "provider": "Respan",
    }
    obs = normalize_sensor_answers(payload, _spec(), ("task_complexity",))
    assert obs["task_complexity"].value == 1.0


def test_confidence_is_the_mean_of_observable_signals():
    payload = _primary_answers(**{"task_complexity": 0.9, "tool_dependency": 0.5})
    obs = normalize_sensor_answers(payload, _spec(), SIGNALS)
    # mean of the two graded (0.9 + 0.5) / 2 -- NOT the max (0.9).
    assert aggregate_confidence(obs) == pytest.approx(0.7)


def test_confidence_is_none_when_nothing_was_observable():
    obs = {
        "a": Observation(None, None, "s", 0.0),
        "b": Observation(None, None, "s", 0.0),
    }
    assert aggregate_confidence(obs) is None


# --------------------------------------------------------------------------
# Snapshot contains no routing directive
# --------------------------------------------------------------------------


def test_snapshot_schema_has_no_routing_fields():
    """CONTROL: nothing in the snapshot can be applied to a routing decision.

    Asserted off the DATACLASS fields, not the hand-written ``to_dict`` keys, so
    a routing field added to the dataclass but omitted from ``to_dict`` cannot
    slip past.
    """
    import dataclasses

    from turnstone.core.sensor_layer import Observation, SensorSnapshot

    forbidden = {
        "model", "route", "target", "client", "alias",
        "reasoning_effort", "temperature", "thinking", "max_tokens",
    }
    payload = _primary_answers(**{"task_complexity": 0.8})
    obs = normalize_sensor_answers(payload, _spec(), ("task_complexity",))
    snap = SensorSnapshot(
        schema_version=SENSOR_SCHEMA_VERSION,
        observations=obs,
        confidence=0.8,
        provenance="test",
        latency_seconds=0.1,
    )
    fields = {f.name for f in dataclasses.fields(SensorSnapshot)}
    fields |= {f.name for f in dataclasses.fields(Observation)}
    assert not (forbidden & fields)
    assert not (forbidden & set(snap.to_dict()))


# --------------------------------------------------------------------------
# End-to-end against a local stand-in for /v1/decisions
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    response_body: dict = {}
    status: int = 200
    received: list = []  # captures what actually went onto the wire

    def log_message(self, *_a):  # noqa: D102 - silence the test server
        return

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        self.received.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
        raw = json.dumps(self.response_body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def _decisions_stub(body: dict, status: int = 200):
    """Stand in for /v1/decisions; always shut the server down on exit.

    The repo's conftest fails any test that leaves a background thread running,
    which is the right default; the server is closed in a finally block rather
    than the test being marked as an intentional leak.
    """
    _Handler.response_body = body
    _Handler.status = status
    _Handler.received = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _open(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sense_end_to_end_returns_snapshot(monkeypatch):
    body = _primary_answers(**{"task_complexity": 0.77})
    with _decisions_stub(body) as opener:
        monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", opener)
        snap = sense(
            base_url="http://switchyard.invalid",
            model="switchyard-smartfree-aux-turnstone",
            caps=TYPED_CAPS,
            evidence={"tool_calls": 12},
            signal_names=("task_complexity",),
        )
    assert snap.schema_version == SENSOR_SCHEMA_VERSION
    assert snap.value("task_complexity") == pytest.approx(0.77)
    assert "Respan" in snap.provenance
    assert "switchyard-decision:v1" in snap.provenance


def test_sense_fails_boringly_on_transport_error(monkeypatch):
    import urllib.request

    def _boom(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", _boom)
    with pytest.raises(SensorUnavailable):
        sense(
            base_url="http://switchyard.invalid",
            model="m",
            caps=TYPED_CAPS,
            evidence={"tool_calls": 1},
            signal_names=("task_complexity",),
        )


def test_sense_refuses_a_non_typed_backend():
    with pytest.raises(SensorUnavailable):
        sense(
            base_url="http://switchyard.invalid",
            model="gpt-6-luna",
            caps={},
            evidence={"tool_calls": 1},
            signal_names=("task_complexity",),
        )


def _open(server):
    real = urllib.request.urlopen

    def _fake(request, timeout=None):
        # Rewrite the host so the call lands on the stand-in server.
        request.full_url = f"http://127.0.0.1:{server.server_port}{request.selector}"
        return real(request, timeout=timeout)

    return _fake


def test_sense_sends_the_bounded_body_we_expect(monkeypatch):
    """F10: assert what actually went ONTO the wire, not just the return value.

    Checks the endpoint path, that the state is the bounded render (no nested
    content), and that every question sent is a noul with real criteria.
    """
    body = _primary_answers(**{"task_complexity": 0.5})
    with _decisions_stub(body) as opener:
        monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", opener)
        sense(
            base_url="http://switchyard.invalid",
            model="switchyard-smartfree-aux-turnstone",
            caps=TYPED_CAPS,
            evidence={"tool_calls": 3, "escalated": True},
            intent="fix the failing test",
            signal_names=("task_complexity", "tool_dependency"),
        )
    path, sent = _Handler.received[0]
    assert path == "/v1/decisions"
    assert sent["model"] == "switchyard-smartfree-aux-turnstone"
    assert "tool_calls: 3" in sent["state"]
    assert "Task intent: fix the failing test" in sent["state"]
    assert set(sent["questions"]) == {"task_complexity", "tool_dependency"}
    for q in sent["questions"].values():
        assert q["type"] == "noul"
        assert set(q["criteria"]) == {"true", "false"}


def test_sense_sends_no_nested_content_onto_the_wire(monkeypatch):
    """CONTROL for the body assertion: a refused payload sends nothing."""
    with _decisions_stub(_primary_answers(**{"task_complexity": 0.5})) as opener:
        monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", opener)
        with pytest.raises(SensorUnavailable):
            sense(
                base_url="http://switchyard.invalid",
                model="m",
                caps=TYPED_CAPS,
                evidence={"transcript": {"role": "user", "content": "secret"}},
                signal_names=("task_complexity",),
            )
    assert _Handler.received == []


@pytest.mark.parametrize("bad_base_url", ["not-a-url", ""])
def test_sense_malformed_base_url_fails_boringly(monkeypatch, bad_base_url):
    """F5: Request() construction was outside the try, so ValueError escaped."""
    with _decisions_stub(_primary_answers(**{"task_complexity": 0.5})) as opener:
        monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", opener)
        with pytest.raises(SensorUnavailable):
            sense(
                base_url=bad_base_url,
                model="m",
                caps=TYPED_CAPS,
                evidence={"tool_calls": 1},
                signal_names=("task_complexity",),
            )


def test_sense_non_mapping_evidence_fails_boringly(monkeypatch):
    with _decisions_stub(_primary_answers(**{"task_complexity": 0.5})) as opener:
        monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", opener)
        with pytest.raises(SensorUnavailable):
            sense(
                base_url="http://switchyard.invalid",
                model="m",
                caps=TYPED_CAPS,
                evidence=["not", "a", "mapping"],  # type: ignore[arg-type]
                signal_names=("task_complexity",),
            )


def test_sense_never_returns_a_partial_snapshot(monkeypatch):
    """A response where SOME signals are missing still returns a snapshot, and
    the missing ones are unobservable rather than absent or zero."""
    body = {"answers": {"task_complexity": {"type": "noul", "noul": 0.3}},
            "contract": SENSOR_CONTRACT, "provider": "Respan"}
    with _decisions_stub(body) as opener:
        monkeypatch.setattr("turnstone.core.sensor_layer.urllib.request.urlopen", opener)
        snap = sense(
            base_url="http://switchyard.invalid",
            model="m",
            caps=TYPED_CAPS,
            evidence={"tool_calls": 1},
            signal_names=("task_complexity", "tool_dependency", "route_sufficiency"),
        )
    assert snap.value("task_complexity") == pytest.approx(0.3)
    for missing in ("tool_dependency", "route_sufficiency"):
        assert snap.value(missing) is None
        assert not snap.is_observable(missing)
