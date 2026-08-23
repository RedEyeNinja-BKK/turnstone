"""Hermetic tests for the DeepSeek-thinking reasoning replay contract.

Incident 2026-08-23: the production 400 ("The `reasoning_content` in the
thinking mode must be passed back to the API") was caused by the replay
path dropping the stored provider ``reasoning_content`` before a
subsequent DeepSeek-thinking turn.

These tests pin the provider replay contract fix:

1. stored DeepSeek reasoning payload survives internal history
   representation (``_provider_content`` → dict round-trip);
2. serialization to a DeepSeek-thinking definition (replay declared)
   emits ``reasoning_content``;
3. serialization to Luna does NOT emit it (no contract);
4. serialization to DeepSeek NT does NOT emit it (no contract);
5. repeated DeepSeek-thinking turns replay the field every turn
   (the upstream-contract failure class is gone);
6. mixed-provider history stays valid — only turns with stored reasoning
   and a declaring target get the field;
7. tool-call histories stay valid (``tool_calls`` preserved, field added
   alongside, ordering intact);
8. historical messages with no reasoning payload pass through unchanged
   (no fabricated material);
9. provider reasoning material is never exposed as normal assistant
   content (``content`` unchanged; field is a sibling);
10. no global replay behavior is introduced (targets without the declared
    contract never receive the field).
"""

from __future__ import annotations

from types import SimpleNamespace

from turnstone.core.history_decoration import (
    attach_openai_reasoning_content_field,
    extract_reasoning_text_from_provider_content,
)
from turnstone.core.model_turn import (
    capability_flag,
    maybe_attach_openai_reasoning_content,
)
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.trajectory import Turn, Role, turn_to_dict


def _caps(*, supports_replay: bool = False) -> ModelCapabilities:
    """Resolved-capabilities object shaped like the production registry lane
    carries (``ModelCapabilities`` after operator overrides)."""
    return ModelCapabilities(supports_reasoning_replay=supports_replay)


def _reasoning_block(text: str) -> dict:
    return {"type": "reasoning_text", "text": text}


def _ds_thinking_message(*, content: str = "Visible answer", reasoning: str = "private chain"):
    return {
        "role": "assistant",
        "content": content,
        "_provider_content": [_reasoning_block(reasoning)],
    }


def _cfg(*, replay: bool, supports_replay: bool, server_type: str = "openai-compatible") -> SimpleNamespace:
    capabilities = {}
    if supports_replay:
        capabilities["supports_reasoning_replay"] = True
    return SimpleNamespace(
        replay_reasoning_to_model=replay,
        capabilities=capabilities,
        server_compat={"server_type": server_type},
        model="deepseek-flash-thinking",
    )


def _replay(
    msgs,
    *,
    replay: bool,
    caps: ModelCapabilities | None,
    server_type: str = "openai-compatible",
):
    """Invoke the gated attach the way the wire path does: cfg carries the
    operator flag and declared capabilities; *caps* is the lane's resolved
    ``ModelCapabilities`` (or ``None`` to exercise the resolve fallback)."""
    cfg = SimpleNamespace(
        replay_reasoning_to_model=replay,
        capabilities={"supports_reasoning_replay": bool(caps and caps.supports_reasoning_replay)},
        server_compat={"server_type": server_type},
        model="deepseek-flash-thinking",
    )
    return maybe_attach_openai_reasoning_content(
        msgs,
        OpenAIChatCompletionsProvider(),
        registry=None,
        alias="deepseek-flash-thinking",
        cfg=cfg,
        caps=caps,
    )


# ---------------------------------------------------------------------------
# 1. Stored reasoning survives internal representation
# ---------------------------------------------------------------------------


class TestStorageSurvival:
    def test_reasoning_payload_survives_turn_round_trip(self):
        turn = Turn(
            Role.ASSISTANT,
            (),
            native=SimpleNamespace(blocks=[_reasoning_block("private chain")], producer=None),
        )
        d = turn_to_dict(turn)
        assert d["_provider_content"] == [_reasoning_block("private chain")]
        text = extract_reasoning_text_from_provider_content(d["_provider_content"])
        assert text == "private chain"


# ---------------------------------------------------------------------------
# 2-5. Gate behavior on the wire path
# ---------------------------------------------------------------------------


class TestWireReplay:
    def test_ds_thinking_emits_reasoning_content(self):
        msgs = [_ds_thinking_message()]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True))
        assert out[0]["reasoning_content"] == "private chain"
        assert out[0]["content"] == "Visible answer"

    def test_luna_does_not_emit(self):
        msgs = [_ds_thinking_message()]
        out = _replay(msgs, replay=False, caps=_caps())
        assert "reasoning_content" not in out[0]

    def test_ds_nt_does_not_emit(self):
        msgs = [_ds_thinking_message()]
        out = _replay(msgs, replay=False, caps=_caps())
        assert "reasoning_content" not in out[0]

    def test_repeated_ds_thinking_turns_replay_every_turn(self):
        # Two consecutive DeepSeek-thinking turns: the prior turn's stored
        # reasoning must be replayed on BOTH the intermediate and final wire.
        prior = _ds_thinking_message(reasoning="first chain")
        second = _ds_thinking_message(content="second answer", reasoning="second chain")
        msgs = [prior, second]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True))
        assert out[0]["reasoning_content"] == "first chain"
        assert out[1]["reasoning_content"] == "second chain"
        assert out[0]["content"] == "Visible answer"
        assert out[1]["content"] == "second answer"


