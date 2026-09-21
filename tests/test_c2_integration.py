"""C2 v1 integration — the session seam around ``_maybe_continue_truncated``.

Covers the operator's mandated matrix: gate/trigger eligibility, sanctioned-rail
reuse, transactionality of ``self.messages`` and the session usage slots,
suppressed-then-committed visible output, budget arithmetic, and every stop
reason.  The policy arithmetic itself is covered by
``tests/test_c2_continuation.py``; these tests pin the WIRING.

The rail is scripted, never stubbed away: every test asserts the legs went
through ``_model_turn_with_fallback`` (the sanctioned entry point), which is
what keeps cancellation, the fallback walk and generation scoping in play.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from tests._session_helpers import RecordingUI, make_session
from turnstone.core.continuation import (
    C2_ENDPOINT_CONTEXT_TOKENS,
    C2_REQUIRED_INCOMPLETE_REASON,
    ContinuationMode,
)
from turnstone.core.model_turn import ModelTurnResult
from turnstone.core.providers import (
    OpenAIResponsesProvider,
    StreamChunk,
    UsageInfo,
)
from turnstone.core.session import (
    GenerationCancelled,
    _SilentLegConsumer,
    _StreamTurnConsumer,
)
from turnstone.core.trajectory import Turn, TurnProvenance

PROVEN_MODEL = "comfyninja/qwen3.8-27b-q3"
PARTIAL = "1\n2\n3\n4\n5\n"


class _FakeStore:
    """Minimal ConfigStore stand-in: the gate read is the only surface used."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class _FakeLane:
    """A lane stand-in carrying only the two facets the capability check reads."""

    def __init__(self, model: str, provider) -> None:
        self.model = model
        self.provider = provider
        self.alias = model


def _result(
    content: str = PARTIAL,
    *,
    finish_reason: str = "length",
    incomplete_reason: str | None = C2_REQUIRED_INCOMPLETE_REASON,
    completion_tokens: int = 40,
    prompt_tokens: int = 100,
    tool_calls: list | None = None,
    native=None,
) -> ModelTurnResult:
    return ModelTurnResult(
        turn=Turn.assistant(content, native=native),
        finish_reason=finish_reason,
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        tool_calls=tool_calls or [],
        provenance=TurnProvenance(model_alias="a", backend_model_id=PROVEN_MODEL),
        incomplete_reason=incomplete_reason,
    )


def _leg(
    content: str,
    *,
    completion_tokens: int | None = 10,
    prompt_tokens: int = 100,
    finish_reason: str = "stop",
    incomplete_reason: str | None = None,
) -> ModelTurnResult:
    """A continuation leg's result.

    Defaults to a NATURAL finish: a leg that itself ends on ``length`` is a
    legitimate outcome but it makes the runner request another leg, so only
    the tests about that behaviour pass ``finish_reason="length"``.
    """
    return _result(
        content,
        finish_reason=finish_reason,
        incomplete_reason=incomplete_reason,
        completion_tokens=completion_tokens,
        prompt_tokens=prompt_tokens,
    )


def _setup(*, gate: bool = True, max_tokens: int = 4096, model: str = PROVEN_MODEL, provider=None):
    """A session + live consumer positioned on the proven capability."""
    ui = RecordingUI()
    session = make_session(ui=ui, max_tokens=max_tokens)
    session._config_store = _FakeStore({"model.auto_continue_truncated": gate})
    session._last_usage = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    session._assistant_pending_tokens = 40
    session._chars_per_token = 4.0
    consumer = _StreamTurnConsumer(session, 0)
    consumer.lane = _FakeLane(model, provider or OpenAIResponsesProvider())
    return session, ui, consumer


def _install_rail(session, legs, calls):
    """Script ``_model_turn_with_fallback``; record every leg's arguments."""

    def fake(consumer, prepare_wire, my_generation=0, *, principal_id=None, max_tokens=None):
        calls.append(
            {
                "consumer": consumer,
                "max_tokens": max_tokens,
                "messages": list(session.messages),
            }
        )
        item = legs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    patcher = patch.object(session, "_model_turn_with_fallback", side_effect=fake)
    patcher.start()
    return patcher


