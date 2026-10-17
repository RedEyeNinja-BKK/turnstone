"""Behavioural regression for the advisory-loop ``user_interjection`` seam.

`Session.send()` folds a queued mid-turn message into a first-class
``role="system"`` turn rather than starting a new send, so the send seam
(which observes ``user_input``) never sees it. The advisory loop in
`_append_pending_system_turns` is therefore the ONLY place that text is
observed.

Unlike the placement assertion in ``test_sensor_interjection_seam.py``, these
tests drive the real production methods on a real ``ChatSession``:
``_collect_advisories`` builds the spec, ``_append_pending_system_turns``
appends it and emits the observation. Nothing here re-implements the loop.
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase

RAW = "Also make sure the transport deadline is enforced on retries."


class _NullUI(SessionUIBase):
    """Minimal UI satisfying the SessionUIBase contract."""


def _registry():
    from turnstone.core.model_registry import ModelConfig, ModelRegistry

    return ModelRegistry(
        models={
            "tier1-decision": ModelConfig(
                alias="tier1-decision",
                base_url="https://switchyard.example.internal",
                api_key="synthetic-not-a-credential",
                model="synthetic-model",
                provider="openrouter",
                capabilities={"supports_typed_decision": True},
            )
        },
        default="tier1-decision",
    )


def _session():
    return ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        registry=_registry(),
    )


@pytest.fixture()
def observed(monkeypatch):
    """Record (event, objective) without touching the network or the store."""
    calls: list[tuple[str, str]] = []
    real = lifecycle.observe_event

    def _record(event, state=None, *, force=False, workstream="", registry=None):
        calls.append((event, str((state or {}).get("objective", ""))))
        return real(event, state, force=force, workstream=workstream, registry=registry)

    monkeypatch.setattr(lifecycle, "observe_event", _record)
    monkeypatch.setattr(lifecycle, "ENABLED", True)
    lifecycle.reset_hooks()
    return calls


def _advisory_specs(session):
    """Drain the queue through the real advisory collector (last-in-batch)."""
    return session._collect_advisories(
        assessment=None, func_name="bash", is_last_in_batch=True
    )


def test_appended_interjection_invokes_the_sensor_exactly_once(observed):
    session = _session()
    session.queue_message(RAW, queue_msg_id="q1")

    specs = _advisory_specs(session)
    assert [s[0] for s in specs] == ["user_interjection"]

    before = len(session.messages)
    session._append_pending_system_turns(specs, deferred_persistence=[])

    assert len(observed) == 1, observed
    assert observed[0][0] == "operator_instruction"
    # The turn really landed in the trajectory as a system turn.
    assert len(session.messages) == before + 1
    appended = session.messages[-1]
    assert appended.role == "system"
    assert appended.source == "user_interjection"


def test_failed_append_generates_no_observation(observed, monkeypatch):
    """A spec that never reaches the model is not an instruction it received."""
    session = _session()
    session.queue_message(RAW, queue_msg_id="q1")
    specs = _advisory_specs(session)

    def _explode(*args, **kwargs):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(session, "_append_system_turn", _explode)

    with pytest.raises(RuntimeError):
        session._append_pending_system_turns(specs, deferred_persistence=[])

    assert observed == [], "an unappended interjection must not be observed"


def test_raw_operator_message_is_observed_not_the_framing(observed):
    session = _session()
    session.queue_message(RAW, queue_msg_id="q1")
    specs = _advisory_specs(session)

    _source, framed, meta = specs[0]
    assert meta["message"] == RAW
    # The wire text is deliberately NOT the raw words.
    assert framed != RAW
    assert RAW in framed

    session._append_pending_system_turns(specs, deferred_persistence=[])

    assert observed == [("operator_instruction", RAW)], (
        "the sensor must receive the operator's words, not the model-facing "
        "preamble the system turn carries"
    )


def test_content_free_interjection_emits_nothing(observed):
    """A bare priority marker renders no turn, so there is no instruction."""
    session = _session()
    session.queue_message("!!!", queue_msg_id="q1")

    specs = _advisory_specs(session)
    session._append_pending_system_turns(specs, deferred_persistence=[])

    assert specs == []
    assert observed == []


def test_sensor_exception_is_fail_open_and_the_turn_still_appends(observed, monkeypatch):
    session = _session()
    session.queue_message(RAW, queue_msg_id="q1")
    specs = _advisory_specs(session)
    before = len(session.messages)

    def _explode(*args, **kwargs):
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(lifecycle, "observe_event", _explode)

    session._append_pending_system_turns(specs, deferred_persistence=[])

    assert len(session.messages) == before + 1, "the turn must survive a sensor failure"


def test_advisory_and_flush_seams_do_not_double_observe(observed):
    """The advisory pop REMOVES the row, so the flush seam cannot see it again."""
    session = _session()
    session.queue_message(RAW, queue_msg_id="q1")

    specs = _advisory_specs(session)
    session._append_pending_system_turns(specs, deferred_persistence=[])

    # The queue was drained by the advisory pop; a later flush finds nothing.
    # The prepare/apply pair is the seam the observation sits in; the public
    # wrapper only adds commit discipline (and refuses a session whose
    # workstream was never persisted, which is how this fixture is built).
    assert session._queued_messages == {}
    plan = session._prepare_queued_flush()
    assert session._apply_queued_flush(plan, []) is False

    assert len(observed) == 1, f"the same message was observed twice: {observed}"


def test_a_distinct_later_message_is_observed_by_the_flush_seam(observed):
    """Sanity counterpart: suppression comes from the pop, not from a lockout."""
    session = _session()
    session.queue_message(RAW, queue_msg_id="q1")
    specs = _advisory_specs(session)

    session.queue_message("A second, different instruction arrived later.",
                          queue_msg_id="q2")
    session._append_pending_system_turns(specs, deferred_persistence=[])

    plan = session._prepare_queued_flush()
    assert session._apply_queued_flush(plan, []) is True
    assert len(observed) == 2, observed
    assert "second, different instruction" in observed[1][1]
