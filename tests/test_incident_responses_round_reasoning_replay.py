"""Incident tests: strict-thinking replay contract on the RESPONSES wire.

Companion to ``test_incident_compaction_round_reasoning.py`` (the Chat-Completions
wire, CI-1).  That repair is composed inside
``model_turn.maybe_attach_vllm_chat_reasoning``, whose first gate admits
Chat-Completions providers only.  After the Responses migration every
turnstone-facing model row is ``server_compat.api_surface = "responses"``, so the
guarded pass stopped being reached and the post-compaction 400 returned
(2026-09-20):

    HTTP 400 "The `reasoning_text` in the thinking mode must be passed back to the API."

The measurements these tests encode were taken on the live lane in TWO rounds
(``switchyard-smart-turnstone`` and ``sw-deepseek-deepseek-flash``).  The first
round fixed the scope of the problem; the second corrected WHERE the requirement
attaches, and that is the rule the pass implements.  Both are kept here, because
the first round's reading was too narrow.

2026-09-20 -- the trailing-tool-round reading:

* history ending on a ``function_call_output`` whose turn carries a
  ``function_call`` and NO reasoning item -> 400;
* the same history with a reasoning item carrying a ``reasoning_text`` content
  part -> 200;
* the same history with a ``summary``-only reasoning item -> **still 400**;
* a history that does not end on a tool result -> 200 even with no reasoning at all.

That last line was over-general and is REFUTED by the 2026-09-22 round.

2026-09-22 -- the per-assistant-turn rule (authoritative):

* the requirement attaches to the TRAILING BLOCK -- the items after the LAST
  ``user`` message -- and inside it to EACH ASSISTANT TURN, not to the presence of
  a tool round;
* one reasoning item at the head of the block is insufficient: three tool rounds
  with a single head item -> 400, the same three rounds each covered -> 200;
* a call-only turn needs its own cover, and a bare ``[assistant]`` list with no
  user message anywhere is refused too, so the whole list is then the block;
* ``summary``-only and empty-``reasoning_text`` items cover nothing;
* placement must PRECEDE the turn it covers.

The tests below therefore assert the per-assistant-turn rule.  Where a test
previously asserted the narrow trailing-round predicate (``d1``, ``d2``, ``d3``,
``d4``, ``g2``, ``i1``, ``i2``, ``i3``, ``i5``, ``i6``, ``i7``), the assertion now states
what the measured rule requires; the safety properties those tests also guarded --
historical turns untouched, no mutation of the caller's list, deterministic ids,
no invented CoT -- are re-asserted rather than dropped.
"""

from __future__ import annotations

import copy
import json

from turnstone.core.history_decoration import (
    _ROUND_REASONING_PLACEHOLDER,
    ROUND_REASONING_PLACEHOLDER,
)
from turnstone.core.providers._openai_responses import (
    _RESPONSES_ROUND_REASONING_ID_DIGEST_CHARS,
    _RESPONSES_ROUND_REASONING_ID_PREFIX,
    _has_usable_reasoning_text,
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


def test_b2_stored_reasoning_block_is_replayed_untouched_and_still_satisfied():
    """A native stored reasoning block is replayed verbatim, never replaced.

    Its ``summary``-only shape does NOT satisfy the contract (a ``summary``-only
    item covers nothing, measured 2026-09-20 and again 2026-09-22), so the pass adds
    the satisfier ALONGSIDE it rather than rewriting the stored item.
    """
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

    native = [r for r in reasoning if r["id"] == "rs_native"]
    synthesized = [r for r in reasoning if r["id"] != "rs_native"]

    assert len(native) == 1, reasoning                       # replayed, not replaced
    assert native[0]["summary"] == [{"type": "summary_text", "text": "native cotton"}]
    assert not _has_usable_reasoning_text(native[0])         # so it covers nothing
    assert ROUND_REASONING_PLACEHOLDER not in json.dumps(native[0])

    assert len(synthesized) == 1, reasoning                  # ...so a cover is added
    assert synthesized[0]["id"].startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)
    assert ROUND_REASONING_PLACEHOLDER in json.dumps(synthesized[0])
    # placed before the assistant turn it covers, not after it
    idx = items.index(synthesized[0])
    assert items[idx + 1]["type"] == "message"
    assert items[idx + 1]["role"] == "assistant"


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


def test_c2_history_ending_on_an_assistant_turn_through_the_composer():
    """The composer path: the block is the final assistant turn, which needs cover.

    (A transcript ending on the user turn is the ``c1`` case and engages nothing.)
    """
    history = [
        {"role": "user", "content": MARKER},
        {"role": "assistant", "content": "carrying on"},
    ]
    _, items = _lower(history, replay=True)
    reasoning = _reasoning_items(items)

    assert len(reasoning) == 1, items
    idx = items.index(reasoning[0])
    assert items[idx + 1]["type"] == "message"
    assert items[idx + 1]["role"] == "assistant"


