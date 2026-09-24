"""Regression battery for the Responses-wire resumed-tool-round reasoning repair.

The defect
----------
A Responses request that RESUMES a tool round -- history ending on
``function_call_output`` -- must carry the reasoning item for the turn that made
those calls, because a strict-thinking upstream cannot continue a thinking turn
whose reasoning is invisible to it.  This wire replays reasoning only from a
stored native ``reasoning`` block, so a round whose interrupted assistant turn
recorded no reasoning was reconstructed with no reasoning item at all, and the
upstream refused it:

    HTTP 400 "The `reasoning_text` in the thinking mode must be passed back to
    the API."

The production trigger is the compaction-shaped resume: the in-band summary is
written by a NON-thinking lane, so the resumed history continues from an
assistant turn (tool calls plus their results) that has nothing to replay.

What these tests pin
--------------------
The repair is narrow by construction, so the battery pins BOTH directions: the
one shape that must be repaired, and every neighbouring shape that must be left
exactly as it was.

All tests drive the real ``OpenAIResponsesProvider``.  The upstream's
strict-thinking rule is encoded once, as a stub, so a "rejected before / accepted
after" pair can be demonstrated hermetically -- no network, no live lane.  The
stub is a PROXY for the live rejection, not the live server itself.

Module under test: ``turnstone/core/providers/_openai_responses.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from turnstone.core.providers import _openai_responses as responses_module
from turnstone.core.providers._openai_responses import (
    OpenAIResponsesProvider,
    _ensure_responses_round_reasoning_items,
)
from turnstone.core.providers._protocol import ModelCapabilities

_MODEL = "gpt-5"
_ARGS = '{"city": "Paris"}'
_CALL_1 = "call_1"
_CALL_2 = "call_2"
_TOOL_OUTPUT = "18C, clear."
_PLACEHOLDER = "(no reasoning text was recorded for this turn)"
_ID_PREFIX = "rs_roundrepair_"
_DIGEST_CHARS = 24

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Look up the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def _caps(*, synthesizes_round_reasoning: bool = True) -> ModelCapabilities:
    """Capability row for a replay-capable Responses endpoint.

    ``synthesizes_round_reasoning=True`` is the declared value on the
    self-hosted Responses lanes this repair was written for.  Pass ``False`` for
    an endpoint that replays reasoning but rejects a synthesized reasoning item
    -- the commercial OpenAI path, measured under ``store: false``.
    """
    return ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_reasoning_replay=True,
        synthesizes_round_reasoning=synthesizes_round_reasoning,
    )


def _tc(call_id: str, args: str = _ARGS) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "get_weather", "arguments": args},
    }


@pytest.fixture
def provider() -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider()


def _payload(
    provider: OpenAIResponsesProvider,
    messages: list[dict[str, Any]],
    *,
    replay: bool = True,
    caps: ModelCapabilities | None = None,
) -> dict[str, Any]:
    """Compose the real ``responses.create`` kwargs for *messages*."""
    return provider._build_kwargs(
        model=_MODEL,
        messages=messages,
        tools=_TOOLS,
        max_tokens=4096,
        temperature=None,
        reasoning_effort=None,
        deferred_names=None,
        capabilities=caps or _caps(),
        replay_reasoning_to_model=replay,
    )


def _items(
    provider: OpenAIResponsesProvider,
    messages: list[dict[str, Any]],
    *,
    replay: bool = True,
) -> list[dict[str, Any]]:
    return _payload(provider, messages, replay=replay)["input"]


# --------------------------------------------------------------------------- #
# Histories.  ``_resumed_round`` is the failing shape: a tool round with no
# recorded reasoning for the turn that made the calls.
# --------------------------------------------------------------------------- #


def _resumed_round(*, with_text: bool = False) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Weather in Paris?"},
        {
            "role": "assistant",
            "content": "Let me check." if with_text else "",
            "tool_calls": [_tc(_CALL_1)],
        },
        {"role": "tool", "tool_call_id": _CALL_1, "content": _TOOL_OUTPUT},
    ]


def _stored_reasoning_round() -> list[dict[str, Any]]:
    """A resumed round whose interrupted turn DID record native reasoning."""
    return [
        {"role": "user", "content": "Weather in Paris?"},
        {
            "role": "assistant",
            "content": "Let me check.",
            "_provider_content": [
                {
                    "type": "reasoning",
                    "id": "rs_stored_abc",
                    "summary": [{"type": "summary_text", "text": "stored chain of thought"}],
                    "encrypted_content": "enc-blob",
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Let me check."}],
                },
                {
                    "type": "function_call",
                    "call_id": _CALL_1,
                    "name": "get_weather",
                    "arguments": _ARGS,
                },
            ],
            "tool_calls": [_tc(_CALL_1)],
        },
        {"role": "tool", "tool_call_id": _CALL_1, "content": _TOOL_OUTPUT},
    ]


def _two_round_history() -> list[dict[str, Any]]:
    """An earlier completed tool round (no reasoning) followed by a resumed one."""
    return [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": "", "tool_calls": [_tc(_CALL_1)]},
        {"role": "tool", "tool_call_id": _CALL_1, "content": _TOOL_OUTPUT},
        {"role": "assistant", "content": "It is 18C and clear in Paris."},
        {"role": "user", "content": "And London?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_tc(_CALL_2, '{"city": "London"}')],
        },
        {"role": "tool", "tool_call_id": _CALL_2, "content": "11C, rain."},
    ]


def _reasoning_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("type") == "reasoning"]


# --------------------------------------------------------------------------- #
# Strict-upstream stub.  Encodes the documented rule the live upstream enforces
# on a resumed thinking round.  A PROXY: it reproduces the rejection class, it is
# not the live server.
# --------------------------------------------------------------------------- #


def _strict_upstream_rejection(payload: dict[str, Any]) -> str | None:
    """Return the 400 reason if the payload violates the strict-thinking contract."""
    items = payload.get("input") or []
    tail = len(items)
    while tail > 0 and items[tail - 1].get("type") == "function_call_output":
        tail -= 1
    if tail == len(items) or tail == 0:
        return None
    start = tail
    while start > 0 and items[start - 1].get("type") in ("reasoning", "function_call"):
        start -= 1
    turn = items[start:tail]
    if not any(item.get("type") == "function_call" for item in turn):
        return None
    if any(item.get("type") == "reasoning" for item in turn):
        return None
    return "The `reasoning_text` in the thinking mode must be passed back to the API."


class TestResumedToolRoundIsRepaired:
    def test_native_reasoning_item_is_synthesized(self, provider: OpenAIResponsesProvider) -> None:
        items = _items(provider, _resumed_round())
        synthesized = _reasoning_items(items)
        assert len(synthesized) == 1
        item = synthesized[0]
        assert item["id"].startswith(_ID_PREFIX)
        assert item["summary"] == [{"type": "summary_text", "text": _PLACEHOLDER}]
        assert item["content"] == [{"type": "reasoning_text", "text": _PLACEHOLDER}]

    def test_item_lands_inside_the_round_before_the_calls(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        items = _items(provider, _resumed_round())
        call_index = next(i for i, it in enumerate(items) if it.get("type") == "function_call")
        reasoning_index = next(i for i, it in enumerate(items) if it.get("type") == "reasoning")
        user_index = max(
            i
            for i, it in enumerate(items)
            if it.get("type") == "message" and it.get("role") == "user"
        )
        assert user_index < reasoning_index < call_index
        assert reasoning_index == call_index - 1
        assert items[-1]["type"] == "function_call_output"

    def test_turn_with_assistant_text_also_repairs(self, provider: OpenAIResponsesProvider) -> None:
        items = _items(provider, _resumed_round(with_text=True))
        assert len(_reasoning_items(items)) == 1
        call_index = next(i for i, it in enumerate(items) if it.get("type") == "function_call")
        assert items[call_index - 1].get("type") == "message"

    def test_carries_reasoning_text_content_not_summary_only(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        """A summary-only item does not satisfy the contract; content must be present."""
        item = _reasoning_items(_items(provider, _resumed_round()))[0]
        assert item["content"] and item["content"][0]["type"] == "reasoning_text"
        assert item["summary"] and item["summary"][0]["type"] == "summary_text"

    def test_multi_call_turn_gets_exactly_one_item(self, provider: OpenAIResponsesProvider) -> None:
        messages = [
            {"role": "user", "content": "Paris and London?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tc(_CALL_1), _tc(_CALL_2, '{"city": "London"}')],
            },
            {"role": "tool", "tool_call_id": _CALL_1, "content": _TOOL_OUTPUT},
            {"role": "tool", "tool_call_id": _CALL_2, "content": "11C, rain."},
        ]
        items = _items(provider, messages)
        assert len(_reasoning_items(items)) == 1

    def test_only_the_current_round_is_repaired(self, provider: OpenAIResponsesProvider) -> None:
        """An earlier reasoning-less tool round must not be decorated."""
        items = _items(provider, _two_round_history())
        synthesized = _reasoning_items(items)
        assert len(synthesized) == 1
        call_indices = [i for i, it in enumerate(items) if it.get("type") == "function_call"]
        reasoning_index = next(i for i, it in enumerate(items) if it.get("type") == "reasoning")
        assert reasoning_index > call_indices[0], "historical call turn was decorated"
        assert reasoning_index == call_indices[-1] - 1


class TestStoredReasoningIsPreserved:
    def test_stored_item_is_replayed_verbatim(self, provider: OpenAIResponsesProvider) -> None:
        items = _items(provider, _stored_reasoning_round())
        replayed = [it for it in _reasoning_items(items) if it.get("id") == "rs_stored_abc"]
        assert len(replayed) == 1
        assert replayed[0]["summary"] == [
            {"type": "summary_text", "text": "stored chain of thought"}
        ]
        assert replayed[0]["encrypted_content"] == "enc-blob"
        assert "status" not in replayed[0]

    def test_no_synthetic_duplicate_is_added(self, provider: OpenAIResponsesProvider) -> None:
        items = _items(provider, _stored_reasoning_round())
        assert len(_reasoning_items(items)) == 1
        assert not any(
            it.get("type") == "reasoning" and str(it.get("id", "")).startswith(_ID_PREFIX)
            for it in items
        )


class TestNegativeControls:
    """Every neighbouring shape must come back byte-for-byte unchanged."""

    def test_replay_gate_off_changes_nothing(self, provider: OpenAIResponsesProvider) -> None:
        messages = _resumed_round()
        gated_off = _payload(provider, messages, replay=False)
        assert not _reasoning_items(gated_off["input"])
        assert "include" not in gated_off, "replay-off must not request encrypted reasoning"

    def test_plain_user_turn_is_not_decorated(self, provider: OpenAIResponsesProvider) -> None:
        messages = [
            {"role": "user", "content": "Hi there."},
            {"role": "assistant", "content": "Hello! How can I help?"},
            {"role": "user", "content": "What is the weather in Paris?"},
        ]
        items = _items(provider, messages)
        assert _reasoning_items(items) == []
        assert [it["type"] for it in items] == ["message", "message", "message"]

    def test_completed_round_is_not_decorated(self, provider: OpenAIResponsesProvider) -> None:
        """A round that ends on an assistant answer carries no requirement."""
        messages = [*_resumed_round(), {"role": "assistant", "content": "It is 18C and clear."}]
        items = _items(provider, messages)
        assert _reasoning_items(items) == []

    def test_unanswered_tool_calls_are_not_decorated(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        """A trailing call block is not a resumed round (no outputs to continue from)."""
        messages = [
            {"role": "user", "content": "Weather in Paris?"},
            {"role": "assistant", "content": "", "tool_calls": [_tc(_CALL_1)]},
        ]
        items = _items(provider, messages)
        assert _reasoning_items(items) == []

    def test_mispaired_trailing_output_is_not_repaired(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        """A stale output answered by an EARLIER turn must fail closed."""
        items = [
            {"type": "message", "role": "user", "content": "Weather in Paris?"},
            {
                "type": "function_call",
                "call_id": _CALL_1,
                "name": "get_weather",
                "arguments": _ARGS,
            },
            {"type": "function_call_output", "call_id": _CALL_1, "output": _TOOL_OUTPUT},
            {"type": "message", "role": "user", "content": "And London?"},
            {"type": "function_call", "call_id": _CALL_2, "name": "get_weather", "arguments": "{}"},
            {"type": "function_call_output", "call_id": _CALL_1, "output": _TOOL_OUTPUT},
        ]
        assert _ensure_responses_round_reasoning_items(items) == items

    def test_output_without_a_call_in_its_turn_is_not_repaired(self) -> None:
        items = [
            {"type": "message", "role": "user", "content": "Hi."},
            {"type": "message", "role": "assistant", "content": "Done."},
            {"type": "function_call_output", "call_id": _CALL_1, "output": _TOOL_OUTPUT},
        ]
        assert _ensure_responses_round_reasoning_items(items) == items

    def test_non_string_call_ids_fail_closed(self) -> None:
        items = [
            {"type": "message", "role": "user", "content": "Hi."},
            {"type": "function_call", "call_id": None, "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": None, "output": "x"},
        ]
        assert _ensure_responses_round_reasoning_items(items) == items

    def test_dict_without_type_key_is_not_repaired(self) -> None:
        items: list[dict[str, Any]] = [
            {"type": "message", "role": "user", "content": "Hi."},
            {},
            {"type": "function_call_output", "call_id": _CALL_1, "output": "x"},
        ]
        assert _ensure_responses_round_reasoning_items(items) == items

    def test_history_without_a_user_message_fails_closed(self) -> None:
        items = [
            {"type": "function_call", "call_id": _CALL_1, "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": _CALL_1, "output": "x"},
        ]
        assert _ensure_responses_round_reasoning_items(items) == items

    def test_empty_and_output_only_inputs_are_unchanged(self) -> None:
        assert _ensure_responses_round_reasoning_items([]) == []
        only_outputs = [
            {"type": "function_call_output", "call_id": _CALL_1, "output": "x"},
        ]
        assert _ensure_responses_round_reasoning_items(only_outputs) == only_outputs


class TestDeterminismAndIdempotence:
    def test_item_is_deterministic_across_runs(self, provider: OpenAIResponsesProvider) -> None:
        first = _reasoning_items(_items(provider, _resumed_round()))[0]
        second = _reasoning_items(_items(provider, _resumed_round()))[0]
        assert first == second

    def test_id_is_fixed_length_hex(self, provider: OpenAIResponsesProvider) -> None:
        item_id = _reasoning_items(_items(provider, _resumed_round()))[0]["id"]
        digest = item_id[len(_ID_PREFIX) :]
        assert len(digest) == _DIGEST_CHARS
        assert all(c in "0123456789abcdef" for c in digest)

    def test_id_binds_the_answered_call_ids(self, provider: OpenAIResponsesProvider) -> None:
        one = _reasoning_items(_items(provider, _resumed_round()))[0]["id"]
        other_round = _two_round_history()
        other = _reasoning_items(_items(provider, other_round))[0]["id"]
        assert one != other, "distinct call ids must not share a synthesized id"

    def test_pass_is_idempotent(self) -> None:
        items = [
            {"type": "message", "role": "user", "content": "Weather in Paris?"},
            {
                "type": "function_call",
                "call_id": _CALL_1,
                "name": "get_weather",
                "arguments": _ARGS,
            },
            {"type": "function_call_output", "call_id": _CALL_1, "output": _TOOL_OUTPUT},
        ]
        once = _ensure_responses_round_reasoning_items(items)
        assert len(_reasoning_items(once)) == 1
        twice = _ensure_responses_round_reasoning_items(once)
        assert twice == once
        assert twice is once, "a satisfied round must return the input object untouched"

    def test_transform_does_not_mutate_its_input(self) -> None:
        items = [
            {"type": "message", "role": "user", "content": "Weather in Paris?"},
            {
                "type": "function_call",
                "call_id": _CALL_1,
                "name": "get_weather",
                "arguments": _ARGS,
            },
            {"type": "function_call_output", "call_id": _CALL_1, "output": _TOOL_OUTPUT},
        ]
        before = json.loads(json.dumps(items))
        _ensure_responses_round_reasoning_items(items)
        assert items == before


class TestStrictUpstreamContract:
    """Before/after against the strict-thinking rule, hermetically."""

    def test_pre_fix_shape_is_rejected(
        self,
        provider: OpenAIResponsesProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the repair removed, the resumed round reproduces the 400 class."""
        monkeypatch.setattr(
            responses_module,
            "_ensure_responses_round_reasoning_items",
            lambda items: items,
        )
        payload = _payload(provider, _resumed_round())
        assert _strict_upstream_rejection(payload) is not None
        assert not _reasoning_items(payload["input"])

    def test_repaired_shape_is_accepted(self, provider: OpenAIResponsesProvider) -> None:
        payload = _payload(provider, _resumed_round())
        assert _strict_upstream_rejection(payload) is None
        assert len(_reasoning_items(payload["input"])) == 1

    def test_compaction_shaped_resume_is_repaired_end_to_end(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        """The resumed round continues after an in-band summarization turn.

        The summary text is a stand-in; the property under test is that the
        resumed turn carries no recorded reasoning, which is what an in-band
        compaction written by a non-thinking lane leaves behind.
        """
        messages = [
            {"role": "user", "content": "Summarize what we have so far."},
            {"role": "assistant", "content": "[compacted summary of the earlier turn]"},
            {"role": "user", "content": "Weather in Paris?"},
            {"role": "assistant", "content": "", "tool_calls": [_tc(_CALL_1)]},
            {"role": "tool", "tool_call_id": _CALL_1, "content": _TOOL_OUTPUT},
        ]
        payload = _payload(provider, messages)
        assert _strict_upstream_rejection(payload) is None
        items = payload["input"]
        assert len(_reasoning_items(items)) == 1
        reasoning_index = next(i for i, it in enumerate(items) if it.get("type") == "reasoning")
        last_user = max(
            i
            for i, it in enumerate(items)
            if it.get("type") == "message" and it.get("role") == "user"
        )
        assert reasoning_index > last_user, "repair escaped its round"
        assert payload["include"] == ["reasoning.encrypted_content"]


class TestSynthesizedItemSchema:
    def test_matches_the_native_responses_reasoning_item_shape(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        item = _reasoning_items(_items(provider, _resumed_round()))[0]
        assert set(item) == {"type", "id", "summary", "content"}
        assert item["type"] == "reasoning"
        assert isinstance(item["id"], str) and item["id"]
        assert isinstance(item["summary"], list)
        assert isinstance(item["content"], list)
        for part in [*item["summary"], *item["content"]]:
            assert isinstance(part, dict)
            assert part["type"] in {"summary_text", "reasoning_text"}
            assert isinstance(part["text"], str) and part["text"]

    def test_replayed_item_carries_no_server_only_fields(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        for item in _reasoning_items(_items(provider, _stored_reasoning_round())):
            assert "status" not in item, "status is server-only on a replayed item"

    def test_whole_payload_is_json_serializable(self, provider: OpenAIResponsesProvider) -> None:
        payload = _payload(provider, _resumed_round(with_text=True))
        assert json.loads(json.dumps(payload)) == json.loads(json.dumps(payload, default=str))
        json.dumps(payload)

    def test_payload_input_types_and_order_are_preserved(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        items = _items(provider, _resumed_round())
        assert [it["type"] for it in items] == [
            "message",
            "reasoning",
            "function_call",
            "function_call_output",
        ]
        assert items[-1]["call_id"] == _CALL_1


# --------------------------------------------------------------------------- #
# Endpoint selection.  The repair is a property of the ENDPOINT, not of the
# replay flag.  The commercial OpenAI Responses path replays reasoning happily
# and still rejects a synthesized item under ``store: false`` -- 400
# ``array_above_max_length`` when the item carries ``content``, 404 when it does
# not (a fabricated ``rs_roundrepair_...`` id cannot be resolved) -- while the
# same tool continuation is ACCEPTED with valid encrypted reasoning, or with the
# reasoning item omitted.  These tests pin the selection so a replay-enabled
# lane can never be handed the repair by accident.
# --------------------------------------------------------------------------- #


def _commercial_upstream_rejection(payload: dict[str, Any]) -> str | None:
    """Reason the commercial path would reject *payload*, or ``None`` if accepted.

    Encodes the measured rule for gpt-6-astra / gpt-5.6-sol with ``store: false``.
    A PROXY for that endpoint class: it reproduces the rejection shapes, it is not
    the live server.
    """
    for item in payload.get("input") or []:
        if item.get("type") != "reasoning":
            continue
        if not str(item.get("id") or "").startswith(_ID_PREFIX):
            continue
        if item.get("content"):
            return "400 array_above_max_length: input[1].content must have a maximum length of zero"
        return f"404: reasoning item {item['id']} not found (store: false)"
    return None


class TestRepairIsSelectedByEndpointCapability:
    def test_capability_off_leaves_the_tool_round_untouched(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        """No declaration -> no repair, even with replay enabled."""
        payload = _payload(
            provider, _resumed_round(), caps=_caps(synthesizes_round_reasoning=False)
        )
        assert _reasoning_items(payload["input"]) == []

    def test_capability_off_keeps_the_item_list_byte_for_byte(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        no_cap = _payload(
            provider,
            _resumed_round(with_text=True),
            caps=_caps(synthesizes_round_reasoning=False),
        )["input"]
        no_replay = _payload(
            provider,
            _resumed_round(with_text=True),
            replay=False,
            caps=_caps(synthesizes_round_reasoning=False),
        )["input"]
        assert no_cap == no_replay

    def test_capability_on_is_what_makes_the_commercial_path_reject(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        """The defect this gate exists to prevent: an ungated repair 400s upstream."""
        ungated = _payload(provider, _resumed_round())  # capability declared on
        assert _commercial_upstream_rejection(ungated) is not None
        gated = _payload(provider, _resumed_round(), caps=_caps(synthesizes_round_reasoning=False))
        assert _commercial_upstream_rejection(gated) is None

    def test_replay_off_wins_over_the_capability(self, provider: OpenAIResponsesProvider) -> None:
        payload = _payload(provider, _resumed_round(), replay=False)
        assert _reasoning_items(payload["input"]) == []

    def test_capability_is_off_by_default(self) -> None:
        """An undeclared endpoint keeps the exact upstream item list."""
        assert ModelCapabilities().synthesizes_round_reasoning is False

    def test_declared_by_capability_not_by_replay_flag(self) -> None:
        """The two questions are independent: replay can be on with the repair off."""
        assert _caps(synthesizes_round_reasoning=False).supports_reasoning_replay is True