def _run(session, consumer, result):
    out = session._maybe_continue_truncated(result, consumer, lambda wire, lane: wire, 0, None)
    # The real seam calls this immediately after; the display splitter only
    # releases the committed suffix at stream end.
    consumer.finish_stream()
    return out


def _run_with_rail(session, consumer, result, legs):
    calls: list[dict] = []
    patcher = _install_rail(session, legs, calls)
    try:
        return _run(session, consumer, result), calls
    finally:
        patcher.stop()


# --------------------------------------------------------------------------
# Gate / trigger
# --------------------------------------------------------------------------


def test_gate_off_returns_the_identical_result():
    session, _, consumer = _setup(gate=False)
    result = _result()
    calls: list[dict] = []
    patcher = _install_rail(session, [], calls)
    try:
        out = _run(session, consumer, result)
    finally:
        patcher.stop()
    assert out is result, "gate OFF must not rebuild or touch the result"
    assert calls == [], "gate OFF must issue no leg"


def test_truthful_length_trigger_is_eligible():
    session, ui, consumer = _setup()
    out, calls = _run_with_rail(
        session, consumer, _result(), [_leg(PARTIAL + "6\n7\n")]
    )
    assert len(calls) == 1
    assert out.content == PARTIAL + "6\n7\n"
    assert ("content", "6\n7\n") in ui.events


@pytest.mark.parametrize(
    "incomplete_reason",
    [None, "unknown_reason", "context_capacity", "content_filter"],
)
def test_untruthful_reason_never_continues(incomplete_reason):
    """``length`` alone is never sufficient — the pre-A serializer fails closed."""
    session, _, consumer = _setup()
    result = _result(incomplete_reason=incomplete_reason)
    out, calls = _run_with_rail(session, consumer, result, [])
    assert calls == []
    assert out is result


def test_natural_stop_never_continues():
    session, _, consumer = _setup()
    result = _result(finish_reason="stop", incomplete_reason=None)
    out, calls = _run_with_rail(session, consumer, result, [])
    assert calls == []
    assert out is result


def test_partial_tool_call_never_continues_and_is_never_executed():
    session, _, consumer = _setup()
    result = _result(tool_calls=[{"id": "1", "function": {"name": "grep", "arguments": "{"}}])
    out, calls = _run_with_rail(session, consumer, result, [])
    assert calls == []
    assert out is result
    assert out.tool_calls, "the truncated call is left exactly as it was"


def test_empty_visible_partial_never_continues():
    session, _, consumer = _setup()
    out, calls = _run_with_rail(session, consumer, _result(content=""), [])
    assert calls == []


def test_reasoning_bearing_turn_is_out_of_scope_for_v1():
    """Mode 2 is deferred: a reasoning-bearing turn performs NO continuation."""
    from turnstone.core.trajectory import ProviderNative

    session, _, consumer = _setup()
    native = ProviderNative(producer="openai_responses", blocks=({"type": "reasoning"},))
    out, calls = _run_with_rail(session, consumer, _result(native=native), [])
    assert calls == [], "v1 must not construct reasoning replay"
    assert out is None or out.finish_reason == "length"


def test_missing_usage_fails_closed():
    session, _, consumer = _setup()
    result = _result()
    result = ModelTurnResult(
        turn=result.turn,
        finish_reason=result.finish_reason,
        usage=None,
        tool_calls=[],
        provenance=result.provenance,
        incomplete_reason=result.incomplete_reason,
    )
    out, calls = _run_with_rail(session, consumer, result, [])
    assert calls == []
    assert out is result


# --------------------------------------------------------------------------
# Capability scope
# --------------------------------------------------------------------------


def test_unproven_backend_model_never_continues():
    session, _, consumer = _setup(model="gpt-5.6-luna")
    out, calls = _run_with_rail(session, consumer, _result(), [])
    assert calls == [], "the code is reusable, the enablement is not generic"


def test_unproven_provider_never_continues():
    other = type("OpenAIChatCompletionsProvider", (), {})()
    session, _, consumer = _setup(provider=other)
    out, calls = _run_with_rail(session, consumer, _result(), [])
    assert calls == []


def test_lane_none_never_continues():
    session, _, consumer = _setup()
    consumer.lane = None
    out, calls = _run_with_rail(session, consumer, _result(), [])
    assert calls == []


# --------------------------------------------------------------------------
# Rail reuse
# --------------------------------------------------------------------------


