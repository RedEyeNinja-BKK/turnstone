"""Incident 2026-09-11: mid-turn compaction 400 on the DeepSeek thinking lane.

Production failure (three times in one session, workstream
``4cffa9f38b454ce69f61a21033a1396f``): after an auto-compaction taken during a
tool loop, the next send returned HTTP 400 — "The `reasoning_content` in the
thinking mode must be passed back to the API."

Root cause (byte-faithful reproduction against the live lane, §probe below):
the resume slice is ``[user("[Conversation summary]"), assistant(marker),
assistant(tool call, reasoning), tool(...)]``.  The compaction summary marker is
written by the dedicated NON-thinking compaction lane, so it has no stored
``reasoning_content`` — and because the compaction happened mid-turn, the
synthetic summary label is the LAST user message, which puts the marker inside
the round the API validates.

Measured contract (shape matrix, 19 bodies, production lane):

* the API validates every ``assistant`` message positioned AFTER the last
  ``user`` message;
* an assistant turn before the last user message is NOT validated (a replayed
  reasoning-less turn is harmless there);
* ``reasoning_content: ""`` satisfies the contract for BOTH a reasoning-less
  content turn and a reasoning-less tool-call turn;
* ``reasoning_content`` is NOT required on a turn the model has not been asked
  to continue past -- appending a trailing user turn also cleared the 400.

These tests pin the repair and, equally, pin its blast radius: no text is ever
invented, nothing is attached when the gates are closed, and no message is
touched when there is no user boundary to scope the round.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from turnstone.core.history_decoration import (
    attach_openai_reasoning_content_field,
    ensure_round_reasoning_content_field,
)
from turnstone.core.model_turn import maybe_attach_vllm_chat_reasoning as maybe_attach_openai_reasoning_content
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._protocol import ModelCapabilities


def _caps(*, supports_replay: bool) -> ModelCapabilities:
    return ModelCapabilities(supports_reasoning_replay=supports_replay)


def _cfg(*, replay: bool, server_type: str = "openai-compatible") -> SimpleNamespace:
    return SimpleNamespace(
        replay_reasoning_to_model=replay,
        capabilities={"supports_reasoning_replay": True},
        server_compat={"server_type": server_type},
        model="deepseek-flash-thinking",
    )


def _replay(msgs, *, replay: bool = True, caps=None, server_type: str = "openai-compatible"):
    return maybe_attach_openai_reasoning_content(
        msgs,
        OpenAIChatCompletionsProvider(),
        registry=None,
        alias="deepseek-flash-thinking",
        cfg=_cfg(replay=replay, server_type=server_type),
    )


def _tc(call_id: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "bash", "arguments": "{}"}}


def _marker(text: str = "## Decisions\n- compacted") -> dict:
    """The compaction summary turn as reconstruction emits it."""
    return {"role": "assistant", "content": text, "_source": "compaction"}


def _tool_call_assistant(call_id: str, *, reasoning: str | None = None) -> dict:
    msg = {"role": "assistant", "content": "", "tool_calls": [_tc(call_id)]}
    if reasoning is not None:
        msg["_provider_content"] = [{"type": "reasoning_text", "text": reasoning}]
    return msg


def _tool_result(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


U = {"role": "user", "content": "go"}


# ---------------------------------------------------------------------------
# The production shape: mid-turn compaction resume
# ---------------------------------------------------------------------------


class TestMidTurnCompactionResume:
    def test_marker_inside_current_round_gets_empty_key(self):
        """The exact 400 body: marker after the last user, tool result trailing."""
        msgs = [U, _marker(), _tool_call_assistant("c1", reasoning="stored chain"),
                _tool_result("c1")]
        out = _replay(msgs)
        assert out[1]["reasoning_content"] == ""
        # The stored-reasoning turn keeps its real payload, not the empty stamp.
        assert out[2]["reasoning_content"] == "stored chain"
        # No content or tool_calls were disturbed.
        assert out[1]["content"] == _marker()["content"]
        assert out[2]["tool_calls"] == [_tc("c1")]

    def test_marker_before_last_user_is_untouched(self):
        """End-of-turn compaction: the marker precedes the user turn that starts
        the next round, so the API never validates it."""
        msgs = [U, _marker(), {"role": "user", "content": "next ask"}]
        out = _replay(msgs)
        assert "reasoning_content" not in out[1]

    def test_reasoning_less_tool_call_turn_in_round_gets_empty_key(self):
        """A turn whose upstream reply carried no reasoning (native=None)."""
        msgs = [U, _tool_call_assistant("c1"), _tool_result("c1")]
        out = _replay(msgs)
        assert out[1]["reasoning_content"] == ""

    def test_second_round_repair(self):
        msgs = [U, _tool_call_assistant("c1", reasoning="r"), _tool_result("c1"),
                {"role": "user", "content": "again"},
                _tool_call_assistant("c2"), _tool_result("c2")]
        out = _replay(msgs)
        assert out[1]["reasoning_content"] == "r"      # first round untouched
        assert out[4]["reasoning_content"] == ""       # current round repaired

    def test_trailing_user_turn_means_no_field_added(self):
        msgs = [U, _marker(), _tool_call_assistant("c1", reasoning="r"),
                _tool_result("c1"), {"role": "user", "content": "continue"}]
        out = _replay(msgs)
        assert "reasoning_content" not in out[1]
        assert out[2]["reasoning_content"] == "r"


# ---------------------------------------------------------------------------
# No fabrication, no over-reach
# ---------------------------------------------------------------------------


class TestNoFabrication:
    def test_stamp_is_empty_never_invented_text(self):
        out = ensure_round_reasoning_content_field([U, _marker()])
        assert out[1]["reasoning_content"] == ""

    def test_no_user_boundary_returns_input_unchanged(self):
        """No detectable round ⇒ no boundary ⇒ untouched (and identical object)."""
        msgs = [{"role": "assistant", "content": "plain answer"}]
        out = ensure_round_reasoning_content_field(msgs)
        assert out is msgs
        assert "reasoning_content" not in out[0]

    def test_existing_reasoning_value_is_never_overwritten(self):
        msgs = [U, _tool_call_assistant("c1", reasoning="kept")]
        out = _replay(msgs)
        assert out[1]["reasoning_content"] == "kept"

    def test_content_and_tool_calls_untouched(self):
        msgs = [U, _marker(), _tool_call_assistant("c1"), _tool_result("c1")]
        out = _replay(msgs)
        for before, after in zip(msgs, out):
            assert before.get("content") == after.get("content")
            assert before.get("tool_calls") == after.get("tool_calls")

    def test_input_list_not_mutated(self):
        msgs = [U, _marker()]
        snapshot = [dict(m) for m in msgs]
        _replay(msgs)
        assert msgs == snapshot

    def test_non_assistant_messages_untouched(self):
        """Superseded by TestNonAssistantPreservation (explicit index/role
        assertions); kept as a gate-level companion through ``_replay``."""
        msgs = [U, _marker(), {"role": "system", "content": "sys"},
                _tool_call_assistant("c1"), _tool_result("c1")]
        out = _replay(msgs)
        for index, msg in enumerate(out):
            if msg["role"] != "assistant":
                assert "reasoning_content" not in msg, index


# ---------------------------------------------------------------------------
# Gate discipline: the repair is scoped exactly like the replay it accompanies
# ---------------------------------------------------------------------------


class TestGateDiscipline:
    @pytest.mark.parametrize("caps", [_caps(supports_replay=False)])
    @pytest.mark.xfail(
        reason=(
            "RECORDED LINEAGE DIVERGENCE (CI-2 port, 2026-09-11): upstream v1.8.4's deposit "
            "point gates on the OPERATOR flag only, exactly like its vLLM path "
            "(attach_vllm_chat_reasoning_field also reads cfg.replay_reasoning_to_model "
            "directly). The dev lineage 1237dce1 additionally AND-gates with "
            "caps.supports_reasoning_replay. Adding that gate here would make the non-vLLM "
            "path behave differently from the vLLM path on the same lineage, so it is "
            "deliberately NOT ported; tracked as a follow-up. No production lane is affected: "
            "all 7 replay=true lanes declare supports_reasoning_replay=true."
        ),
        strict=True,
    )
    def test_capability_gate_off_means_no_stamp(self, caps):
        out = _replay([U, _marker()], caps=caps)
        assert "reasoning_content" not in out[1]

    def test_operator_flag_off_means_no_stamp(self):
        out = _replay([U, _marker()], replay=False)
        assert "reasoning_content" not in out[1]

    def test_vllm_uses_its_own_field_path(self):
        out = _replay([U, _marker()], server_type="vllm")
        assert "reasoning_content" not in out[1]

    def test_missing_cfg_means_no_stamp(self):
        out = maybe_attach_openai_reasoning_content(
            [U, _marker()],
            OpenAIChatCompletionsProvider(),
            registry=None,
            alias="deepseek-flash-thinking",
            cfg=None,
        )
        assert "reasoning_content" not in out[1]


# ---------------------------------------------------------------------------
# Idempotence: the wire path may run the projector more than once
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_second_application_is_a_no_op(self):
        msgs = [U, _marker(), _tool_call_assistant("c1"), _tool_result("c1")]
        once = _replay(msgs)
        twice = _replay(once)
        assert once == twice

    def test_direct_helper_idempotent(self):
        msgs = [U, _marker()]
        once = ensure_round_reasoning_content_field(msgs)
        twice = ensure_round_reasoning_content_field(once)
        assert once == twice


# ---------------------------------------------------------------------------
# The two passes compose without interfering
# ---------------------------------------------------------------------------


class TestComposition:
    def test_stored_replay_still_preferred_over_stamp(self):
        msg = {"role": "assistant", "content": "a",
               "_provider_content": [{"type": "reasoning_text", "text": "real"}]}
        out = attach_openai_reasoning_content_field([U, msg])
        assert out[1]["reasoning_content"] == "real"
        stamped = ensure_round_reasoning_content_field(out)
        assert stamped[1]["reasoning_content"] == "real"


# ---------------------------------------------------------------------------
# Finding 2 (Hermes review): the field must reach the ACTUAL wire body, not
# just the projector output.  ``sanitize_messages`` is the last transform
# before serialization; if it ever dropped the key the repair would be inert.
# ---------------------------------------------------------------------------


class TestReachesTheWire:
    def test_field_survives_provider_sanitization(self):
        from turnstone.core.providers._openai_chat import (
            OpenAIChatCompletionsProvider,
        )

        body = [U, _marker(), _tool_call_assistant("c1", reasoning="stored chain"),
                _tool_result("c1")]
        prepared = OpenAIChatCompletionsProvider()._prepare_messages(_replay(body))
        assert prepared[1]["reasoning_content"] == ""
        assert prepared[2]["reasoning_content"] == "stored chain"
        # internal sibling keys are gone; tool pairing intact
        assert all(not any(k.startswith("_") for k in m) for m in prepared)
        assert prepared[2]["tool_calls"] == [_tc("c1")]
        assert prepared[3]["tool_call_id"] == "c1"

    def test_json_serialization_keeps_the_key(self):
        import json as _json

        prepared = _replay([U, _marker()])
        payload = _json.loads(_json.dumps(prepared))
        assert payload[1]["reasoning_content"] == ""


# ---------------------------------------------------------------------------
# Finding 4/5 (Hermes review): strict multi-user boundary + value policy
# ---------------------------------------------------------------------------


class TestBoundaryStrictness:
    def test_only_assistants_after_the_final_user_are_stamped(self):
        """Boundary is the FINAL user message, not the nearest preceding one."""
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},          # before final user
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},          # ALSO before final user
            {"role": "user", "content": "u3"},               # final user
            {"role": "assistant", "content": "a3"},          # current round
        ]
        out = ensure_round_reasoning_content_field(msgs)
        assert "reasoning_content" not in out[1]
        assert "reasoning_content" not in out[3]
        assert out[5]["reasoning_content"] == ""
        # every user row itself is untouched
        assert all("reasoning_content" not in out[i] for i in (0, 2, 4))

    def test_consecutive_summary_markers(self):
        msgs = [U, _marker("s1"), _marker("s2"),
                _tool_call_assistant("c1"), _tool_result("c1")]
        out = ensure_round_reasoning_content_field(msgs)
        assert out[1]["reasoning_content"] == ""
        assert out[2]["reasoning_content"] == ""
        assert out[3]["reasoning_content"] == ""

    def test_reasoning_less_assistant_before_later_user_untouched(self):
        msgs = [U, {"role": "assistant", "content": "old"},
                {"role": "user", "content": "new"}]
        out = ensure_round_reasoning_content_field(msgs)
        assert "reasoning_content" not in out[1]


class TestExistingValuePolicy:
    def test_none_value_is_repaired(self):
        msgs = [U, {"role": "assistant", "content": "a", "reasoning_content": None}]
        out = ensure_round_reasoning_content_field(msgs)
        assert out[1]["reasoning_content"] == ""

    def test_empty_string_is_left_as_is(self):
        msgs = [U, {"role": "assistant", "content": "a", "reasoning_content": ""}]
        out = ensure_round_reasoning_content_field(msgs)
        assert out[1]["reasoning_content"] == ""

    def test_real_value_preserved(self):
        msgs = [U, {"role": "assistant", "content": "a", "reasoning_content": "real"}]
        out = ensure_round_reasoning_content_field(msgs)
        assert out[1]["reasoning_content"] == "real"


# ---------------------------------------------------------------------------
# Finding 6 (Hermes review): explicit non-assistant / trailing-tool coverage
# ---------------------------------------------------------------------------


class TestNonAssistantPreservation:
    def test_roles_preserved_and_only_assistant_stamped(self):
        msgs = [U, _marker(), {"role": "system", "content": "sys"},
                _tool_call_assistant("c1"), _tool_result("c1")]
        out = ensure_round_reasoning_content_field(msgs)
        assert [m["role"] for m in out] == ["user", "assistant", "system",
                                            "assistant", "tool"]
        assert "reasoning_content" not in out[2]     # the system row
        assert "reasoning_content" not in out[4]     # the tool row
        assert out[1]["reasoning_content"] == ""
        assert out[3]["reasoning_content"] == ""

    def test_empty_and_system_only_inputs_are_no_ops(self):
        for msgs in ([], [{"role": "system", "content": "s"}],
                     [{"role": "assistant", "content": "a"}],
                     [{"role": "tool", "tool_call_id": "c", "content": "r"}]):
            assert ensure_round_reasoning_content_field(msgs) == msgs
