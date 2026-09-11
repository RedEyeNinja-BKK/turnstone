"""Per-lane request headers — ``server_compat["extra_headers"]``.

Covers the three layers of the feature:
  1. ``merge_server_compat_headers`` — extraction + validation (the security edge:
     a stored value must not be able to forge a header or split the request).
  2. ``provider_extra_headers`` — provider scoping + one-snapshot resolution.
  3. ``resolve_lane`` / ``model_turn`` — the values actually reach the SDK call.

Motivation (2026-09-11): an upstream WAF rejected the OpenAI Python SDK's own
``User-Agent`` (``OpenAI/Python <version>``) with a hard 403 *before*
authentication, making a direct lane unservable.  Overriding the header is the
only fix that keeps the SDK client (and its timeout/retry/streaming behaviour).
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from structlog.testing import capture_logs

from tests._session_helpers import as_stream
from turnstone.core.model_turn import (
    EXTRA_HEADERS_PROVIDERS,
    ModelLane,
    model_turn,
    provider_extra_headers,
    resolve_lane,
)
from turnstone.core.providers._protocol import CompletionResult, ModelCapabilities, StreamChunk
from turnstone.core.server_compat import merge_server_compat, merge_server_compat_headers
from turnstone.core.trajectory import Turn


class _FakeProvider:
    """Records every ``create_streaming`` call (same shape as test_model_turn)."""

    provider_name = "openai-compatible"

    def __init__(self, results: list[CompletionResult] | None = None) -> None:
        self.results = list(results or [CompletionResult(content="ok")])
        self.calls: list[dict[str, Any]] = []

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    def create_streaming(self, **kwargs: Any) -> list[StreamChunk]:
        self.calls.append(kwargs)
        return as_stream(self.results.pop(0))


def _fake_registry(
    *, server_compat: dict[str, Any] | None = None, capabilities: dict[str, Any] | None = None
) -> MagicMock:
    cfg = SimpleNamespace(
        capabilities=capabilities or {},
        server_compat=server_compat or {},
        replay_reasoning_to_model=False,
        temperature=None,
    )
    reg = MagicMock()
    reg.get_config.return_value = cfg
    return reg


# ---------------------------------------------------------------------------
# 1. merge_server_compat_headers — extraction and validation
# ---------------------------------------------------------------------------


class TestMergeServerCompatHeaders:
    def test_absent_and_malformed_inputs_yield_empty(self) -> None:
        assert merge_server_compat_headers(None) == {}
        assert merge_server_compat_headers({}) == {}
        assert merge_server_compat_headers({"server_type": "openai-compatible"}) == {}
        # ``extra_headers`` present but not a mapping — ignored, loudly.
        with capture_logs() as caps:
            assert merge_server_compat_headers({"extra_headers": "User-Agent: x"}) == {}
        assert any(
            c["event"] == "server_compat.extra_headers.ignored"
            and c.get("reason") == "not_a_mapping"
            for c in caps
        )

    def test_valid_headers_pass_through(self) -> None:
        got = merge_server_compat_headers(
            {"extra_headers": {"User-Agent": "turnstone/1.0", "X-Api-Version": "2"}}
        )
        assert got == {"User-Agent": "turnstone/1.0", "X-Api-Version": "2"}

    def test_value_is_not_required_to_be_a_known_header(self) -> None:
        # Only the NAME is constrained; the value is opaque by design.
        got = merge_server_compat_headers({"extra_headers": {"User-Agent": "anything at all"}})
        assert got == {"User-Agent": "anything at all"}

    def test_invalid_name_is_dropped(self) -> None:
        for bad in ("Bad Name", "X:Y", "", "Host\r\nX-Injected"):
            with capture_logs() as caps:
                got = merge_server_compat_headers({"extra_headers": {bad: "v"}})
            assert got == {}, bad
            assert any(
                c["event"] == "server_compat.extra_headers.dropped"
                and c.get("reason") == "invalid_name"
                for c in caps
            ), bad

    def test_non_string_value_is_dropped(self) -> None:
        with capture_logs() as caps:
            got = merge_server_compat_headers({"extra_headers": {"X-N": 5}})
        assert got == {}
        assert any(
            c["event"] == "server_compat.extra_headers.dropped"
            and c.get("reason") == "non_string_value"
            for c in caps
        )

    def test_control_characters_in_value_are_dropped(self) -> None:
        """A stored value must not be able to forge a second header.

        Rejects the full C0 range plus DEL, not merely the three characters that
        happen to split a request in the obvious way.
        """
        bad_values = [
            "ok\r\nX-Injected: 1",
            "ok\nX-Injected: 1",
            "ok\x00",
            "ok\x01",
            "ok\x1f",
            "ok\x7f",
            "ok\x0b\x0c",
        ]
        for bad in bad_values:
            with capture_logs() as caps:
                got = merge_server_compat_headers({"extra_headers": {"X-N": bad}})
            assert got == {}, repr(bad)
            assert any(
                c["event"] == "server_compat.extra_headers.dropped"
                and c.get("reason") == "control_character"
                for c in caps
            ), repr(bad)

    def test_leading_and_trailing_whitespace_is_preserved_verbatim(self) -> None:
        # Whitespace is legal in a field value; the layer must not silently trim.
        got = merge_server_compat_headers({"extra_headers": {"X-N": " padded "}})
        assert got == {"X-N": " padded "}

    def test_mixed_entries_keep_only_the_valid_ones(self) -> None:
        got = merge_server_compat_headers(
            {"extra_headers": {"User-Agent": "ok", "Bad Name": "x", "X-N": 3}}
        )
        assert got == {"User-Agent": "ok"}

    def test_values_are_never_logged(self) -> None:
        """Header values are secrets: a drop may name the header, never its value."""
        secret = "super-secret-value"
        with capture_logs() as caps:
            merge_server_compat_headers({"extra_headers": {"X-N": 5, "Ok": secret}})
        dump = repr(caps)
        assert secret not in dump

    def test_headers_and_body_are_independent(self) -> None:
        """Adding headers must not disturb the extra_body contract."""
        compat = {
            "server_type": "llama.cpp",
            "extra_body": {"reasoning_format": "none"},
            "extra_headers": {"User-Agent": "turnstone/1.0"},
        }
        assert merge_server_compat(None, compat) == {"reasoning_format": "none"}
        assert merge_server_compat_headers(compat) == {"User-Agent": "turnstone/1.0"}


# ---------------------------------------------------------------------------
# 2. provider_extra_headers — provider scoping
# ---------------------------------------------------------------------------


def _provider(name: str) -> Any:
    p = _FakeProvider()
    p.provider_name = name
    return p


class TestProviderExtraHeaders:
    def test_resolves_from_the_registry_snapshot(self) -> None:
        registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "ua/1"}})
        assert provider_extra_headers(_provider("openai-compatible"), registry, "ali") == {
            "User-Agent": "ua/1"
        }

    def test_uses_a_prefetched_cfg_without_touching_the_registry(self) -> None:
        registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "from-registry"}})
        cfg = SimpleNamespace(server_compat={"extra_headers": {"User-Agent": "from-cfg"}})
        got = provider_extra_headers(_provider("openai-compatible"), registry, "ali", cfg=cfg)
        assert got == {"User-Agent": "from-cfg"}
        registry.get_config.assert_not_called()

    def test_none_when_unconfigured(self) -> None:
        registry = _fake_registry(server_compat={"server_type": "openai-compatible"})
        assert provider_extra_headers(_provider("openai-compatible"), registry, "ali") is None

    def test_none_for_providers_outside_the_scoped_set(self) -> None:
        registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "ua/1"}})
        for name in ("not-a-provider", "", "openai-compat"):
            assert provider_extra_headers(_provider(name), registry, "ali") is None, name

    def test_inheriting_and_native_providers_all_resolve_headers(self) -> None:
        """Regression for the review's blocking scoping finding.

        xAI and google inherit ``create_streaming`` from OpenAI-shaped bases and
        anthropic takes ``extra_headers`` natively, so a configured header must
        reach every one of them -- excluding them would drop it silently.
        """
        registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "ua/1"}})
        for name in ("xai", "google", "anthropic", "anthropic-compatible", "openai"):
            assert provider_extra_headers(_provider(name), registry, "ali") == {
                "User-Agent": "ua/1"
            }, name

    def test_scoped_set_covers_exactly_the_provider_capability_surface(self) -> None:
        """The gate must name every provider name the factory dispatches.

        Review finding (2026-09-11): the first version of this test only compared
        the tuple to itself, so a provider that DOES accept ``extra_headers``
        could be omitted from the gate and lose its configured headers silently.
        This derives the name set from the factory's own dispatch source and from
        each provider's real signature, so a newly added provider fails the guard
        instead of silently losing the feature.
        """
        import inspect
        import re
        from pathlib import Path

        import turnstone.core.providers as providers_pkg
        from turnstone.core.providers import create_provider

        src = Path(inspect.getsourcefile(providers_pkg)).read_text()
        dispatched = set(re.findall(r'provider_name == "([^"]+)"', src))

        # The source scan catches a NEWLY ADDED provider under the current
        # if-chain dispatch.  It is deliberately advisory: a harmless refactor
        # (a mapping, match/case, single quotes, constants) can make the scan
        # yield nothing or an incomplete set, and that must not fail the suite
        # for a change that cannot break runtime behaviour.  So the couple-to-
        # spelling assertion runs only when the scan actually produced a set;
        # the signature assertions below always run.
        if dispatched:
            assert dispatched == set(EXTRA_HEADERS_PROVIDERS), (
                f"gate {sorted(EXTRA_HEADERS_PROVIDERS)} != factory {sorted(dispatched)}; "
                "if a provider was added, add it to EXTRA_HEADERS_PROVIDERS"
            )

        # Always true regardless of dispatch spelling: every gated name is a
        # real provider whose entry point accepts the kwarg.
        for name in sorted(set(EXTRA_HEADERS_PROVIDERS) | dispatched):
            provider = create_provider(name)
            assert provider.provider_name == name
            assert "extra_headers" in inspect.signature(provider.create_streaming).parameters, name


# ---------------------------------------------------------------------------
# 3. resolve_lane / model_turn — the values reach the SDK call
# ---------------------------------------------------------------------------


def test_resolve_lane_plumbs_headers_from_the_registry() -> None:
    registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "turnstone/1.0"}})
    lane = resolve_lane(_FakeProvider(), object(), "m", alias="ali", registry=registry)
    assert lane.extra_headers == {"User-Agent": "turnstone/1.0"}


def test_resolve_lane_respects_preresolved_none() -> None:
    """``None`` is a valid resolved value, distinct from the resolve-for-me sentinel."""
    registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "turnstone/1.0"}})
    lane = resolve_lane(
        _FakeProvider(), object(), "m", alias="ali", registry=registry, extra_headers=None
    )
    assert lane.extra_headers is None


def test_resolve_lane_without_registry_has_no_headers() -> None:
    lane = ModelLane(provider=_FakeProvider(), client=object(), model="m")
    assert lane.extra_headers is None


def test_extra_headers_reach_create_streaming() -> None:
    """The end-to-end plumbing claim: the lane's headers are handed to the SDK call."""
    provider = _FakeProvider()
    registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "turnstone/1.0"}})
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)

    model_turn(lane, [Turn.user("hello")])

    assert provider.calls[0]["extra_headers"] == {"User-Agent": "turnstone/1.0"}


