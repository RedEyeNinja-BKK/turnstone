"""Phase-3 combined-seam tests — C0 x C2 x the production replay/compaction work.

Why this file exists
--------------------
The C candidate was cut from an older base than the DEPLOYED production
lineage, so three pieces of machinery meet for the first time on the
convergence branch:

* **C0** — the Responses provider preserving the endpoint's own
  ``incomplete_details.reason`` verbatim on the terminal chunk, carried
  through ``drain_stream`` into ``CompletionResult`` / ``ModelTurnResult``.
* **C2** — bounded, automatic continuation of a *truthfully* truncated
  Mode-1 answer, gated on that reason.
* **production's post-branch work** — the in-round Responses
  reasoning-replay repair (``ensure_responses_round_reasoning_items``),
  ordered phase replay, and the in-band compaction-summary history shape.

Each of those is covered by its own side of the tree; nothing covered the
INTERACTIONS, which is exactly where a merge of two independently-tested
lineages breaks. These tests pin the interactions only — no policy
arithmetic is re-litigated here (that lives in ``test_c2_continuation.py``
and ``test_c2_integration.py``).

Sections
  A  C0's terminal reason crossed with the round-repair / phase replay.
  B  The C2 trigger crossed with that replay state: request-side replay
     artifacts must never be visible to continuation eligibility.
  C  Compaction/reopen crossed with continuation: neither arms the gate, and
     history shape never changes eligibility, the leg budget, or the
     transactionality of the session's own state.
  D  Gate-OFF parity: with the feature off, the request shape and the
     session's history are exactly production's.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import RecordingUI, make_session
from turnstone.core.continuation import C2_REQUIRED_INCOMPLETE_REASON
from turnstone.core.history_decoration import ROUND_REASONING_PLACEHOLDER
from turnstone.core.model_turn import ModelTurnResult
from turnstone.core.providers import (
    OpenAIResponsesProvider,
    UsageInfo,
    drain_stream,
)
from turnstone.core.providers._openai_responses import (
    _RESPONSES_ROUND_REASONING_ID_PREFIX,
)
from turnstone.core.session import _StreamTurnConsumer
from turnstone.core.trajectory import ProviderNative, Turn, TurnProvenance

PROVEN_MODEL = "comfyninja/qwen3.8-27b-q3-mtp"
PARTIAL = "1\n2\n3\n4\n5\n"
COMPACTION_MARKER = "[Conversation summary] user asked X; assistant replied Y."

#: Sentinel for "the terminal event carries no ``incomplete_details`` at all" —
#: distinct from ``None``, which is a present-but-empty details value.
_ABSENT = object()


# ── provider-side (wire) helpers ─────────────────────────────────────────────


def _text_events(text: str) -> list[Any]:
    """The minimal truncated-run body: visible deltas + one completed item."""
    item = SimpleNamespace(type="message", content=[])
    item.model_dump = lambda **_kw: {  # type: ignore[method-assign]
        "type": "message",
        "content": [{"type": "output_text", "text": text}],
    }
    return [
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(type="response.output_item.done", item=item),
    ]


def _terminal(*, status: str, reason: Any = _ABSENT, output: list | None = None) -> Any:
    """A terminal Responses event; ``reason`` is passed through to
    ``incomplete_details`` exactly as given (sentinel = omit the key)."""
    usage = SimpleNamespace(
        input_tokens=942,
        output_tokens=4096,
        total_tokens=5038,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    kwargs: dict[str, Any] = {"status": status, "usage": usage}
    if output is not None:
        kwargs["output"] = output
    if reason is not _ABSENT:
        kwargs["incomplete_details"] = reason
    return SimpleNamespace(
        type="response.completed" if status == "completed" else "response.incomplete",
        response=SimpleNamespace(**kwargs),
    )


def _drain(events: list[Any]):
    provider = OpenAIResponsesProvider()
    client = MagicMock()
    client.responses.create.return_value = events
    return drain_stream(
        provider.create_streaming(
            client=client,
            model="gpt-5.1",
            messages=[{"role": "user", "content": "hi"}],
            capabilities=None,
        )
    )


def _round_reasoning_items(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("type") == "reasoning"]


def _lower(items: list[dict], *, replay: bool):
    """The production lowering entry point, unbound so no client state is needed."""
    provider = object.__new__(OpenAIResponsesProvider)
    return provider._convert_messages(items, replay_reasoning_to_model=replay)


def _failing_round() -> list[dict]:
    """The production shape the in-round repair exists for, in CANONICAL chat
    form (what a session actually stores): the resumed round's tool-call turn
    carries no reasoning item — the measured 400 reproducer."""
    return [
        {"role": "user", "content": "Check the service status."},
        {"role": "assistant", "content": "I will check."},
        {"role": "user", "content": COMPACTION_MARKER},
        {"role": "assistant", "content": "Backend request failed before generation started."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"state":"active"}'},
    ]


# ── session-side helpers (mirrors test_c2_integration.py) ────────────────────


class _FakeStore:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class _FakeLane:
    def __init__(self, model: str, provider) -> None:
        self.model = model
        self.provider = provider
        self.alias = model
        # Declared serving identity, as the real lane factory captures it;
        # "" when the provider declares nothing.
        self.provider_name = getattr(provider, "provider_name", "") or ""


def _result(
    content: str = PARTIAL,
    *,
    finish_reason: str = "length",
    incomplete_reason: str | None = C2_REQUIRED_INCOMPLETE_REASON,
    completion_tokens: int = 40,
    prompt_tokens: int = 942,
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
        tool_calls=[],
        provenance=TurnProvenance(model_alias="a", backend_model_id=PROVEN_MODEL),
        incomplete_reason=incomplete_reason,
    )


def _leg(content: str, *, completion_tokens: int = 10, prompt_tokens: int = 1000) -> ModelTurnResult:
    return _result(
        content,
        finish_reason="stop",
        incomplete_reason=None,
        completion_tokens=completion_tokens,
        prompt_tokens=prompt_tokens,
    )


def _setup(*, gate: bool = True, history: list | None = None, max_tokens: int = 4096):
    ui = RecordingUI()
    session = make_session(ui=ui, max_tokens=max_tokens)
    session._config_store = _FakeStore({"model.auto_continue_truncated": gate})
    session._last_usage = {"prompt_tokens": 942, "completion_tokens": 40, "total_tokens": 982}
    session._assistant_pending_tokens = 40
    session._chars_per_token = 4.0
    session.messages = list(history) if history is not None else []
    consumer = _StreamTurnConsumer(session, 0)
    consumer.lane = _FakeLane(PROVEN_MODEL, OpenAIResponsesProvider())
    return session, ui, consumer


def _install_rail(session, legs: list, calls: list):
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


def _run_with_rail(session, consumer, result, legs: list):
    calls: list[dict] = []
    patcher = _install_rail(session, legs, calls)
    try:
        out = session._maybe_continue_truncated(result, consumer, lambda wire, lane: wire, 0, None)
        consumer.finish_stream()
    finally:
        patcher.stop()
    return out, calls


OBSERVED_COMPACTED_HISTORY = [
    {"role": "user", "content": "Check the service status."},
    {"role": "assistant", "content": "I will check."},
    {"role": "user", "content": COMPACTION_MARKER},
]

OBSERVED_LONG_HISTORY = OBSERVED_COMPACTED_HISTORY + [
    {"role": "assistant", "content": "Backend request failed before generation started."},
    {"role": "user", "content": "try again"},
    {"role": "assistant", "content": "retrying now"},
]


# ═══════════════════════════════════════════════════════════════════════════
# A. C0 (terminal reason) crossed with the production replay apparatus
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("replay", [False, True])
def test_a1_round_repair_items_and_the_terminal_reason_coexist(replay):
    """BOTH properties in one run: the in-round replay repair still synthesizes
    its item (when the wire's replay flag allows it), while C0 carries the
    endpoint's reason through the same drain.

    ``replay=False`` is the cloud/no-flag posture: the item list must stay
    exactly as it is today, and the reason is still captured — C0 does not
    depend on replay being enabled.
    """
    _, items = _lower(_failing_round(), replay=replay)
    synthesized = _round_reasoning_items(items)
    if replay:
        assert len(synthesized) == 1, "the in-round replay repair must survive convergence"
        assert synthesized[0]["id"].startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)
        assert synthesized[0]["content"][0]["text"] == ROUND_REASONING_PLACEHOLDER
    else:
        assert synthesized == [], "no flag => no synthesized item (cloud posture)"

    result = _drain(
        [
            *_text_events(PARTIAL),
            _terminal(
                status="incomplete",
                reason=SimpleNamespace(reason=C2_REQUIRED_INCOMPLETE_REASON),
            ),
        ]
    )
    assert result.finish_reason == "length"
    assert result.incomplete_reason == C2_REQUIRED_INCOMPLETE_REASON
    assert result.content == PARTIAL
    assert [b["type"] for b in result.provider_blocks] == ["message"]


@pytest.mark.parametrize(
    "details",
    [
        SimpleNamespace(reason=C2_REQUIRED_INCOMPLETE_REASON),
        {"reason": C2_REQUIRED_INCOMPLETE_REASON},
    ],
    ids=["sdk-object", "plain-dict"],
)
def test_a2_both_details_shapes_are_read_verbatim(details):
    """The SDK hands back an object, but a dict is equally admissible; both
    must reach the drained result untouched (C0 reads, never synthesizes)."""
    result = _drain([*_text_events(PARTIAL), _terminal(status="incomplete", reason=details)])
    assert result.incomplete_reason == C2_REQUIRED_INCOMPLETE_REASON


@pytest.mark.parametrize(
    "details",
    [_ABSENT, None, {}, SimpleNamespace(), {"reason": None}],
    ids=["key-absent", "none", "empty-dict", "empty-object", "reason-none"],
)
def test_a3_absent_details_stay_none_and_are_never_synthesized(details):
    """The absence is load-bearing: a local context wall sends no reason, and
    that absence is what keeps it distinct from a hosted content filter."""
    kwargs = {} if details is _ABSENT else {"reason": details}
    result = _drain([*_text_events(PARTIAL), _terminal(status="incomplete", **kwargs)])
    assert result.finish_reason == "length"
    assert result.incomplete_reason is None


def test_a4_a_later_terminal_event_does_not_inherit_the_earlier_reason():
    """The reset is PER TERMINAL EVENT: a second terminal event carries its own
    reason (here: none), never the previous one's.

    Note the two layers honestly.  The provider resets and re-derives per
    terminal event; ``drain_stream`` collapses with a last-non-empty update,
    because a real Responses stream carries exactly ONE terminal event.  A
    malformed stream that sent two could therefore surface the earlier reason
    beside the later ``finish_reason`` — harmless for C2, which additionally
    requires ``finish_reason == "length"``, and not reachable on a conformant
    endpoint.  Recorded rather than asserted away.
    """
    provider = OpenAIResponsesProvider()
    events = [
        *_text_events(PARTIAL),
        _terminal(
            status="incomplete",
            reason=SimpleNamespace(reason=C2_REQUIRED_INCOMPLETE_REASON),
        ),
        _terminal(status="completed"),
    ]
    terminals = [c for c in provider._iter_stream(iter(events)) if c.finish_reason]
    assert len(terminals) == 2
    assert terminals[0].incomplete_reason == C2_REQUIRED_INCOMPLETE_REASON
    assert terminals[1].incomplete_reason is None, "per-terminal reset"
    assert terminals[1].finish_reason == "stop"


def test_a5_truncation_without_the_terminal_payload_keeps_its_blocks():
    """Crossed case: a payload-less terminal must not drop the collected
    repair items (they are what the NEXT turn replays), and carries no reason."""
    item = SimpleNamespace(type="reasoning", summary=[])
    item.model_dump = lambda **_kw: {"type": "reasoning", "summary": []}  # type: ignore[method-assign]
    result = _drain(
        [
            SimpleNamespace(type="response.output_item.done", item=item),
            SimpleNamespace(type="response.incomplete", response=None),
        ]
    )
    assert result.finish_reason == "length"
    assert result.provider_blocks == [{"type": "reasoning", "summary": []}]
    assert result.incomplete_reason is None


# ═══════════════════════════════════════════════════════════════════════════
# B. The C2 trigger crossed with replay / compaction state
# ═══════════════════════════════════════════════════════════════════════════


def test_b1_compaction_shaped_history_does_not_block_the_trigger():
    """Replay-repaired, compaction-shaped history is REQUEST state: it must
    not appear to the continuation trigger at all."""
    session, ui, consumer = _setup(history=OBSERVED_COMPACTED_HISTORY)
    out, calls = _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "6\n7\n")])

    assert len(calls) == 1, "a truthful truncation must still continue"
    # The leg submits history + the prefill assistant turn, i.e. the repair
    # state is carried on the wire, not consulted by the policy.
    submitted = calls[0]["messages"]
    assert submitted[: len(OBSERVED_COMPACTED_HISTORY)] == OBSERVED_COMPACTED_HISTORY
    assert out.content == PARTIAL + "6\n7\n"
    assert ("content", "6\n7\n") in ui.events


def test_b2_request_side_replay_items_are_not_visible_continuation_state():
    """Vary ONLY the result's native blocks across one identical history.

    This is the sharp form of the rule: eligibility reads the result's own
    requisites, so a history full of replay artifacts (and a provider that
    synthesizes them) changes nothing — while a reasoning-bearing RESULT
    still refuses (Mode 2 is deferred, not inferred).
    """
    session_text, _, consumer_text = _setup(history=OBSERVED_COMPACTED_HISTORY)
    out_text, calls_text = _run_with_rail(
        session_text, consumer_text, _result(), [_leg(PARTIAL + "6\n")]
    )
    assert len(calls_text) == 1
    assert out_text.content == PARTIAL + "6\n"

    reasoning_native = ProviderNative(
        producer="openai_responses", blocks=({"type": "reasoning", "id": "rs_1"},)
    )
    session, _, consumer = _setup(history=OBSERVED_COMPACTED_HISTORY)
    out, calls_reasoning = _run_with_rail(
        session, consumer, _result(native=reasoning_native), []
    )
    assert calls_reasoning == [], "a reasoning-bearing turn must not be continued"
    assert out.finish_reason == "length"


def test_b3_a_continuation_run_leaves_the_request_state_byte_identical():
    """Transactionality crossed with the repair: lowering the same history
    after a run produces byte-identical items — the run left no trace that
    could change what the NEXT turn replays."""
    history = _failing_round()
    before = copy.deepcopy(history)
    _, items_before = _lower(history, replay=True)

    session, _, consumer = _setup(history=OBSERVED_COMPACTED_HISTORY)
    _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "6\n")])
    assert session.messages == OBSERVED_COMPACTED_HISTORY, "no synthetic history stranded"

    _, items_after = _lower(history, replay=True)
    assert items_after == items_before
    assert history == before, "lowering must not mutate the caller's history"


# ═══════════════════════════════════════════════════════════════════════════
# C. Compaction / reopen crossed with continuation
# ═══════════════════════════════════════════════════════════════════════════


def test_c1_gate_off_over_compacted_history_issues_no_leg_and_touches_nothing():
    """Reopening a compacted session must never ARM the feature."""
    session, _, consumer = _setup(gate=False, history=OBSERVED_COMPACTED_HISTORY)
    result = _result()
    out, calls = _run_with_rail(session, consumer, result, [])
    assert out is result
    assert calls == []
    assert session.messages == OBSERVED_COMPACTED_HISTORY


def test_c2_history_shape_never_changes_eligibility_or_the_leg_budget():
    """Compaction shrinks the history; that must not move the continuation
    decision NOR the leg's token allowance (the budget anchors on the
    provider's own prompt_tokens, never on conversation size)."""
    session_a, _, consumer_a = _setup(history=OBSERVED_COMPACTED_HISTORY)
    _, calls_a = _run_with_rail(session_a, consumer_a, _result(), [_leg(PARTIAL + "6\n")])

    session_b, _, consumer_b = _setup(history=OBSERVED_LONG_HISTORY)
    _, calls_b = _run_with_rail(session_b, consumer_b, _result(), [_leg(PARTIAL + "6\n")])

    assert len(calls_a) == len(calls_b) == 1
    assert calls_a[0]["max_tokens"] == calls_b[0]["max_tokens"]
    assert calls_a[0]["max_tokens"] is not None
    # Only the submitted history differs — which is the point.
    assert calls_a[0]["messages"] != calls_b[0]["messages"]


def test_c3_session_state_is_restored_after_a_gate_on_run():
    """The seam installs temporary state; a successful run must restore the
    SAME objects, and the aggregate accounting must be the initial generation
    plus every accepted leg."""
    session, _, consumer = _setup(history=OBSERVED_COMPACTED_HISTORY)
    messages_before = session.messages
    usage_before = session._last_usage
    pending_before = session._assistant_pending_tokens

    out, calls = _run_with_rail(session, consumer, _result(), [_leg(PARTIAL + "6\n", completion_tokens=10)])

    assert len(calls) == 1
    assert session.messages is messages_before
    assert session._last_usage is usage_before
    assert session._last_usage["completion_tokens"] == 50, "40 initial + 10 leg"
    assert session._assistant_pending_tokens == 50
    assert pending_before == 40
    assert out.usage.completion_tokens == 50
    assert out.usage.total_tokens == out.usage.prompt_tokens + 50


def test_c4_a_dying_leg_leaves_no_synthetic_history_and_keeps_the_answer():
    """A leg that dies mid-run is a provider failure, not a continuation
    outcome: no exception escapes, the message state is restored, and the
    answer already displayed is served unchanged."""
    session, _, consumer = _setup(history=OBSERVED_COMPACTED_HISTORY)
    result = _result()
    out, calls = _run_with_rail(session, consumer, result, [RuntimeError("wire died")])

    assert len(calls) == 1
    assert out is result, "no suffix was validated, so the original result stands"
    assert session.messages == OBSERVED_COMPACTED_HISTORY


# ═══════════════════════════════════════════════════════════════════════════
# D. Gate OFF parity with production's request shape
# ═══════════════════════════════════════════════════════════════════════════


def test_d1_gate_off_leaves_the_result_and_the_history_untouched():
    session, _, consumer = _setup(gate=False, history=OBSERVED_LONG_HISTORY)
    result = _result(native=ProviderNative(producer="openai_responses", blocks=()))
    out, calls = _run_with_rail(session, consumer, result, [])
    assert out is result
    assert calls == []
    assert session.messages == OBSERVED_LONG_HISTORY


def test_d2_gate_off_lowering_is_still_the_production_round_repair_shape():
    """With C2 off, the wire the operator runs is production's: the repair
    items are synthesized exactly as the deployed lineage produces them."""
    _, items = _lower(_failing_round(), replay=True)
    synthesized = _round_reasoning_items(items)
    assert len(synthesized) == 1
    assert synthesized[0]["id"].startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)
    assert synthesized[0]["content"][0]["type"] == "reasoning_text"