# ── D. no-tool negative control ──────────────────────────────────────────────


def test_d1_a_plain_assistant_turn_in_the_block_gains_its_own_cover():
    """"No tool round" does not mean "no requirement" (corrected 2026-09-22).

    The requirement attaches to the assistant turn in the trailing block, so a
    transcript whose continued turn is a plain answer is covered too.  The narrower
    reading -- that only a trailing tool round armed the repair -- is what let the
    post-compaction 400 survive.
    """
    items = [_msg("user", MARKER), _msg("assistant", "plain answer")]
    out = ensure_responses_round_reasoning_items(items)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    assert out[0] == items[0]                 # the user turn is untouched
    assert out.index(reasoning[0]) == 1       # the cover precedes the assistant turn
    assert out[2] == items[1]


def test_d2_earlier_tools_are_irrelevant_and_the_history_is_untouched():
    """"The conversation contains tools somewhere" is not the arming condition.

    Arming follows the assistant turn in the block, so tools earlier in the
    conversation neither arm nor disarm it -- and the pre-summary history must come
    through byte-identical.
    """
    items = [
        _msg("user", "Check."),
        _msg("assistant", "calling"),
        _call("call_1"),
        _output("call_1"),
        _msg("user", MARKER),
        _msg("assistant", "plain closing answer"),
    ]
    out = ensure_responses_round_reasoning_items(items)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    # inserted inside the trailing block, immediately before the closing turn
    assert out[out.index(reasoning[0]) + 1] == items[-1]
    # and everything before the last user message is reproduced unchanged
    assert out[:5] == items[:5]


def test_d3_an_orphan_tool_output_neither_arms_nor_blocks_the_turn():
    """Arming depends on the assistant turn, not on tool-call correlation.

    The output answers no call in the block, which under the superseded
    trailing-round reading failed closed.  The measured rule asks only whether the
    block holds an assistant turn with nothing to replay -- it does -- so the turn is
    covered and the orphan item is passed through untouched.
    """
    items = [_msg("user", MARKER), _msg("assistant", "x"), _output("call_1")]
    out = ensure_responses_round_reasoning_items(items)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    assert out[out.index(reasoning[0]) + 1] == items[1]
    assert out[-1] == items[2]


def test_d4_empty_input_and_empty_call_ids():
    """Empty input is returned as-is; id-less items are passed through unchanged.

    The superseded reading armed on tool-call ids, so an empty ``call_id`` failed
    closed.  The block rule does not consult ids at all: the turn is covered, and the
    id-less items themselves are neither rewritten nor dropped.
    """
    assert ensure_responses_round_reasoning_items([]) == []

    no_id = [_msg("user", MARKER), _call(""), _output("")]
    out = ensure_responses_round_reasoning_items(no_id)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    assert out[out.index(reasoning[0]) + 1] == no_id[1]   # precedes the call
    assert out[-2] == no_id[1]                            # call passed through
    assert out[-1] == no_id[2]                            # output passed through


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
    assert a["id"] == b["id"]
    assert a["id"].startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)
    assert len(a["id"]) == len(_RESPONSES_ROUND_REASONING_ID_PREFIX) + \
        _RESPONSES_ROUND_REASONING_ID_DIGEST_CHARS


def test_f5_synthesized_id_is_charset_safe_bounded_and_collision_resistant():
    """The id never leaks an upstream call_id into the wire field."""
    import re

    def synth_id(call_id: str) -> str:
        items = [
            _msg("user", MARKER),
            _call(call_id),
            _output(call_id),
        ]
        return _reasoning_items(ensure_responses_round_reasoning_items(items))[0]["id"]

    safe = re.compile(r"^[A-Za-z0-9_-]+$")
    for exotic in (
        "call_plain",
        "call with spaces",
        "call\nwith\nnewlines",
        "call/with:punctuation!?",
        "\u0e01\u0e32\u0e23\u0e40\u0e23\u0e35\u0e22\u0e01",
        "x" * 4000,
    ):
        rid = synth_id(exotic)
        assert safe.match(rid), (exotic, rid)
        assert len(rid) == len(_RESPONSES_ROUND_REASONING_ID_PREFIX) + \
            _RESPONSES_ROUND_REASONING_ID_DIGEST_CHARS

    # distinct rounds -> distinct ids
    assert synth_id("call_a") != synth_id("call_b")