def test_no_headers_configured_omits_the_kwarg() -> None:
    """An untouched lane must keep the exact pre-feature call shape.

    This is not cosmetic: ``test_backend_auth_token_binds_sdk_credential_once``
    pins that dynamic credentials ride ``with_options`` and never an override
    header, which it asserts by requiring the ``extra_headers`` kwarg to be
    ABSENT.  Passing ``extra_headers=None`` on every lane would erode that
    guard, so the kwarg is withheld unless an operator configured headers.
    """
    provider = _FakeProvider()
    registry = _fake_registry(server_compat={})
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)

    model_turn(lane, [Turn.user("hello")])

    assert "extra_headers" not in provider.calls[0]


def test_operator_headers_never_carry_the_backend_auth_token() -> None:
    """A lane can have BOTH operator headers and a minted credential.

    The credential must ride ``with_options`` (SDK-level), and the only header
    sent must be the operator's own — the minted token must never appear as an
    override header.
    """
    provider = _FakeProvider()
    registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": "turnstone/1.0"}})
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)
    client = MagicMock()
    client.with_options.return_value = object()
    lane = ModelLane(**{**lane.__dict__, "client": client})

    model_turn(lane, [Turn.user("hello")], backend_auth_token="minted-token")

    headers = provider.calls[0]["extra_headers"]
    assert headers == {"User-Agent": "turnstone/1.0"}
    assert "minted-token" not in repr(headers)
    client.with_options.assert_called_once_with(api_key="minted-token")