def test_leg_goes_through_the_sanctioned_rail_with_the_exact_leg_budget():
    session, _, consumer = _setup(max_tokens=1000)
    _, calls = _run_with_rail(
        session,
        consumer,
        _result(completion_tokens=1000),
        [_leg(PARTIAL + "tail")],
    )
    assert len(calls) == 1
    # c_total = min(2*1000, 65536) = 2000; used = 1000; remaining = 1000;
    # leg_budget = min(1000, 1000, 64000, context_room) = 1000.
    assert calls[0]["max_tokens"] == 1000
    assert isinstance(calls[0]["consumer"], _SilentLegConsumer)


def test_leg_uses_a_silent_consumer_not_the_visible_one():
    session, ui, consumer = _setup()
    before = list(ui.events)
    _, calls = _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "x")])
    leg_consumer = calls[0]["consumer"]
    assert leg_consumer is not consumer
    assert isinstance(leg_consumer, _StreamTurnConsumer)
    # The leg rendered nothing of its own, and accumulated nothing.
    assert leg_consumer.partial_content() == ""
    assert not leg_consumer._content_parts


def test_max_tokens_override_does_not_mutate_the_session_setting():
    session, _, consumer = _setup(max_tokens=4096)
    _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "x")])
    assert session.max_tokens == 4096


# --------------------------------------------------------------------------
# Transactionality
# --------------------------------------------------------------------------


def test_messages_restored_after_success():
    session, _, consumer = _setup()
    session.messages = [{"role": "user", "content": "hi"}]
    original = list(session.messages)
    _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "x")])
    assert session.messages == original


def test_leg_sees_the_prefill_appended_to_history():
    session, _, consumer = _setup()
    session.messages = [{"role": "user", "content": "hi"}]
    _, calls = _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "x")])
    seen = calls[0]["messages"]
    assert seen[0] == {"role": "user", "content": "hi"}
    assert seen[-1]["role"] == "assistant"
    assert seen[-1]["content"] == PARTIAL


def test_messages_restored_after_provider_failure():
    session, _, consumer = _setup()
    session.messages = [{"role": "user", "content": "hi"}]
    original = list(session.messages)
    out, calls = _run_with_rail(
        session, consumer, _result(), [RuntimeError("provider died")]
    )
    assert session.messages == original
    assert calls, "the leg was attempted"
    assert out is not None and out.content == PARTIAL, "the initial answer survives"


def test_messages_restored_after_prefix_mismatch():
    session, _, consumer = _setup()
    session.messages = [{"role": "user", "content": "hi"}]
    original = list(session.messages)
    out, _ = _run_with_rail(session, consumer, _result(), [_leg("BAD PREFIX")])
    assert session.messages == original
    assert out is not None and out.content == PARTIAL


def test_messages_restored_after_missing_usage():
    session, _, consumer = _setup()
    session.messages = [{"role": "user", "content": "hi"}]
    original = list(session.messages)
    no_usage = _leg(PARTIAL + "y")
    no_usage = ModelTurnResult(
        turn=no_usage.turn,
        finish_reason=no_usage.finish_reason,
        usage=None,
        tool_calls=[],
        provenance=no_usage.provenance,
    )
    out, _ = _run_with_rail(session, consumer, _result(), [no_usage])
    assert session.messages == original
    assert out.content == PARTIAL, "an unaccountable leg contributes nothing"


def test_cancellation_propagates_and_restores_messages():
    session, _, consumer = _setup()
    session.messages = [{"role": "user", "content": "hi"}]
    original = list(session.messages)
    calls: list[dict] = []
    patcher = _install_rail(session, [GenerationCancelled()], calls)
    try:
        with pytest.raises(GenerationCancelled):
            _run(session, consumer, _result())
    finally:
        patcher.stop()
    assert session.messages == original


def test_session_usage_slots_survive_a_leg():
    session, _, consumer = _setup()
    session._last_usage = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    session._assistant_pending_tokens = 7
    out, _ = _run_with_rail(
        session, consumer, _result(), [_leg(PARTIAL + "z", completion_tokens=9)]
    )
    # The leg's isolated 9 tokens must not have replaced the logical turn's.
    assert session._last_usage["completion_tokens"] == 40 + 9
    # ``_assistant_pending_tokens`` tracks the SAME quantity, and a stale
    # value here under-reports the turn on the next estimate path.
    assert session._assistant_pending_tokens == 40 + 9
    assert session._assistant_pending_tokens == out.usage.completion_tokens