def test_d3_gate_off_with_replay_off_removes_nothing_from_the_wire():
    _, with_replay = _lower(_failing_round(), replay=True)
    _, without_replay = _lower(_failing_round(), replay=False)
    assert _round_reasoning_items(with_replay) != _round_reasoning_items(without_replay)
    assert _round_reasoning_items(without_replay) == []


# ═══════════════════════════════════════════════════════════════════════════
# E. The chain itself: C0's signal -> C2's trigger
# ═══════════════════════════════════════════════════════════════════════════


def test_e1_only_the_truthful_wire_reason_reaches_the_trigger():
    """The integration that motivated C0: a real truncation terminal becomes
    an eligible continuation, while its reason-less twin (the local context
    wall) does not — driven through the provider, not by hand-built results."""
    truthful = _drain(
        [
            *_text_events(PARTIAL),
            _terminal(
                status="incomplete",
                reason=SimpleNamespace(reason=C2_REQUIRED_INCOMPLETE_REASON),
            ),
        ]
    )
    wall = _drain([*_text_events(PARTIAL), _terminal(status="incomplete")])

    def _as_result(completion) -> ModelTurnResult:
        return ModelTurnResult(
            turn=Turn.assistant(completion.content),
            finish_reason=completion.finish_reason,
            usage=completion.usage,
            tool_calls=[],
            provenance=TurnProvenance(model_alias="a", backend_model_id=PROVEN_MODEL),
            incomplete_reason=completion.incomplete_reason,
        )

    session, _, consumer = _setup(history=OBSERVED_COMPACTED_HISTORY)
    out, calls = _run_with_rail(session, consumer, _as_result(truthful), [_leg(PARTIAL + "6\n")])
    assert len(calls) == 1, "a truthful output-budget reason must continue"
    assert out.content == PARTIAL + "6\n"

    session_wall, _, consumer_wall = _setup(history=OBSERVED_COMPACTED_HISTORY)
    wall_result = _as_result(wall)
    out_wall, calls_wall = _run_with_rail(session_wall, consumer_wall, wall_result, [])
    assert calls_wall == [], "a reason-less truncation must never continue"
    assert out_wall is wall_result
    # The same wire content, the same history, the same lane — the ONLY
    # difference is the endpoint's stated reason.
    assert truthful.content == wall.content == PARTIAL