def test_headers_do_not_leak_into_extra_params() -> None:
    """Different SDK channels: headers must not appear in the request body."""
    provider = _FakeProvider()
    registry = _fake_registry(
        server_compat={
            "extra_body": {"reasoning_format": "none"},
            "extra_headers": {"User-Agent": "turnstone/1.0"},
        }
    )
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)

    model_turn(lane, [Turn.user("hello")])

    called = provider.calls[0]
    assert called["extra_params"] == {"reasoning_format": "none"}
    assert called["extra_headers"] == {"User-Agent": "turnstone/1.0"}
    assert "User-Agent" not in (called["extra_params"] or {})


def test_dataclasses_replace_preserves_headers() -> None:
    """``lane_thinking_suppressed`` rebuilds lanes with ``replace(lane, ...)``.

    It does not name the new field, so ``replace`` must carry it across -- a
    thinking-suppressed lane must not silently lose its configured headers.
    """
    lane = ModelLane(
        provider=_FakeProvider(),
        client=object(),
        model="m",
        extra_headers={"User-Agent": "turnstone/1.0"},
    )
    rebuilt = replace(lane, extra_params={"k": "v"})
    assert rebuilt.extra_headers == {"User-Agent": "turnstone/1.0"}


def test_header_value_never_reaches_the_logs_on_dispatch() -> None:
    """Values are secrets: a configured header must not appear in any log event."""
    secret = "super-secret-header-value-8f3a"
    provider = _FakeProvider()
    registry = _fake_registry(server_compat={"extra_headers": {"User-Agent": secret}})
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)

    with capture_logs() as caps:
        model_turn(lane, [Turn.user("hello")])

    assert secret not in repr(caps)
    assert provider.calls[0]["extra_headers"] == {"User-Agent": secret}


def test_operator_may_set_a_credential_bearing_header_name() -> None:
    """Documented stance on credential-bearing header names.

    Names are deliberately NOT restricted: an operator may legitimately need a
    proxy/gateway auth header (e.g. ``CF-Access-Client-Id``).  The guarantee is
    only that this channel is the operator's own mapping -- a *dynamically*
    minted backend credential never arrives here.
    """
    provider = _FakeProvider()
    registry = _fake_registry(
        server_compat={"extra_headers": {"Authorization": "Bearer operator-supplied"}}
    )
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)

    model_turn(lane, [Turn.user("hello")])

    assert provider.calls[0]["extra_headers"] == {"Authorization": "Bearer operator-supplied"}
