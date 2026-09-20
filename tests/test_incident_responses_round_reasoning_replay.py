"""Incident tests: strict-thinking replay contract on the RESPONSES wire.

Companion to ``test_incident_compaction_round_reasoning.py`` (the Chat-Completions
wire, CI-1).  That repair is composed inside
``model_turn.maybe_attach_vllm_chat_reasoning``, whose first gate admits
Chat-Completions providers only.  After the Responses migration every
turnstone-facing model row is ``server_compat.api_surface = "responses"``, so the
guarded pass stopped being reached and the post-compaction 400 returned
(2026-09-20):

    HTTP 400 "The `reasoning_text` in the thinking mode must be passed back to the API."

The shaping measurements these tests encode were taken on the live lane on
2026-09-20 (``switchyard-smart-turnstone`` and ``sw-deepseek-deepseek-flash``):

* history ending on a ``function_call_output`` whose turn carries a
  ``function_call`` and NO reasoning item -> 400;
* the same history with a reasoning item carrying a ``reasoning_text`` content
  part -> 200;
* the same history with a ``summary``-only reasoning item -> **still 400**;
* a history that does not end on a tool result -> 200 even with no reasoning at all.
"""

from __future__ import annotations

import copy
import json

from turnstone.core.history_decoration import (
    _ROUND_REASONING_PLACEHOLDER,
    ROUND_REASONING_PLACEHOLDER,
)
from turnstone.core.providers._openai_responses import (
    _RESPONSES_ROUND_REASONING_ID_PREFIX,
    OpenAIResponsesProvider,
    ensure_responses_round_reasoning_items,
)

# ── item builders ────────────────────────────────────────────────────────────


def _msg(role: str, text: str) -> dict:
    return {
        "type": "message",
        "role": role,
        "content": [
            {
                "type": "output_text" if role == "assistant" else "input_text",
                "text": text,
            }
        ],
    }


def _call(call_id: str = "call_1") -> dict:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": "get_status",
        "arguments": "{}",
    }


def _output(call_id: str = "call_1") -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": '{"state":"active"}'}


def _stored_reasoning(text: str = "I should call get_status.", rid: str = "rs_real") -> dict:
    return {
        "type": "reasoning",
        "id": rid,
        "summary": [{"type": "summary_text", "text": text}],
        "content": [{"type": "reasoning_text", "text": text}],
    }


MARKER = "[Conversation summary] user asked X; assistant replied Y."


def _failing_shape() -> list[dict]:
    """The production shape: the resumed round's tool-call turn has no reasoning."""
    return [
        _msg("user", "Check the service status."),
        _msg("assistant", "I will check."),
        _msg("user", MARKER),
        _msg("assistant", "Backend request failed before generation started."),
        _call(),
        _output(),
    ]


def _reasoning_items(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("type") == "reasoning"]


def _provider() -> OpenAIResponsesProvider:
    # Instance without running __init__: the conversion path needs no client state.
    return object.__new__(OpenAIResponsesProvider)


def _lower(messages: list[dict], *, replay: bool):
    return _provider()._convert_messages(messages, replay_reasoning_to_model=replay)


# ── A. positive reproducer ───────────────────────────────────────────────────


def test_a1_failing_shape_gains_exactly_one_reasoning_item():
    items = _failing_shape()
    out = ensure_responses_round_reasoning_items(items)

    assert len(_reasoning_items(out)) == 1, out
    item = _reasoning_items(out)[0]
    assert item["type"] == "reasoning"
    assert item["id"].startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)


def test_a2_synthesized_item_carries_a_reasoning_text_content_part():
    """A ``summary``-only item does NOT satisfy the contract (measured 2026-09-20).

    This is the assertion that stops the repair from silently re-introducing the
    400: ``_reasoning_item_for_input`` always sets ``summary`` and only
    conditionally sets ``content``, so the obvious port emits summary-only.
    """
    out = ensure_responses_round_reasoning_items(_failing_shape())
    item = _reasoning_items(out)[0]

    assert [p.get("type") for p in item["content"]] == ["reasoning_text"]
    assert item["content"][0]["text"] == ROUND_REASONING_PLACEHOLDER
    assert ROUND_REASONING_PLACEHOLDER != ""


