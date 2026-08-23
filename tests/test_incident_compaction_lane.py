"""Hermetic tests for the dedicated non-thinking compaction lane.

Incident 2026-08-23 (compaction-lane + DeepSeek replay contract).  These
tests pin the compaction LANE SELECTION policy only:

1. compaction selects ``deepseek/flash-nt`` (canonical, non-thinking, 1M);
2. the selected compaction lane is non-thinking;
3. compaction never selects ``deepseek/flash-thinking``;
4. compaction never resolves through generic ``switchyard-smart``;
5. primary-lane thinking does not influence the compaction lane;
6. unavailable DeepSeek-NT does NOT fall back to a thinking primary;
7. the ``openai/luna`` fallback is used only when the request size truthfully
   fits its usable context envelope;
8. an oversized request with unavailable DeepSeek-NT fails closed;
9. compaction's ``[system, user]`` request carries no assistant reasoning
   material (the replay attach is a no-op on it);
10. existing compaction output semantics regress cleanly (a registry-backed
    session still produces a summary through ``_compact_messages``).

Family-aware rule (operator extension): OpenAI/Luna-family working lanes
compact on ``openai/luna`` FIRST at Luna's own ctx; every other lane keeps
the canonical ``deepseek/flash-nt`` primary with ``openai/luna`` fallback.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.core.history_decoration import attach_openai_reasoning_content_field
from turnstone.core.model_turn import ModelLane
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.trajectory import turns_from_dicts


class _FakeProvider:
    provider_name = "openai-compatible"

    def __init__(self, caps: ModelCapabilities) -> None:
        self._caps = caps

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return self._caps


def _cfg(
    *,
    alias: str,
    model: str,
    ctx: int,
    thinking_mode: str = "none",
    caps_extra: dict | None = None,
) -> SimpleNamespace:
    capabilities = {
        "context_window": ctx,
        "max_output_tokens": 32768,
        "thinking_mode": thinking_mode,
        "server_compat": {"server_type": "switchyard"},
    }
    capabilities.update(caps_extra or {})
    return SimpleNamespace(
        model_id=model,
        context_window=ctx,
        max_tokens=32768,
        surface_persisted_reasoning=True,
        replay_reasoning_to_model=False,
        capabilities=capabilities,
        server_compat={"server_type": "switchyard"},
    )


def _provider(ctx: int, thinking_mode: str = "none") -> _FakeProvider:
    return _FakeProvider(
        ModelCapabilities(
            context_window=ctx,
            max_output_tokens=32768,
            thinking_mode=thinking_mode,
            thinking_param="enable_thinking",
            server_parses_reasoning=False,
        )
    )


class _FakeRegistry:
    """Minimal registry exposing the canonical aliases for lane resolution."""

    def __init__(self, aliases: dict[str, tuple[_FakeProvider, SimpleNamespace]]) -> None:
        self._aliases = aliases
        self.default = "switchyard-smart"
        self.count = len(aliases)
        self.fallback = None
        self.agent_model = None

    def get_config(self, alias: str):
        return self._aliases.get(alias, (None, None))[1]

    def resolve_binding(self, alias: str):
        if alias not in self._aliases:
            raise KeyError(alias)
        provider, cfg = self._aliases[alias]
        return (MagicMock(), cfg.model_id, cfg, provider, None, 1)

    def list_aliases(self) -> list[str]:
        return list(self._aliases)

    def get_admission(self, alias: str):
        return None


def _registry(*, include_nt=True, include_luna=True, include_thinking=True, include_smart=True):
    aliases: dict[str, tuple[_FakeProvider, SimpleNamespace]] = {}
    if include_nt:
        aliases["deepseek-flash-nt"] = (
            _provider(1_048_576),
            _cfg(alias="deepseek-flash-nt", model="deepseek/flash-nt", ctx=1_048_576),
        )
    if include_luna:
        aliases["openai-luna"] = (
            _provider(266_000),
            _cfg(alias="openai-luna", model="openai/luna", ctx=266_000),
        )
    if include_thinking:
        aliases["deepseek-flash-thinking"] = (
            _provider(1_048_576, thinking_mode="manual"),
            _cfg(
                alias="deepseek-flash-thinking",
                model="deepseek/flash-thinking",
                ctx=1_048_576,
                thinking_mode="manual",
            ),
        )
    if include_smart:
        aliases["switchyard-smart"] = (
            _provider(1_048_576),
            _cfg(alias="switchyard-smart", model="localclaw/smart", ctx=1_048_576),
        )
    return _FakeRegistry(aliases)


def _session_with_registry(registry, *, primary_alias="switchyard-smart"):
    """Build a registry-backed session whose PRIMARY lane is *primary_alias*.

    Uses ``make_session``'s registry path so the session binding comes from
    the same registry the compaction resolver consults (matching production
    construction).  Falls back to a bare session with ``_registry`` attached
    when the alias is not present (the caller then controls ``_model_binding``
    directly if it needs a particular family).
    """
    if primary_alias in registry._aliases:
        return make_session(registry=registry, model_alias=primary_alias)
    s = make_session()
    s._registry = registry
    return s


def _stub_summary(text: str = "DENSE"):
    return SimpleNamespace(content=text, finish_reason="stop", producer="test-summary-provider")


# ---------------------------------------------------------------------------
# 1-2. Primary selection + non-thinking
# ---------------------------------------------------------------------------


class TestPrimarySelection:
    def test_selects_deepseek_flash_nt(self):
        reg = _registry()
        s = _session_with_registry(reg)
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "deepseek-flash-nt"

    def test_selected_lane_is_nonthinking(self):
        reg = _registry()
        s = _session_with_registry(reg)
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        caps = lane.capabilities
        assert caps.thinking_mode in ("none", "")
        assert not getattr(caps, "server_parses_reasoning", True)


# ---------------------------------------------------------------------------
# 3-4. Never thinking / never generic smart
# ---------------------------------------------------------------------------


class TestNegativeSelection:
    def test_never_selects_thinking_alias(self):
        # Only the thinking alias + smart exist; no NT, no luna.
        reg = _registry(include_nt=False, include_luna=False)
        s = _session_with_registry(reg, primary_alias="deepseek-flash-thinking")
        assert s._resolve_compaction_lane(request_chars=1_000) is None
        assert s._COMPACTION_LANE_UNAVAILABLE_REASON == "compaction_nonthinking_lane_unavailable"

    def test_never_resolves_generic_switchyard_smart(self):
        # Only switchyard-smart exists — it must NOT be used as the
        # compaction lane even though it is resolvable.
        reg = _registry(include_nt=False, include_luna=False, include_thinking=False)
        s = _session_with_registry(reg)
        assert s._resolve_compaction_lane(request_chars=1_000) is None

    def test_thinking_primary_does_not_influence_compaction(self):
        # Session primary is DeepSeek THINKING; compaction still prefers NT.
        reg = _registry()
        s = _session_with_registry(reg, primary_alias="deepseek-flash-thinking")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "deepseek-flash-nt"
        assert lane.capabilities.thinking_mode in ("none", "")


# ---------------------------------------------------------------------------
# 5-8. Fallback / fail-closed policy
# ---------------------------------------------------------------------------


class TestFallbackPolicy:
    def test_nt_unavailable_does_not_fall_back_to_thinking_primary(self):
        # NT missing; thinking + smart present; luna present and admitted →
        # luna (non-thinking) is the fallback, never the thinking primary.
        reg = _registry(include_nt=False)
        s = _session_with_registry(reg, primary_alias="deepseek-flash-thinking")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "openai-luna"
        assert lane.capabilities.thinking_mode in ("none", "")

    def test_luna_fallback_used_when_request_fits(self):
        reg = _registry(include_nt=False)
        s = _session_with_registry(reg)
        lane = s._resolve_compaction_lane(request_chars=10_000)
        assert lane is not None
        assert lane.alias == "openai-luna"

    def test_oversized_request_fails_closed(self):
        # NT missing; luna present but the request is far larger than Luna's
        # usable envelope → fail closed (None), not truncate, not thinking.
        reg = _registry(include_nt=False)
        s = _session_with_registry(reg)
        huge = 266_000 * 4 * 10  # ~10x Luna's full window in chars
        assert s._resolve_compaction_lane(request_chars=huge) is None

    def test_nt_unavailable_and_no_fallback_fails_closed(self):
        reg = _registry(include_nt=False, include_luna=False)
        s = _session_with_registry(reg)
        assert s._resolve_compaction_lane(request_chars=1_000) is None


# ---------------------------------------------------------------------------
# 9. Compaction request shape is [system, user] — no assistant reasoning
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_compaction_system_user_request_untouched_by_replay(self):
        msgs = [
            {"role": "system", "content": "You are the summarizer."},
            {"role": "user", "content": "Summarize: lots of history here."},
        ]
        out = attach_openai_reasoning_content_field(msgs)
        assert out == msgs
        assert all(m.get("reasoning_content") is None for m in out)


# ---------------------------------------------------------------------------
# 10. Existing compaction output semantics regress cleanly
# ---------------------------------------------------------------------------


class TestSemanticsRegression:
    def test_compaction_produces_summary_with_dedicated_lane(self, tmp_db):
        reg = _registry()
        s = _session_with_registry(reg)
        s.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "did the thing"},
            ]
        )
        s._msg_tokens = [1, 1]

        # Prove the dedicated lane is what the summary runs on.
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None and lane.alias == "deepseek-flash-nt"

        with patch.object(s, "_utility_completion", return_value=_stub_summary()):
            assert s._compact_messages(auto=True) is True
        assert any("DENSE" in (m.text or "") for m in s.messages)


# ---------------------------------------------------------------------------
# Family doctrine (operator extension): GPT→luna, DeepSeek→flash-nt,
# local Qwen → themselves in NT mode
# ---------------------------------------------------------------------------


def _registry_family(*, with_htpc=True, with_comfy=True):
    aliases = _registry()._aliases
    if with_htpc:
        aliases["htpc-qwen3.5-9b"] = (
            _provider(65_536),
            _cfg(alias="htpc-qwen3.5-9b", model="htpc/qwen3.5-9b", ctx=65_536),
        )
    if with_comfy:
        aliases["comfyninja-qwen3.8-27b"] = (
            _provider(69_888),
            _cfg(alias="comfyninja-qwen3.8-27b", model="comfyninja/qwen3.8-27b", ctx=69_888),
        )
    return _FakeRegistry(aliases)


class TestFamilyDoctrine:
    def test_gpt_family_compacts_with_luna(self):
        reg = _registry_family()
        # A plain-luna (GPT-family) working lane compacts on luna FIRST.
        s = _session_with_registry(reg, primary_alias="openai-luna")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "openai-luna"

    def test_gpt_family_luna_oversized_falls_to_deepseek_nt(self):
        reg = _registry_family()
        s = _session_with_registry(reg, primary_alias="openai-luna")
        # Request beyond Luna's usable envelope → DeepSeek-NT (1M) fallback.
        big = 266_000 * 4 * 2  # ~2x Luna's window in chars
        lane = s._resolve_compaction_lane(request_chars=big)
        assert lane is not None
        assert lane.alias == "deepseek-flash-nt"

    def test_deepseek_family_compacts_with_flash_nt(self):
        reg = _registry_family()
        s = _session_with_registry(reg, primary_alias="deepseek-flash-nt")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "deepseek-flash-nt"

    def test_local_qwen_compacts_with_itself_nt(self):
        reg = _registry_family()
        s = _session_with_registry(reg, primary_alias="htpc-qwen3.5-9b")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "htpc-qwen3.5-9b"
        assert lane.capabilities.thinking_mode in ("none", "")

    def test_local_comfy_qwen_compacts_with_itself_nt(self):
        reg = _registry_family()
        s = _session_with_registry(reg, primary_alias="comfyninja-qwen3.8-27b")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "comfyninja-qwen3.8-27b"

    def test_local_qwen_own_nt_unavailable_falls_to_cloud_nt(self):
        reg = _registry_family(with_htpc=False, with_comfy=True)
        # HTPC session whose own NT alias is missing → canonical DeepSeek-NT.
        s = _session_with_registry(reg, primary_alias="htpc-qwen3.5-9b")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "deepseek-flash-nt"