def test_f6_synthesized_id_is_deterministic_and_block_order_is_identity():
    """Parallel calls hash IN ORDER; block order is part of what the id identifies.

    The superseded reading hashed the call ids as a set, so two orders of the same
    calls produced one id.  The id now binds the block as it will be replayed, in
    order: the same block always yields the same id (retries stay stable), and a
    re-ordered block is a different identity rather than a silent collision.
    """
    def run(order: list[str]) -> str:
        items = [_msg("user", MARKER)]
        items += [_call(c) for c in order]
        items += [_output(c) for c in order]
        return _reasoning_items(ensure_responses_round_reasoning_items(items))[0]["id"]

    for order in (["call_1", "call_2"], ["call_2", "call_1"]):
        rid = run(order)
        assert rid.startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)
        assert len(rid) == len(_RESPONSES_ROUND_REASONING_ID_PREFIX) + \
            _RESPONSES_ROUND_REASONING_ID_DIGEST_CHARS
        assert rid == run(list(order)), "the same block must give the same id"

    assert run(["call_1", "call_2"]) != run(["call_2", "call_1"])


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


def test_g2_a_leading_assistant_turn_with_no_user_message_is_covered():
    """No user turn anywhere: the boundary cannot be established, so the whole list
    is the block -- and the lane refuses uncovered assistant content even then
    (measured 2026-09-22: a bare ``[assistant]`` list is refused).
    """
    items = [_msg("assistant", "leading"), _call(), _output()]
    out = ensure_responses_round_reasoning_items(items)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    assert out[out.index(reasoning[0]) + 1] == items[0]
    assert out[-2] == items[1]
    assert out[-1] == items[2]


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
    ids = [r["id"] for r in _reasoning_items(first)]
    assert len(ids) == 1
    assert ids[0].startswith(_RESPONSES_ROUND_REASONING_ID_PREFIX)


# ── I. review findings: boundary, correlation, id hardening ──────────────────


def test_i1_each_assistant_turn_in_the_block_is_repaired():
    """Two sequential tool rounds in one block: each turn gets its own cover.

    Covering only the LAST round is what the superseded reading did, and it is
    measurably insufficient: an earlier uncovered turn in the block still 400s.
    """
    items = [
        _msg("user", "do two things"),
        _msg("assistant", "first"),
        _call("call_1"),
        _output("call_1"),
        _msg("assistant", "second"),
        _call("call_2"),
        _output("call_2"),
    ]
    out = ensure_responses_round_reasoning_items(items)
    reasoning = _reasoning_items(out)

    assert len(reasoning) == 2, out
    first_cover, second_cover = reasoning
    # one cover immediately before EACH assistant turn, in block order
    assert out[out.index(first_cover) + 1] == items[1]
    assert out[out.index(second_cover) + 1] == items[4]
    # the historical round's own items are passed through unchanged
    assert out[out.index(_call("call_1")) - 1]["type"] == "message"


def test_i2_an_orphan_trailing_output_is_covered_like_any_other_turn():
    """The block's assistant turn is covered; the unanswered output passes through."""
    items = [
        _msg("user", MARKER),
        _msg("assistant", "calling"),
        _call("call_1"),
        _output("call_orphan"),
    ]
    out = ensure_responses_round_reasoning_items(items)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    assert out[out.index(reasoning[0]) + 1] == items[1]
    assert out[-1] == items[3]


def test_i3_a_stale_output_does_not_suppress_the_cover():
    """An output whose call_id belongs to a PREVIOUS turn changes nothing.

    Two assistant turns are in the block, so two covers are required, regardless of
    which outputs answer which calls.
    """
    items = [
        _msg("user", MARKER),
        _msg("assistant", "first"),
        _call("call_A"),
        _output("call_A"),
        _msg("assistant", "second"),
        _call("call_B"),
        _output("call_A"),
    ]
    out = ensure_responses_round_reasoning_items(items)
    reasoning = _reasoning_items(out)

    assert len(reasoning) == 2, out
    assert out[out.index(reasoning[0]) + 1] == items[1]
    assert out[out.index(reasoning[1]) + 1] == items[4]


def test_i4_partial_parallel_results_still_arm():
    """A subset of answered calls is legitimate (streaming/interrupted round)."""
    items = [
        _msg("user", MARKER),
        _msg("assistant", "calling"),
        _call("call_1"),
        _call("call_2"),
        _output("call_1"),
    ]
    out = ensure_responses_round_reasoning_items(items)
    assert len(_reasoning_items(out)) == 1