def test_a3_item_is_inserted_immediately_before_the_continued_turn():
    out = ensure_responses_round_reasoning_items(_failing_shape())
    idx_call = next(i for i, x in enumerate(out) if x.get("type") == "function_call")
    idx_reason = next(i for i, x in enumerate(out) if x.get("type") == "reasoning")

    assert idx_reason < idx_call
    # ...and the trailing tool output still terminates the history.
    assert out[-1]["type"] == "function_call_output"
    assert len(out) == len(_failing_shape()) + 1


def test_a4_placeholder_text_is_shared_with_the_chat_wire_repair():
    assert ROUND_REASONING_PLACEHOLDER == _ROUND_REASONING_PLACEHOLDER


def test_a5_multi_item_round_gets_one_item_not_one_per_item():
    """Several assistant-emitted items in one turn must still get a single item."""
    items = [
        _msg("user", "Check."),
        _msg("user", MARKER),
        _msg("assistant", "part one"),
        _call("call_1"),
        _call("call_2"),
        _output("call_1"),
        _output("call_2"),
    ]
    out = ensure_responses_round_reasoning_items(items)
    assert len(_reasoning_items(out)) == 1


def test_a6_real_serialization_of_the_chat_history_gains_the_item():
    """End-to-end through the composer on a live-shaped Chat-Completions history."""
    history = [
        {"role": "user", "content": "Check the service status."},
        {"role": "assistant", "content": "I will check."},
        {"role": "user", "content": MARKER},
        {"role": "assistant", "content": "Backend request failed before generation started."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_status", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"state":"active"}'},
    ]
    _, items = _lower(history, replay=True)
    reasoning = _reasoning_items(items)

    assert len(reasoning) == 1, items
    assert reasoning[0]["content"][0]["type"] == "reasoning_text"
    assert items[-1]["type"] == "function_call_output"
    # The item must survive JSON serialization to the wire.
    assert '"reasoning_text"' in json.dumps(items)


# ── B. existing-reasoning control ────────────────────────────────────────────


def test_b1_stored_reasoning_is_preserved_exactly_and_not_duplicated():
    items = [
        _msg("user", "Check."),
        _msg("user", MARKER),
        _stored_reasoning("REAL-STORED"),
        _msg("assistant", "calling"),
        _call(),
        _output(),
    ]
    out = ensure_responses_round_reasoning_items(items)
    reasoning = _reasoning_items(out)

    assert len(reasoning) == 1
    assert reasoning[0]["content"][0]["text"] == "REAL-STORED"
    assert reasoning[0]["id"] == "rs_real"
    assert out == items  # byte-identical: nothing added, nothing rewritten


def test_b2_stored_reasoning_from_provider_blocks_is_replayed_not_replaced():
    """A native stored reasoning block round-trips; no placeholder is added."""
    history = [
        {"role": "user", "content": MARKER},
        {
            "role": "assistant",
            "content": "calling",
            "_provider_content": [
                {"type": "reasoning", "id": "rs_native",
                 "summary": [{"type": "summary_text", "text": "native cotton"}]},
            ],
            "_producer": "openai",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_status", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]
    _, items = _lower(history, replay=True)
    reasoning = _reasoning_items(items)

    assert [r["id"] for r in reasoning] == ["rs_native"], reasoning
    assert all(ROUND_REASONING_PLACEHOLDER not in json.dumps(r) for r in reasoning)


# ── C. plain-user-turn negative control ──────────────────────────────────────


def test_c1_history_ending_on_a_user_turn_is_unchanged():
    items = [
        _msg("user", MARKER),
        _msg("assistant", "no reasoning here"),
        _call(),
        _output(),
        _msg("user", "and now carry on"),
    ]
    assert ensure_responses_round_reasoning_items(items) == items


def test_c2_history_ending_on_a_user_turn_through_the_composer():
    history = [
        {"role": "user", "content": MARKER},
        {"role": "assistant", "content": "carrying on"},
    ]
    _, items = _lower(history, replay=True)
    assert _reasoning_items(items) == []


# ── D. no-tool negative control ──────────────────────────────────────────────


def test_d1_no_tool_round_means_no_synthesis():
    items = [_msg("user", MARKER), _msg("assistant", "plain answer")]
    assert ensure_responses_round_reasoning_items(items) == items


def test_d2_tools_earlier_in_the_conversation_are_not_a_trigger():
    """"The conversation contains tools somewhere" must not arm the repair."""
    items = [
        _msg("user", "Check."),
        _msg("assistant", "calling"),
        _call("call_1"),
        _output("call_1"),
        _msg("user", MARKER),
        _msg("assistant", "plain closing answer"),
    ]
    assert ensure_responses_round_reasoning_items(items) == items


def test_d3_tool_output_without_a_function_call_is_not_armed():
    items = [_msg("user", MARKER), _msg("assistant", "x"), _output("call_1")]
    assert ensure_responses_round_reasoning_items(items) == items


def test_d4_empty_and_call_id_less_inputs_are_returned_unchanged():
    assert ensure_responses_round_reasoning_items([]) == []
    no_id = [_msg("user", MARKER), _call(""), _output("")]
    assert ensure_responses_round_reasoning_items(no_id) == no_id


# ── E. gate-off control ──────────────────────────────────────────────────────


def test_e1_operator_flag_off_leaves_the_lowered_items_untouched():
    history = [
        {"role": "user", "content": MARKER},
        {"role": "assistant", "content": "Backend request failed before generation started."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_status", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]
    _, on = _lower(history, replay=True)
    _, off = _lower(history, replay=False)

    assert _reasoning_items(on) != []
    assert _reasoning_items(off) == []
    assert off == [
        i for i in on if i.get("type") != "reasoning"
    ], "gate-off output must be exactly the ungated item list"


def test_e2_flag_off_is_the_pre_fix_behaviour_for_the_ungated_provider():
    """A lane without the flag produces the same items as before the repair."""
    items = _failing_shape()
    # With the flag off the composer never calls the pass.
    assert _reasoning_items(items) == []


# ── F. purity / idempotence / determinism ────────────────────────────────────


def test_f1_idempotent():
    once = ensure_responses_round_reasoning_items(_failing_shape())
    twice = ensure_responses_round_reasoning_items(once)
    assert once == twice


def test_f2_input_is_not_mutated():
    items = _failing_shape()
    before = copy.deepcopy(items)
    ensure_responses_round_reasoning_items(items)
    assert items == before
    assert json.dumps(items) == json.dumps(before)


def test_f3_synthesized_id_is_deterministic_across_calls():
    a = _reasoning_items(ensure_responses_round_reasoning_items(_failing_shape()))[0]
    b = _reasoning_items(ensure_responses_round_reasoning_items(_failing_shape()))[0]
    assert a["id"] == b["id"] == f"{_RESPONSES_ROUND_REASONING_ID_PREFIX}call_1"


def test_f4_no_invented_reasoning_text():
    """The item asserts only the placeholder; it fabricates no CoT."""
    item = _reasoning_items(ensure_responses_round_reasoning_items(_failing_shape()))[0]
    texts = [p["text"] for p in item["content"]] + [p["text"] for p in item["summary"]]
    assert set(texts) == {ROUND_REASONING_PLACEHOLDER}


# ── G. scope boundary ────────────────────────────────────────────────────────


def test_g1_historical_assistant_turns_are_not_touched():
    """Only the continued turn is repaired; earlier turns gain nothing."""
    items = [
        _msg("user", "first"),
        _msg("assistant", "historical pre-summary answer"),
        _msg("user", MARKER),
        _msg("assistant", "in-round"),
        _call(),
        _output(),
    ]
    out = ensure_responses_round_reasoning_items(items)

    assert len(_reasoning_items(out)) == 1
    # the historical assistant message (index 1) is untouched and no reasoning
    # item was inserted anywhere before the last user message
    idx_user = max(i for i, x in enumerate(out) if x.get("role") == "user")
    idx_reason = next(i for i, x in enumerate(out) if x.get("type") == "reasoning")
    assert idx_reason > idx_user
    assert out[1] == items[1]


def test_g2_turn_before_any_user_message_is_not_armed():
    items = [_msg("assistant", "leading"), _call(), _output()]
    assert ensure_responses_round_reasoning_items(items) == items


# ── H. retry stability ───────────────────────────────────────────────────────


def test_h1_repeated_lowering_is_stable():
    history = [
        {"role": "user", "content": MARKER},
        {"role": "assistant", "content": "calling"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_9", "type": "function",
                 "function": {"name": "get_status", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "{}"},
    ]
    _, first = _lower(history, replay=True)
    _, second = _lower(history, replay=True)

    assert first == second
    assert [r["id"] for r in _reasoning_items(first)] == [
        f"{_RESPONSES_ROUND_REASONING_ID_PREFIX}call_9"
    ]