# ---------------------------------------------------------------------------
# 6-7. Mixed-provider and tool-call history
# ---------------------------------------------------------------------------


class TestHistoryShapes:
    def test_mixed_history_only_declared_targets_get_field(self):
        luna_turn = {"role": "assistant", "content": "luna answer"}
        ds_turn = _ds_thinking_message(reasoning="ds chain")
        user = {"role": "user", "content": "next"}
        msgs = [luna_turn, ds_turn, user]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True))
        # Luna assistant turn has no stored reasoning → untouched.
        assert "reasoning_content" not in out[0]
        # DeepSeek turn gets the field.
        assert out[1]["reasoning_content"] == "ds chain"
        # User message untouched.
        assert out[2] == user

    def test_tool_call_history_preserved(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
                "_provider_content": [_reasoning_block("tool reasoning")],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True))
        assert out[0]["reasoning_content"] == "tool reasoning"
        assert out[0]["tool_calls"][0]["function"]["name"] == "lookup"
        assert out[1]["role"] == "tool"
        # Ordering preserved.
        assert [m["role"] for m in out] == ["assistant", "tool"]


# ---------------------------------------------------------------------------
# 8-9. No fabrication / no content pollution
# ---------------------------------------------------------------------------


class TestNoFabrication:
    def test_no_reasoning_payload_passes_through_unchanged(self):
        msgs = [{"role": "assistant", "content": "plain answer"}]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True))
        assert out == msgs
        assert "reasoning_content" not in out[0]

    def test_reasoning_never_becomes_visible_content(self):
        msgs = [_ds_thinking_message(reasoning="secret chain")]
        out = attach_openai_reasoning_content_field(msgs)
        assert out[0]["content"] == "Visible answer"
        assert out[0]["reasoning_content"] == "secret chain"
        assert "secret chain" not in (out[0]["content"] or "")


# ---------------------------------------------------------------------------
# 10. No global replay
# ---------------------------------------------------------------------------


class TestNoGlobalReplay:
    def test_missing_contract_never_attaches(self):
        msgs = [_ds_thinking_message()]
        # Operator flag on but capability gate off.
        out = _replay(msgs, replay=True, caps=_caps())
        assert "reasoning_content" not in out[0]
        # Capability gate on but operator flag off.
        out2 = _replay(msgs, replay=False, caps=_caps(supports_replay=True))
        assert "reasoning_content" not in out2[0]
        # No cfg at all (registry miss) → unchanged.
        out3 = maybe_attach_openai_reasoning_content(
            msgs,
            OpenAIChatCompletionsProvider(),
            registry=None,
            alias="deepseek-flash-thinking",
            cfg=None,
            caps=None,
        )
        assert "reasoning_content" not in out3[0]

    def test_vllm_server_uses_reasoning_field_not_reasoning_content(self):
        # A vLLM-declared definition must use the existing ``reasoning``
        # path, not the DeepSeek ``reasoning_content`` path.
        msgs = [_ds_thinking_message()]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True), server_type="vllm")
        assert "reasoning_content" not in out[0]


# ---------------------------------------------------------------------------
# Reviewer-closed gaps: real ModelCapabilities shape + capability_flag
# ---------------------------------------------------------------------------


class TestResolvedCapabilityShape:
    def test_real_modelcapabilities_object_emits(self):
        """The production lane carries the RESOLVED ``ModelCapabilities``
        object (static table merged with operator overrides).  Gate must read
        ``supports_reasoning_replay`` off that object and emit the field."""
        msgs = [_ds_thinking_message(reasoning="resolved chain")]
        out = _replay(msgs, replay=True, caps=_caps(supports_replay=True))
        assert out[0]["reasoning_content"] == "resolved chain"

    def test_capability_flag_reads_both_shapes(self):
        # Resolved object shape.
        assert capability_flag(_caps(supports_replay=True), "supports_reasoning_replay") is True
        assert capability_flag(_caps(), "supports_reasoning_replay") is False
        # Raw dict shape (operator-declared capabilities JSON).
        assert capability_flag({"supports_reasoning_replay": True}, "supports_reasoning_replay") is True
        assert capability_flag({"supports_reasoning_replay": False}, "supports_reasoning_replay") is False
        assert capability_flag({}, "supports_reasoning_replay") is False
        # None / non-capability shapes degrade without raising.
        assert capability_flag(None, "supports_reasoning_replay") is False