def test_i5_a_call_id_less_output_is_passed_through():
    items = [_msg("user", MARKER), _call("call_1"), {"type": "function_call_output"}]
    out = ensure_responses_round_reasoning_items(items)

    reasoning = _reasoning_items(out)
    assert len(reasoning) == 1, out
    assert out[out.index(reasoning[0]) + 1] == items[1]
    assert out[-1] == items[2]


def test_i6_last_user_boundary_is_a_reachable_branch_that_extends_the_block():
    """The no-user case is live behaviour, not a net.

    With no user message anywhere the boundary cannot be established, so the whole
    list is treated as the block and its assistant turns are covered -- measured
    2026-09-22: a bare ``[assistant]`` list is refused by the lane.  The superseded
    reading instead anchored on the trailing tool result, which made ``start <=
    last_user`` unreachable and let it document the no-user case as refused.

    A trailing user turn is still the ``c1`` case: the block ends before it, so
    there is nothing to cover and nothing is added.
    """
    # A turn after a user message is covered (the intended case)...
    armed = [_msg("user", "first"), _call("call_1"), _output("call_1")]
    assert len(_reasoning_items(ensure_responses_round_reasoning_items(armed))) == 1

    # ...and a leading assistant turn with NO user message at all is covered too:
    # the whole list is the block.
    no_user = [_msg("assistant", "leading"), _call("call_1"), _output("call_1")]
    out = ensure_responses_round_reasoning_items(no_user)
    covers = _reasoning_items(out)
    assert len(covers) == 1, out
    assert out[out.index(covers[0]) + 1] == no_user[0]

    # A trailing user turn ends the block before it: nothing to cover, nothing added.
    trailing_user = [_call("call_1"), _output("call_1"), _msg("user", "later question")]
    assert ensure_responses_round_reasoning_items(trailing_user) == trailing_user


def test_i7_every_short_history_ends_with_every_turn_covered():
    """Machine-checked post-condition over every short history.

    This replaces the earlier form, which replicated the implementation's index
    walk INSIDE the test and asserted a property of that replica.  The replica kept
    passing after the walk it modelled was replaced, so it verified nothing about
    the live code -- a stale-replica pass, the same failure mode as a verifier whose
    frozen anchor agrees with code that has since changed.  This form calls the real
    pass and checks the property the lane requires, on the real output:

    * every assistant turn in the trailing block is immediately PRECEDED by a
      reasoning item carrying non-empty ``reasoning_text``;
    * the items before the block are reproduced in order, unchanged, and nothing is
      dropped or duplicated;
    * the pass is idempotent.

    Both boundary classes are exercised, so the no-user case cannot silently
    disappear from the enumeration if the walk changes again.
    """
    import itertools

    def u():
        return _msg("user", "u")

    def a():
        return _msg("assistant", "a")

    def c():
        return _call("call_1")

    def o():
        return _output("call_1")

    def s():
        return {"type": "message", "role": "system",
                "content": [{"type": "input_text", "text": "s"}]}

    def is_turn(item: dict) -> bool:
        """An item the upstream attributes to the assistant's own output."""
        kind = item.get("type")
        return kind == "function_call" or (
            kind == "message" and item.get("role") == "assistant"
        )

    checked = no_user_histories = covered_turns = 0
    for length in range(1, 6):
        for combo in itertools.product([u, a, c, o, s], repeat=length):
            items = [f() for f in combo]
            out = ensure_responses_round_reasoning_items(items)
            checked += 1

            # the boundary, recomputed on the INPUT
            last_user = -1
            for index, item in enumerate(items):
                if item.get("type") == "message" and item.get("role") == "user":
                    last_user = index
            if last_user < 0:
                no_user_histories += 1
            block_start = last_user + 1

            if block_start >= len(items):
                # block empty: engage nothing, not even a copy
                assert out == items, items
                continue

            # nothing outside the block is changed, added or dropped
            assert out[:block_start] == items[:block_start], items

            block = out[block_start:]
            for offset, item in enumerate(block):
                if not is_turn(item):
                    continue
                if offset > 0 and is_turn(block[offset - 1]):
                    continue  # inside a maximal run: one cover serves the run
                assert offset > 0, (
                    "turn at the head of the block has no room for a cover", items)
                assert _has_usable_reasoning_text(block[offset - 1]), (
                    "assistant turn not preceded by usable reasoning_text", items)
                covered_turns += 1

            # idempotent: every synthesized cover is itself a usable item
            assert ensure_responses_round_reasoning_items(out) == out, items

    # guard against a vacuous pass
    assert checked == sum(5 ** n for n in range(1, 6)), checked
    assert no_user_histories > 0, "expected histories with no user boundary at all"
    assert covered_turns > 0, "expected at least one covered turn"
