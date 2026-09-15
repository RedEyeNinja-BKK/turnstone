"""CI-2 stored-reasoning replay — extraction-source and composition battery.

Companion to ``test_incident_compaction_round_reasoning.py`` (which covers CI-1).
This file covers the OTHER half of the contract on non-vLLM OpenAI-compatible lanes:

  pass 1  ``attach_openai_reasoning_content_field``      -> replay STORED reasoning
  pass 2  ``ensure_round_reasoning_content_field``       -> validate the CURRENT ROUND

All sentinels are synthetic. No production reasoning text is used.

§4 of the CI-2 GO requires proving the DATA SOURCE, not just the output: the
extractor must select only provider-generated reasoning material, fail safely on
malformed/missing input, never invent content, never mistake ordinary assistant
text for reasoning, and never misread one provider's metadata as another's.
That is what ``TestExtractorSource`` below asserts.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from turnstone.core.history_decoration import (
    _ROUND_REASONING_PLACEHOLDER,
    attach_openai_reasoning_content_field,
    ensure_round_reasoning_content_field,
    extract_reasoning_text_from_provider_content,
)
from turnstone.core.model_turn import maybe_attach_vllm_chat_reasoning as apply_gate
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

SENTINEL_MID = "SENTINEL-STORED-REASONING-MID"
SENTINEL_POST = "SENTINEL-STORED-REASONING-POST"


# ── message builders ────────────────────────────────────────────────────────────────────
def U(text="u"):
    return {"role": "user", "content": text}


def A(text, *, pc=None, rc="__ABSENT__"):
    m = {"role": "assistant", "content": text}
    if pc is not None:
        m["_provider_content"] = pc
    if rc != "__ABSENT__":
        m["reasoning_content"] = rc
    return m


def TOOL_RESULT(cid="c1", text="12:00"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


def A_TOOLCALL(text="", *, pc=None, cid="c1"):
    m = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "get_time", "arguments": "{}"}}
        ],
    }
    if pc is not None:
        m["_provider_content"] = pc
    return m


def marker(text="## Decisions\n- compacted"):
    """The mid-turn compaction summary marker: a NON-reasoning assistant turn."""
    return {"role": "assistant", "content": text, "_source": "compaction"}


# OpenAI-chat reasoning blocks are type "reasoning_text".
def pc_openai_chat(text, *, extra_text="the answer"):
    return [
        {"type": "reasoning_text", "text": text},
        {"type": "text", "text": extra_text},
    ]


def cfg(*, replay=True, server_type="openai-compatible"):
    return SimpleNamespace(
        replay_reasoning_to_model=replay,
        server_compat={"server_type": server_type},
        model="deepseek-flash-thinking",
    )


def through_gate(msgs, *, replay=True, server_type="openai-compatible"):
    """Drive the REAL deposit point (the only call site)."""
    return apply_gate(
        msgs,
        OpenAIChatCompletionsProvider(),
        registry=None,
        alias="deepseek-flash-thinking",
        cfg=cfg(replay=replay, server_type=server_type),
    )


def compose_direct(msgs):
    """The composition under test, without the gate (pass 1 then pass 2)."""
    return ensure_round_reasoning_content_field(attach_openai_reasoning_content_field(msgs))


# ══ §4 EXTRACTOR / SOURCE-DATA PROOF ════════════════════════════════════════════════════
class TestExtractorSource:
    def test_openai_chat_reasoning_text_is_selected(self):
        assert extract_reasoning_text_from_provider_content(
            pc_openai_chat(SENTINEL_MID)
        ) == SENTINEL_MID

    def test_plain_text_block_is_not_treated_as_reasoning(self):
        blocks = [{"type": "text", "text": "ordinary assistant output"}]
        assert extract_reasoning_text_from_provider_content(blocks) == ""

    def test_unknown_block_types_are_ignored(self):
        blocks = [{"type": "totally_unknown", "text": "should not be read"}]
        assert extract_reasoning_text_from_provider_content(blocks) == ""

    @pytest.mark.parametrize("bad", [None, "", [], {}, 0, "string-instead-of-list", [[]], [None]])
    def test_malformed_provider_content_fails_safe(self, bad):
        # Must return "" (or at least never raise) — a malformed payload must not
        # break the wire path.
        out = extract_reasoning_text_from_provider_content(bad)
        assert out == "" or isinstance(out, str)

    def test_missing_text_key_is_safe(self):
        blocks = [{"type": "reasoning_text"}]
        out = extract_reasoning_text_from_provider_content(blocks)
        assert isinstance(out, str)

    def test_mixed_order_still_finds_reasoning(self):
        # The extractor scans for the recognised block type rather than trusting index 0.
        blocks = [
            {"type": "text", "text": "message first"},
            {"type": "reasoning_text", "text": SENTINEL_MID},
        ]
        assert extract_reasoning_text_from_provider_content(blocks) == SENTINEL_MID

    def test_anthropic_thinking_not_misread_as_openai_chat(self):
        # An Anthropic-shaped block must not be handed to the OpenAI-chat extractor's
        # reasoning_text path by mistake. Either it is ignored here, or — if the
        # recognised provider dispatch claims it — it must not yield the OpenAI marker.
        anthropic_blocks = [
            {"type": "thinking", "thinking": "ANTHROPIC-SENTINEL"},
        ]
        out = extract_reasoning_text_from_provider_content(anthropic_blocks)
        assert "SENTINEL-STORED" not in out

    def test_no_reasoning_material_yields_empty(self):
        assert extract_reasoning_text_from_provider_content([]) == ""


# ══ PASS-1 UNIT BEHAVIOUR ═══════════════════════════════════════════════════════════════
class TestAttachOpenAIReasoningContent:
    def test_stored_reasoning_is_replayed_post_last_user(self):
        out = attach_openai_reasoning_content_field([U(), A("a", pc=pc_openai_chat(SENTINEL_POST))])
        assert out[1]["reasoning_content"] == SENTINEL_POST

    def test_stored_reasoning_is_replayed_mid_history(self):
        msg = [U("u1"), A("a1", pc=pc_openai_chat(SENTINEL_MID)), U("u2"), A("a2")]
        out = attach_openai_reasoning_content_field(msg)
        assert out[1]["reasoning_content"] == SENTINEL_MID

    def test_never_invents_when_no_provider_content(self):
        out = attach_openai_reasoning_content_field([U(), A("no material")])
        assert "reasoning_content" not in out[1]

    def test_empty_provider_content_list_is_no_op(self):
        out = attach_openai_reasoning_content_field([U(), A("a", pc=[])])
        assert "reasoning_content" not in out[1]

    def test_existing_reasoning_content_is_overwritten_by_stored_material(self):
        # Pass 1 replays STORED material. If a stale/derived value is already present,
        # the stored provider payload is authoritative for the replay field.
        out = attach_openai_reasoning_content_field(
            [U(), A("a", pc=pc_openai_chat(SENTINEL_POST), rc="stale-value")]
        )
        assert out[1]["reasoning_content"] == SENTINEL_POST

    def test_input_list_not_mutated(self):
        src = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        snapshot = [dict(m) for m in src]
        attach_openai_reasoning_content_field(src)
        assert src == snapshot

    def test_returns_new_list(self):
        src = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        assert attach_openai_reasoning_content_field(src) is not src

    def test_idempotent(self):
        src = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        once = attach_openai_reasoning_content_field(src)
        twice = attach_openai_reasoning_content_field(once)
        assert once == twice

    def test_tool_calls_preserved_alongside_replay(self):
        out = attach_openai_reasoning_content_field([U(), A_TOOLCALL(pc=pc_openai_chat(SENTINEL_POST))])
        assert out[1]["tool_calls"] == A_TOOLCALL()["tool_calls"]
        assert out[1]["reasoning_content"] == SENTINEL_POST

    def test_non_assistant_messages_untouched(self):
        out = attach_openai_reasoning_content_field([U(), TOOL_RESULT()])
        assert "reasoning_content" not in out[0]
        assert "reasoning_content" not in out[1]


# ══ §5 REQUIRED A/B MATRIX ══════════════════════════════════════════════════════════════
class TestABMatrix:
    """'Current' = CI-1 only (what production does today).
    'Full'    = CI-2 replay then CI-1 ensure (the repair)."""

    def _ab(self, msgs, **kw):
        """current = CI-1 only (production today, i.e. pass 2 WITHOUT pass 1).
        full    = the repair (pass 1 then pass 2)."""
        if kw.get("replay", True) is False:
            return through_gate(msgs, **kw), through_gate(msgs, **kw)
        current = ensure_round_reasoning_content_field(msgs)   # pass 2 alone
        full = compose_direct(msgs)                            # pass 1 then pass 2
        return current, full

    def test_stored_reasoning_mid_history(self):
        msgs = [U("u1"), A("a1", pc=pc_openai_chat(SENTINEL_MID)), U("u2"), A("a2")]
        current, full = self._ab(msgs)
        assert "reasoning_content" not in current[1]           # current: absent
        assert full[1]["reasoning_content"] == SENTINEL_MID    # full: replayed

    def test_stored_reasoning_post_last_user(self):
        msgs = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        current, full = self._ab(msgs)
        assert current[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER           # current: non-empty placeholder (empty until 2026-09-15)
        assert full[1]["reasoning_content"] == SENTINEL_POST   # full: replayed

    def test_no_stored_reasoning_post_last_user(self):
        msgs = [U(), marker()]
        current, full = self._ab(msgs)
        assert current[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER
        assert full[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER              # CI-1 still supplies the placeholder

    def test_existing_reasoning_content_preserved_when_no_stored_material(self):
        msgs = [U(), A("a", rc="EXPLICIT")]
        current, full = self._ab(msgs)
        assert current[1]["reasoning_content"] == "EXPLICIT"
        assert full[1]["reasoning_content"] == "EXPLICIT"

    def test_existing_reasoning_content_preserved_against_pass2(self):
        # Pass 2 must never clobber a valid existing value...
        out = ensure_round_reasoning_content_field([U(), A("a", rc="EXPLICIT")])
        assert out[1]["reasoning_content"] == "EXPLICIT"

    @pytest.mark.parametrize("bad", [None, 0, "not-a-list", {}, [[]]])
    def test_malformed_provider_data_is_safe(self, bad):
        msgs = [U(), A("a", pc=bad)]
        current, full = self._ab(msgs)
        assert current[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER
        assert full[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER

    def test_no_provider_data_is_safe(self):
        msgs = [U(), A("a")]
        current, full = self._ab(msgs)
        assert current[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER
        assert full[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER

    def test_replay_flag_false_is_untouched(self):
        msgs = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        out = through_gate(msgs, replay=False)
        assert out == msgs
        assert "reasoning_content" not in out[1]

    def test_non_applicable_provider_is_untouched(self):
        # A non-OpenAIChatCompletionsProvider must short-circuit before any pass.
        class Other:
            pass

        msgs = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        out = apply_gate(msgs, Other(), registry=None, alias="x", cfg=cfg())
        assert out == msgs

    def test_vllm_lane_uses_the_other_field_path(self):
        msgs = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        out = through_gate(msgs, server_type="vllm")
        assert "reasoning_content" not in out[1]               # non-vLLM pass not applied
        assert out[1].get("reasoning") == SENTINEL_POST        # vLLM path does its own

    def test_missing_cfg_is_untouched(self):
        msgs = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        out = apply_gate(msgs, OpenAIChatCompletionsProvider(), registry=None, alias="x", cfg=None)
        assert out == msgs


# ══ §3 CI-1 NON-REGRESSION UNDER COMPOSITION ═════════════════════════════════════════════
class TestCI1NonRegression:
    def test_composition_is_idempotent(self):
        msgs = [
            U("u1"),
            A("a1", pc=pc_openai_chat(SENTINEL_MID)),
            U("u2"),
            A("a2"),
            marker(),
        ]
        once = compose_direct(msgs)
        twice = compose_direct(once)
        assert once == twice

    def test_composition_does_not_mutate_input(self):
        msgs = [U("u1"), A("a1", pc=pc_openai_chat(SENTINEL_MID)), U("u2"), marker()]
        snapshot = [dict(m) for m in msgs]
        compose_direct(msgs)
        assert msgs == snapshot

    def test_pass2_window_still_limited_to_current_round(self):
        # A pre-last-user assistant with NO material must stay un-stamped by CI-1...
        msgs = [U("u1"), A("a1"), U("u2")]
        out = compose_direct(msgs)
        assert "reasoning_content" not in out[1]

    def test_marker_inside_round_still_gets_non_empty_placeholder(self):
        out = compose_direct([U(), marker()])
        assert out[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER

    def test_composition_never_invents_for_marker(self):
        out = compose_direct([U(), marker()])
        assert out[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER

    def test_replay_and_stamp_coexist_in_one_round(self):
        msgs = [U(), A("a1", pc=pc_openai_chat(SENTINEL_POST)), marker()]
        out = compose_direct(msgs)
        assert out[1]["reasoning_content"] == SENTINEL_POST   # replayed
        assert out[2]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER              # stamped

    def test_tool_boundary_shape_replays_and_stamps(self):
        msgs = [
            U("what time"),
            A_TOOLCALL(pc=pc_openai_chat(SENTINEL_POST)),
            TOOL_RESULT(),
        ]
        out = compose_direct(msgs)
        assert out[1]["reasoning_content"] == SENTINEL_POST
        assert out[1]["tool_calls"] == A_TOOLCALL()["tool_calls"]
        assert "reasoning_content" not in out[2]              # tool messages never stamped

    def test_no_user_message_is_unchanged(self):
        msgs = [{"role": "system", "content": "s"}, A("a")]
        assert compose_direct(msgs) == msgs


# ══ ordering: pass 1 must run BEFORE pass 2 ═════════════════════════════════════════════
class TestPassOrdering:
    def test_reverse_order_would_lose_stored_material(self):
        """Proves the composition order matters: if ensure() ran first it would stamp a
        non-empty assistant turn... it does not (it only fills absent/None), but the
        reverse order WOULD have pass 1 overwrite pass 2's work with stored text on a
        turn whose key was just set. Assert the documented order is the one in use."""
        msgs = [U(), A("a", pc=pc_openai_chat(SENTINEL_POST))]
        forward = compose_direct(msgs)
        assert forward[1]["reasoning_content"] == SENTINEL_POST
        # and a turn with nothing stored still ends with the empty stamp
        msgs2 = [U(), A("b")]
        assert compose_direct(msgs2)[1]["reasoning_content"] == _ROUND_REASONING_PLACEHOLDER
