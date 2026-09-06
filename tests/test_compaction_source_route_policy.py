"""Hermetic tests for the operator compaction-routing policy (2026-09-02).

Pins :attr:`ChatSession._COMPACTION_SOURCE_ROUTE_MAP` semantics — the
source-route → compaction-lane mapping checked BEFORE the family ladder:

1. smart lanes compact by lane tier (smart → agentic; agentic → itself;
   bounded → itself) — lane identity, never physical-backend identity;
2. fixed local MTP lanes compact on their own exact NT variant;
3. direct/emergency providers stay on the same direct path (deepseek →
   itself; direct OpenAI → direct NT twin; GLM → itself, no NT emulation;
   direct OpenRouter qwen → direct NT twin);
4. a mapped source yields EXACTLY its mapped target (no cross-class
   fallback) and fails closed when that target is unavailable;
5. unmapped sources keep the 2026-08-23 family ladder unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests._session_helpers import make_session
from turnstone.core.model_turn import ModelLane, lane_thinking_suppressed, lane_without_thinking
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.session import ChatSession


class _SeamProvider:
    provider_name = "openai-compatible"


class _FakeProvider:
    provider_name = "openai-compatible"

    def __init__(self, caps: ModelCapabilities) -> None:
        self._caps = caps

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return self._caps

    def __init__(self, caps: ModelCapabilities) -> None:
        self._caps = caps

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return self._caps


def _cfg(*, alias: str, model: str, ctx: int) -> SimpleNamespace:
    return SimpleNamespace(
        model_id=model,
        context_window=ctx,
        max_tokens=32768,
        surface_persisted_reasoning=True,
        replay_reasoning_to_model=False,
        capabilities={"context_window": ctx, "max_output_tokens": 32768},
        server_compat={"server_type": "switchyard"},
    )


def _provider(ctx: int) -> _FakeProvider:
    return _FakeProvider(ModelCapabilities(context_window=ctx, max_output_tokens=32768))


# alias -> (model id, declared window) for every registry row a mapped
# source may need, plus the legacy ladder rows.
_ROWS: dict[str, tuple[str, int]] = {
    # smart lanes
    "switchyard-smart-turnstone": ("switchyard-smart-turnstone", 524_288),
    "switchyard-smart-agentic-turnstone": ("switchyard-smart-agentic-turnstone", 524_288),
    "switchyard-smart-bounded-turnstone": ("switchyard-smart-bounded-turnstone", 262_144),
    "switchyard-smart-hermes": ("switchyard-smart-hermes", 524_288),
    "switchyard-smart-agentic-hermes": ("switchyard-smart-agentic-hermes", 524_288),
    "switchyard-smart-bounded-hermes": ("switchyard-smart-bounded-hermes", 266_000),
    # Option-2 canonical (2026-09-06)
    "switchyard-smart-openclaw-local": ("switchyard-smart-openclaw-local", 524_288),
    "switchyard-smart-agentic-openclaw-local": ("switchyard-smart-agentic-openclaw-local", 524_288),
    "switchyard-smart-bounded-openclaw-local": ("switchyard-smart-bounded-openclaw-local", 266_000),
    "switchyard-smart-openclaw-remote": ("switchyard-smart-openclaw-remote", 524_288),
    "switchyard-smart-agentic-openclaw-remote": ("switchyard-smart-agentic-openclaw-remote", 524_288),
    "switchyard-smart-bounded-openclaw-remote": ("switchyard-smart-bounded-openclaw-remote", 266_000),
    # legacy rows retained as source keys for historical sessions
    "switchyard-smart-openclaw": ("switchyard-smart-openclaw", 524_288),
    "switchyard-smart-agentic-openclaw": ("switchyard-smart-agentic-openclaw", 524_288),
    "switchyard-smart-bounded-openclaw": ("switchyard-smart-bounded-openclaw", 266_000),
    "switchyard-smart-remoteopenclaw": ("switchyard-smart-remoteopenclaw", 524_288),
    "switchyard-smart-agentic-remoteopenclaw": ("switchyard-smart-agentic-remoteopenclaw", 524_288),
    "switchyard-smart-bounded-remoteopenclaw": ("switchyard-smart-bounded-remoteopenclaw", 266_000),
    "switchyard-smart-bounded-dsh": ("switchyard-smart-bounded-dsh", 266_000),
    # fixed local
    "switchyard-comfyninja-qwen3.8-27b-q3-mtp": ("switchyard/comfyninja/qwen3.8-27b-q3-mtp", 196_608),
    "switchyard-comfyninja-qwen3.8-27b-q3-mtp-nt": ("switchyard/comfyninja/qwen3.8-27b-q3-mtp-nt", 196_608),
    "switchyard-comfyninja-qwen3.8-27b-q4-mtp": ("switchyard/comfyninja/qwen3.8-27b-q4-mtp", 147_456),
    "switchyard-comfyninja-qwen3.8-27b-q4-mtp-nt": ("switchyard/comfyninja/qwen3.8-27b-q4-mtp-nt", 147_456),
    "switchyard-htpc-qwen3.5-9b-mtp": ("switchyard/htpc/qwen3.5-9b-mtp", 81_920),
    "switchyard-htpc-qwen3.5-9b-mtp-nt": ("switchyard/htpc/qwen3.5-9b-mtp-nt", 81_920),
    # direct/emergency
    "deepseek-deepseek-v4-flash": ("deepseek-v4-flash", 1_048_576),
    "deepseek-v4-flash-nt": ("deepseek-v4-flash", 1_048_576),
    "deepseek-deepseek-v4-pro": ("deepseek-v4-pro", 1_048_576),
    "deepseek-v4-pro-nt": ("deepseek-v4-pro", 1_048_576),
    "gpt-5.6-luna": ("gpt-5.6-luna", 262_144),
    "gpt-5.6-luna-nt": ("gpt-5.6-luna", 262_144),
    "gpt-5.4-mini": ("gpt-5.4-mini", 262_144),
    "gpt-5.4-mini-nt": ("gpt-5.4-mini", 262_144),
    "gpt-5.6-sol": ("gpt-5.6-sol", 262_144),
    "gpt-5.6-sol-nt": ("gpt-5.6-sol", 262_144),
    "gpt-5.6-terra": ("gpt-5.6-terra", 262_144),
    "gpt-5.6-terra-nt": ("gpt-5.6-terra", 262_144),
    "glm-5.3-flash": ("z-ai/glm-5.3-flash", 1_310_720),
    "qwen3.8-flash": ("qwen/qwen3.8-flash", 1_000_000),
    "qwen3.8-flash-nt": ("qwen/qwen3.8-flash", 1_000_000),
    # legacy ladder fallbacks
    "deepseek-flash-nt": ("deepseek/flash-nt", 1_048_576),
    "openai-luna": ("openai/luna", 266_000),
}


def _registry(aliases: dict[str, tuple[str, int]] | None = None):
    rows = aliases if aliases is not None else _ROWS
    built = {
        alias: (_provider(ctx), _cfg(alias=alias, model=model, ctx=ctx))
        for alias, (model, ctx) in rows.items()
    }
    reg = MagicMock()
    reg.default = "switchyard-smart-turnstone"
    reg.resolve_binding = lambda alias: (
        (MagicMock(), built[alias][1].model_id, built[alias][1], built[alias][0], None, 1)
        if alias in built
        else (_ for _ in ()).throw(KeyError(alias))
    )
    return reg


def _lane_for(reg, alias: str):
    """Resolve *alias* through the real binding path and return its lane."""
    from turnstone.core.model_turn import resolve_model_binding

    return resolve_model_binding(reg, alias).lane


def _target_for(reg, source_alias: str) -> str | None:
    s = make_session(registry=reg, model_alias=source_alias)
    return s._compaction_source_route_target()


def _compaction_lane(reg, source_alias: str, *, request_chars: int = 1_000):
    s = make_session(registry=reg, model_alias=source_alias)
    return s._resolve_compaction_lane(request_chars=request_chars)


class TestSmartLaneTierMapping:
    def test_smart_turnstone_compacts_on_agentic(self):
        reg = _registry()
        s = make_session(registry=reg, model_alias="switchyard-smart-turnstone")
        lane = s._resolve_compaction_lane(request_chars=1_000)
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-turnstone"

    def test_agentic_compacts_on_itself(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-agentic-turnstone")
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-turnstone"

    def test_bounded_compacts_on_itself(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-bounded-turnstone")
        assert lane is not None
        assert lane.alias == "switchyard-smart-bounded-turnstone"

    def test_smart_hermes_compacts_on_agentic(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-hermes")
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-hermes"

    def test_smart_openclaw_compacts_on_agentic(self):
        # Legacy universal openclaw -> canonical agentic-openclaw-local (Option-2)
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-openclaw")
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-openclaw-local"

    def test_smart_openclaw_local_compacts_on_agentic_local(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-openclaw-local")
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-openclaw-local"

    def test_smart_remoteopenclaw_compacts_on_agentic(self):
        # Legacy remoteopenclaw family -> canonical agentic-openclaw-remote (Option-2)
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-remoteopenclaw")
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-openclaw-remote"

    def test_smart_openclaw_remote_compacts_on_agentic_remote(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-openclaw-remote")
        assert lane is not None
        assert lane.alias == "switchyard-smart-agentic-openclaw-remote"

    def test_bounded_openclaw_local_compacts_on_itself(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-bounded-openclaw-local")
        assert lane is not None
        assert lane.alias == "switchyard-smart-bounded-openclaw-local"

    def test_bounded_openclaw_remote_compacts_on_itself(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-bounded-openclaw-remote")
        assert lane is not None
        assert lane.alias == "switchyard-smart-bounded-openclaw-remote"

    def test_bounded_dsh_compacts_on_itself(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-smart-bounded-dsh")
        assert lane is not None
        assert lane.alias == "switchyard-smart-bounded-dsh"


class TestFixedLocalMtp:
    def test_comfy_q3_mtp_compacts_on_own_nt_variant(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-comfyninja-qwen3.8-27b-q3-mtp")
        assert lane is not None
        assert lane.alias == "switchyard-comfyninja-qwen3.8-27b-q3-mtp-nt"
        assert lane.model == "switchyard/comfyninja/qwen3.8-27b-q3-mtp-nt"

    def test_comfy_nt_compacts_on_itself(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-comfyninja-qwen3.8-27b-q3-mtp-nt")
        assert lane is not None
        assert lane.alias == "switchyard-comfyninja-qwen3.8-27b-q3-mtp-nt"

    def test_htpc_mtp_compacts_on_own_nt_variant(self):
        reg = _registry()
        lane = _compaction_lane(reg, "switchyard-htpc-qwen3.5-9b-mtp")
        assert lane is not None
        assert lane.alias == "switchyard-htpc-qwen3.5-9b-mtp-nt"

    def test_htpc_never_jumps_into_smart_lane(self):
        reg = _registry()
        target = _target_for(reg, "switchyard-htpc-qwen3.5-9b-mtp")
        assert target == "switchyard-htpc-qwen3.5-9b-mtp-nt"
        assert "smart" not in (target or "")


class TestDirectEmergency:
    def test_direct_deepseek_stays_on_direct_path(self):
        reg = _registry()
        target = _target_for(reg, "deepseek-deepseek-v4-flash")
        assert target == "deepseek-v4-flash-nt"
        lane = _compaction_lane(reg, "deepseek-deepseek-v4-flash")
        assert lane is not None
        assert lane.alias == "deepseek-v4-flash-nt"
        assert lane.model == "deepseek-v4-flash"  # same direct provider host row family

    def test_direct_openai_uses_direct_nt_twin(self):
        reg = _registry()
        lane = _compaction_lane(reg, "gpt-5.6-luna")
        assert lane is not None
        assert lane.alias == "gpt-5.6-luna-nt"
        assert lane.model == "gpt-5.6-luna"

    def test_glm_compacts_on_itself_no_nt_emulation(self):
        reg = _registry()
        lane = _compaction_lane(reg, "glm-5.3-flash")
        assert lane is not None
        assert lane.alias == "glm-5.3-flash"

    def test_direct_openrouter_qwen_uses_direct_nt_twin(self):
        reg = _registry()
        lane = _compaction_lane(reg, "qwen3.8-flash")
        assert lane is not None
        assert lane.alias == "qwen3.8-flash-nt"
        assert lane.model == "qwen/qwen3.8-flash"


class TestFailClosed:
    def test_mapped_target_unavailable_fails_closed(self):
        reg = _registry({"switchyard-smart-turnstone": ("switchyard-smart-turnstone", 524_288),
                         "deepseek-flash-nt": ("deepseek/flash-nt", 1_048_576),
                         "openai-luna": ("openai/luna", 266_000)})
        s = make_session(registry=reg, model_alias="switchyard-smart-turnstone")
        assert s._resolve_compaction_lane(request_chars=1_000) is None

    def test_oversized_request_fails_closed_on_bounded_self(self):
        reg = _registry()
        s = make_session(registry=reg, model_alias="switchyard-smart-bounded-turnstone")
        assert s._resolve_compaction_lane(request_chars=10_000_000) is None


class TestUnmappedSourcesKeepLadder:
    def test_unmapped_local_ladder_unchanged(self):
        reg = _registry({"switchyard-smart": ("localclaw/smart", 1_048_576),
                         "deepseek-flash-nt": ("deepseek/flash-nt", 1_048_576),
                         "openai-luna": ("openai/luna", 266_000)})
        s = make_session(registry=reg, model_alias="switchyard-smart")
        pref = s._compaction_lane_preference()
        assert pref == ["deepseek-flash-nt", "openai-luna"]

    def test_unmapped_unknown_ladder_unchanged(self):
        reg = _registry({"some-other-lane": ("some/other", 100_000),
                         "deepseek-flash-nt": ("deepseek/flash-nt", 1_048_576),
                         "openai-luna": ("openai/luna", 266_000)})
        s = make_session(registry=reg, model_alias="some-other-lane")
        assert s._compaction_lane_preference() == ["deepseek-flash-nt", "openai-luna"]


class TestLookupPrecedence:
    def test_alias_key_wins_over_model_key(self, monkeypatch):
        reg = _registry({
            "dual-key-lane": ("z-ai/glm-5.3-flash", 1_310_720),
            "alias-target": ("alias/target", 100_000),
            "model-target": ("model/target", 100_000),
        })
        s = make_session(registry=reg, model_alias="dual-key-lane")
        # Alias key maps to alias-target; the model id would map to
        # model-target.  Alias-first must win.
        monkeypatch.setitem(ChatSession._COMPACTION_SOURCE_ROUTE_MAP,
                            "dual-key-lane", "alias-target")
        monkeypatch.setitem(ChatSession._COMPACTION_SOURCE_ROUTE_MAP,
                            "z-ai/glm-5.3-flash", "model-target")
        assert s._compaction_source_route_target() == "alias-target"

    def test_lookup_is_case_insensitive(self, monkeypatch):
        reg = _registry({"MiXeD-Case-Lane": ("some/model", 100_000)})
        s = make_session(registry=reg, model_alias="MiXeD-Case-Lane")
        monkeypatch.setitem(ChatSession._COMPACTION_SOURCE_ROUTE_MAP,
                            "mixed-case-lane", "@self")
        assert s._compaction_source_route_target() == "mixed-case-lane"


class TestSeamNtEnforcement:
    """The seam facts the map's comments rely on (review finding 7)."""

    def _lane(self, caps_kwargs: dict) -> ModelLane:
        return ModelLane(
            provider=_SeamProvider(),
            client=MagicMock(),
            model="m",
            alias="a",
            capabilities=ModelCapabilities(**caps_kwargs),
            extra_params={},
        )

    def test_declared_toggle_lane_is_suppressed_and_pinned(self):
        # Direct DeepSeek shape: manual toggle declared, no server parsing.
        lane = self._lane({"thinking_mode": "manual", "thinking_param": "enable_thinking",
                           "server_parses_reasoning": False})
        assert lane_thinking_suppressed(lane) is True
        off = lane_without_thinking(lane)
        assert off.extra_params["chat_template_kwargs"]["enable_thinking"] is False
        assert off.reasoning_effort is None

    def test_server_parses_reasoning_lane_is_exempt(self):
        # Direct OpenAI NT twin shape: server segregates, row effort stands.
        lane = self._lane({"server_parses_reasoning": True})
        lane = replace(lane, reasoning_effort="none")
        assert lane_thinking_suppressed(lane) is False
        off = lane_without_thinking(lane)
        assert off is lane  # untouched
        assert off.reasoning_effort == "none"

    def test_toggleless_lane_suppresses_without_guessing_keys(self):
        # GLM shape: no declared toggle; seam clears effort but pins nothing.
        lane = self._lane({"server_parses_reasoning": False})
        assert lane_thinking_suppressed(lane) is True
        off = lane_without_thinking(lane)
        assert "chat_template_kwargs" not in (off.extra_params or {})
        assert off.reasoning_effort is None

    def test_adaptive_lane_with_server_parsing_is_exempt(self):
        # Direct OpenRouter qwen3.8-flash base row shape: the seam does NOT
        # pin (which is why the dedicated NT twin row omits the flag).
        lane = self._lane({"thinking_mode": "adaptive", "thinking_param": "enable_thinking",
                           "server_parses_reasoning": True})
        assert lane_thinking_suppressed(lane) is False

    def test_nt_twin_row_shape_pins_the_toggle(self):
        # qwen3.8-flash-nt row shape: adaptive toggle, NO server parsing
        # declaration → seam pins enable_thinking=false.
        lane = self._lane({"thinking_mode": "adaptive", "thinking_param": "enable_thinking",
                           "server_parses_reasoning": False})
        assert lane_thinking_suppressed(lane) is True
        off = lane_without_thinking(lane)
        assert off.extra_params["chat_template_kwargs"]["enable_thinking"] is False