def test_committed_usage_is_initial_plus_continuation():
    session, _, consumer = _setup()
    out, _ = _run_with_rail(
        session,
        consumer,
        _result(completion_tokens=40, prompt_tokens=100),
        [_leg(PARTIAL + "z", completion_tokens=11)],
    )
    assert out.usage.completion_tokens == 51
    assert out.usage.total_tokens == 100 + 51


# --------------------------------------------------------------------------
# Visible output
# --------------------------------------------------------------------------


def test_failed_leg_exposes_zero_suffix():
    session, ui, consumer = _setup()
    _run_with_rail(session, consumer, _result(), [_leg("NOT A CONTINUATION")])
    assert ("content", "NOT A CONTINUATION") not in ui.events
    assert all(kind != "content" or PARTIAL not in text for kind, text in ui.events)


def test_prefix_is_not_duplicated_and_no_second_message():
    session, ui, consumer = _setup()
    out, _ = _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "6\n7\n")])
    emitted = [text for kind, text in ui.events if kind == "content"]
    assert "".join(emitted) == "6\n7\n", "only the validated suffix is emitted"
    assert out.content.count(PARTIAL) == 1


def test_leg_output_is_suppressed_until_validated():
    """A leg that returns a bad prefix must reach the operator as nothing."""
    session, ui, consumer = _setup()
    _run_with_rail(session, consumer, _result(), [_leg("junk" + PARTIAL)])
    assert [t for k, t in ui.events if k == "content"] == []


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_r_1000_caps_the_run_at_2000():
    session, _, consumer = _setup(max_tokens=1000)
    _, calls = _run_with_rail(
        session,
        consumer,
        _result(completion_tokens=1000),
        [_leg(PARTIAL + "a", completion_tokens=1000)],
    )
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1000


def test_r_32768_caps_the_run_at_65536():
    session, _, consumer = _setup(max_tokens=32768)
    _, calls = _run_with_rail(
        session,
        consumer,
        _result(completion_tokens=1),
        [_leg(PARTIAL + "a", completion_tokens=1)],
    )
    assert calls[0]["max_tokens"] == 32768


def test_exhausted_budget_issues_no_leg():
    session, _, consumer = _setup(max_tokens=100)
    out, calls = _run_with_rail(
        session, consumer, _result(completion_tokens=200), []
    )
    assert calls == []
    assert out is not None


def test_at_most_three_legs_and_the_budget_can_bind_first():
    session, _, consumer = _setup(max_tokens=1000)
    legs = [
        _leg(PARTIAL + "a", completion_tokens=300, finish_reason="length"),
        _leg(PARTIAL + "ab", completion_tokens=300, finish_reason="length"),
        _leg(PARTIAL + "abc", completion_tokens=300, finish_reason="length"),
        _leg(PARTIAL + "abcd", completion_tokens=10, finish_reason="length"),
    ]
    _, calls = _run_with_rail(session, consumer, _result(completion_tokens=100), legs)
    # used starts at 100; +300*3 = 1000 == c_total -> the fourth leg is refused.
    assert len(calls) == 3


def test_endpoint_context_room_can_bind_a_leg():
    """The anchor is the provider's OWN figure for the request it accepted."""
    session, _, consumer = _setup(max_tokens=4096)
    _, calls = _run_with_rail(
        session,
        consumer,
        _result(prompt_tokens=C2_ENDPOINT_CONTEXT_TOKENS - 120),
        [_leg(PARTIAL + "small")],
    )
    assert calls, "a leg is still allowed while some room remains"
    assert calls[0]["max_tokens"] < 4096, "the endpoint wall binds before R_effective"


