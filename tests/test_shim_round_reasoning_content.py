"""Deterministic tests for the TEMPORARY v1.8.4 transition compatibility shim.

Covers: no-op without a boundary; single/multiple stamping; non-overwrite of existing
reasoning; no touching of pre-boundary assistant turns; idempotency; and REAL serialization
through the runtime's own composition entry point (not the helper in isolation).
"""

from __future__ import annotations

import json

from turnstone.core.history_decoration import ensure_round_reasoning_content_field
from turnstone.core.model_turn import maybe_attach_vllm_chat_reasoning

U = {"role": "user", "content": "u"}
A = {"role": "assistant", "content": "a"}


# ── A. no assistant after the last user -> no mutation ────────────────────────────────
def test_a_no_assistant_after_last_user_is_unchanged():
    msgs = [U, A, U]
    out = ensure_round_reasoning_content_field(msgs)
    assert out == msgs
    assert all("reasoning_content" not in m for m in out)


# ── B. one assistant after the last user without the field -> "" ──────────────────────
def test_b_single_post_user_assistant_is_stamped():
    msgs = [U, {"role": "assistant", "content": "trailing"}]
    out = ensure_round_reasoning_content_field(msgs)
    assert out[-1]["reasoning_content"] == ""
    assert "reasoning_content" not in out[0]


# ── C. multiple assistant messages after the last user -> all stamped ─────────────────
def test_c_all_post_user_assistants_are_stamped():
    msgs = [U, A, U, {"role": "assistant", "content": "x"},
            {"role": "tool", "content": "t"}, {"role": "assistant", "content": "y"}]
    out = ensure_round_reasoning_content_field(msgs)
    assert out[3]["reasoning_content"] == ""
    assert out[5]["reasoning_content"] == ""
    assert out[2 - 1].get("reasoning_content") is None  # pre-boundary untouched
    assert out[4]["role"] == "tool" and "reasoning_content" not in out[4]


# ── D. existing reasoning content is never overwritten (None IS repaired) ─────────────
def test_d_existing_value_is_never_overwritten():
    msgs = [U, {"role": "assistant", "content": "a", "reasoning_content": "REAL"},
            {"role": "assistant", "content": "b", "reasoning_content": None},
            {"role": "assistant", "content": "c"}]
    out = ensure_round_reasoning_content_field(msgs)
    assert out[1]["reasoning_content"] == "REAL"
    assert out[2]["reasoning_content"] == ""
    assert out[3]["reasoning_content"] == ""


# ── E. pre-boundary assistant turns are not modified ──────────────────────────────────
def test_e_pre_boundary_assistants_untouched():
    msgs = [U, {"role": "assistant", "content": "old"}, U]
    out = ensure_round_reasoning_content_field(msgs)
    assert "reasoning_content" not in out[1]
    assert out == msgs


# ── F. idempotency ────────────────────────────────────────────────────────────────────
def test_f_idempotent():
    msgs = [U, A, {"role": "assistant", "content": "p"}, {"role": "assistant", "content": "q"}]
    once = ensure_round_reasoning_content_field(msgs)
    twice = ensure_round_reasoning_content_field(once)
    assert once == twice
    assert ensure_round_reasoning_content_field(twice) == twice


# ── F2. pure transform: input never mutated ───────────────────────────────────────────
def test_f2_input_not_mutated():
    msgs = [U, {"role": "assistant", "content": "x"}]
    before = json.dumps(msgs)
    ensure_round_reasoning_content_field(msgs)
    assert json.dumps(msgs) == before


# ── G. REAL serialization through the runtime composition entry point ─────────────────
class _Cfg:
    """Minimal stand-in for a fetched ModelConfig on a non-vLLM lane."""
    server_compat = {"server_type": "openai"}
    replay_reasoning_to_model = True


class _CfgOff:
    server_compat = {"server_type": "openai"}
    replay_reasoning_to_model = False


def _provider():
    from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
    # instance without running __init__ — the gate only needs the type check
    return object.__new__(OpenAIChatCompletionsProvider)


def test_g_real_wire_payload_carries_the_field():
    msgs = [U, {"role": "assistant", "content": "trailing turn"}]
    out = maybe_attach_vllm_chat_reasoning(msgs, _provider(), None, "lane", cfg=_Cfg())
    payload = json.dumps(out)
    assert '"reasoning_content": ""' in payload, payload
    # and the pre-boundary turn must not have gained the field
    assert json.dumps(out).count("reasoning_content") == 1


def test_g2_operator_gate_off_means_no_shim():
    msgs = [U, {"role": "assistant", "content": "trailing turn"}]
    out = maybe_attach_vllm_chat_reasoning(msgs, _provider(), None, "lane", cfg=_CfgOff())
    assert "reasoning_content" not in json.dumps(out)


def test_g3_vllm_lane_uses_upstream_path_not_the_shim():
    class _VllmCfg:
        server_compat = {"server_type": "vllm"}
        replay_reasoning_to_model = True
    msgs = [U, {"role": "assistant", "content": "trailing turn"}]
    out = maybe_attach_vllm_chat_reasoning(msgs, _provider(), None, "lane", cfg=_VllmCfg())
    # upstream path projects persisted reasoning; with none stored the payload is unchanged
    assert "reasoning_content" not in json.dumps(out)