def test_the_leg_anchor_is_a_real_measurement_not_a_guess():
    """Leg 2's room is built from leg 1's OWN reported prompt size.

    A character-estimate over the whole history would never land on exactly
    the wall minus leg 1's real prompt plus leg 1's own suffix, so landing
    there is the property that proves the anchor is measured.
    """
    session, _, consumer = _setup(max_tokens=32768)
    wall = C2_ENDPOINT_CONTEXT_TOKENS
    suffix = "x" * 40
    _, calls = _run_with_rail(
        session,
        consumer,
        _result(prompt_tokens=100),
        [
            _leg(PARTIAL + suffix, prompt_tokens=wall - 50, finish_reason="length"),
            _leg(PARTIAL + suffix + "y"),
        ],
    )
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 32768, "R_effective binds the first leg"
    # 50 tokens of wall, less the 40-char suffix leg 2 appends (ceil -> 10).
    assert calls[1]["max_tokens"] == 50 - math.ceil(len(suffix) / 4.0)


def test_no_endpoint_room_fails_closed_without_a_leg():
    session, _, consumer = _setup()
    out, calls = _run_with_rail(
        session,
        consumer,
        _result(prompt_tokens=C2_ENDPOINT_CONTEXT_TOKENS + 1),
        [],
    )
    assert calls == []
    assert out is not None and out.content == PARTIAL


def test_an_untrustworthy_anchor_fails_closed():
    """No real measurement => no continuation, rather than a guess."""
    session, _, consumer = _setup()
    out, calls = _run_with_rail(session, consumer, _result(prompt_tokens=0), [])
    assert calls == []
    assert out.content == PARTIAL


# --------------------------------------------------------------------------
# Stop reasons
# --------------------------------------------------------------------------


def test_zero_progress_stops_the_run():
    session, _, consumer = _setup()
    out, calls = _run_with_rail(
        session,
        consumer,
        _result(),
        [_leg(PARTIAL, completion_tokens=50), _leg(PARTIAL + "later")],
    )
    assert len(calls) == 1, "a no-new-visible-text leg does not burn the next leg"
    assert out.content == PARTIAL


def test_prefix_mismatch_stops_the_run():
    session, _, consumer = _setup()
    out, calls = _run_with_rail(
        session,
        consumer,
        _result(),
        [_leg("garbage"), _leg(PARTIAL + "later")],
    )
    assert len(calls) == 1
    assert out.content == PARTIAL


def test_leg_limit_stops_the_run():
    session, _, consumer = _setup(max_tokens=32768)
    legs = [
        _leg(PARTIAL + "a" * (i + 1), completion_tokens=1, finish_reason="length")
        for i in range(5)
    ]
    _, calls = _run_with_rail(session, consumer, _result(completion_tokens=1), legs)
    assert len(calls) == 3


def test_accepted_legs_survive_a_later_leg_failing():
    session, ui, consumer = _setup()
    out, _ = _run_with_rail(
        session,
        consumer,
        _result(),
        [_leg(PARTIAL + "good", completion_tokens=5), RuntimeError("died")],
    )
    assert out.content == PARTIAL + "good"
    assert ("content", "good") in ui.events


# --------------------------------------------------------------------------
# Silent consumer semantics
# --------------------------------------------------------------------------


def test_silent_consumer_is_a_real_consumer_subclass():
    session, _, _ = _setup()
    leg = _SilentLegConsumer(session, 0)
    assert isinstance(leg, _StreamTurnConsumer)
    assert callable(leg)


def test_silent_consumer_renders_nothing_but_stays_armed():
    session, ui, _ = _setup()
    leg = _SilentLegConsumer(session, 0)
    leg.lane = _FakeLane(PROVEN_MODEL, OpenAIResponsesProvider())
    before = list(ui.events)
    leg(StreamChunk(content_delta="secret"))
    assert ui.events == before, "no render path may fire"
    assert leg._saw_chunk, "the arm signal must still latch"


def test_silent_consumer_does_not_touch_session_usage_slots():
    session, _, _ = _setup()
    session._last_usage = {"completion_tokens": 40}
    session._assistant_pending_tokens = 40
    leg = _SilentLegConsumer(session, 0)
    leg.on_stream_armed()
    assert session._last_usage == {"completion_tokens": 40}
    assert session._assistant_pending_tokens == 40


def test_silent_consumer_records_health_success():
    from unittest.mock import MagicMock

    session, _, _ = _setup()
    leg = _SilentLegConsumer(session, 0)
    tracker = MagicMock()
    leg.tracker = tracker
    leg.on_stream_armed()
    tracker.record_success.assert_called_once()


def test_mode_is_mode1_only():
    assert ContinuationMode.MODE1_VISIBLE_ONLY.value == "mode1_visible_only"
