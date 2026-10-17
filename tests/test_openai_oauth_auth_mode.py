# SPDX-License-Identifier: Apache-2.0
"""openai_oauth auth mode: registry acceptance and credential-source wiring.

The direct-account OAuth lane is a *dynamic* auth mode like the Entra ones, but
its credential does not come from an OIDC token-exchange leg — it comes from the
local coordinated token store (Option B). These tests pin both halves:

* the registry accepts and normalises ``auth_mode="openai_oauth"``;
* the dispatch seam mints that credential from the store, without an acting
  user, and fails closed when the store is unusable.

Run standalone::

    TMPDIR=/var/tmp PYTHONPATH=<tree> /opt/turnstone-venv/bin/python tests/test_openai_oauth_auth_mode.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import turnstone.core.model_registry as mr  # noqa: E402
from turnstone.core.model_backend_auth import (  # noqa: E402
    DIRECT_OAUTH_RESOURCE,
    BackendAuthUnavailableError,
    DirectOAuthBindingError,
    direct_oauth_endpoint_problem,
    resolve_model_backend_auth_token,
)
from turnstone.core.model_registry import ModelConfig  # noqa: E402

_TMP = "/var/tmp"
_ROOT = pathlib.Path(os.environ.get("R2_REPO") or pathlib.Path(__file__).resolve().parents[1])


class _ConfigStore:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}

    def get(self, key: str) -> Any:
        return self.values.get(key)


def _cfg(mode: str = "openai_oauth", **over: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "alias": "gpt-6.1-sol-native",
        "base_url": DIRECT_OAUTH_RESOURCE,
        "api_key": "",  # keyless: fail-closed applies
        "model": "gpt-6.1-sol",
        "provider": "openai",  # the Responses surface; see _MODEL_PROVIDERS
        "auth_mode": mode,
        "obo_audience": DIRECT_OAUTH_RESOURCE,
    }
    base.update(over)
    return ModelConfig(**base)


class _NoTokenMint:
    """Mint client that always refuses, for the non-direct fallback contract."""

    def mint_model_obo_token_sync(self, **_kwargs: Any) -> None:
        return None

    def mint_app_token_sync(self, **_kwargs: Any) -> None:
        return None


def _seed_store(root: pathlib.Path, *, expires_in: int = 3600) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "oaiapp_TEST",
                "access_token": "at-FROM-STORE",
                "refresh_token": "rt-SECRET",
                "expires_at": int(time.time()) + expires_in,
                "granted_scopes": "openid profile offline_access chatgpt.tokens.use.direct",
                "sub": "sub-AAA",
                "email": "operator@example.invalid",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(root / "credentials.json", 0o600)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_mode_is_known_and_dynamic() -> None:
    assert "openai_oauth" in mr.MODEL_AUTH_MODES
    assert "openai_oauth" in mr.DYNAMIC_MODEL_AUTH_MODES
    assert "openai_oauth" in mr.DIRECT_OAUTH_MODEL_AUTH_MODES


def test_mode_is_not_an_obo_exchange_mode() -> None:
    """It must not claim an exchange leg it does not use."""
    assert "openai_oauth" not in mr.MODEL_AUTH_MODE_PROFILES
    assert "openai_oauth" not in mr.SCOPES_MODEL_AUTH_MODES
    assert "openai_oauth" not in mr.APP_IDENTITY_MODEL_AUTH_MODES


def test_normalizer_accepts_the_mode_with_a_resource() -> None:
    mode, audience, scopes = mr._normalize_auth_mode(
        "gpt-6.1-sol-native", "openai_oauth", "https://api.openai.com/v1", ""
    )
    assert mode == "openai_oauth"
    assert audience == "https://api.openai.com/v1"
    assert scopes == ""


def test_normalizer_still_requires_an_audience() -> None:
    try:
        mr._normalize_auth_mode("a", "openai_oauth", "", "")
    except mr.ModelAuthConfigError as exc:
        assert "obo_audience" in str(exc)
    else:
        raise AssertionError("a direct-oauth row without a resource must be refused")


def test_registry_reports_dynamic_auth() -> None:
    registry = mr.ModelRegistry(models={"sol": _cfg()}, default="sol")
    assert registry.has_dynamic_auth() is True


# ---------------------------------------------------------------------------
# credential source
# ---------------------------------------------------------------------------


def test_mints_from_the_store_without_an_acting_user() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed_store(root)
        token = resolve_model_backend_auth_token(
            "gpt-6.1-sol-native",
            _cfg(),
            principal_id="",  # no acting user: this is an app identity
            config_store=_ConfigStore({"model.openai_oauth_store": str(root)}),
            mint_client=None,
        )
        assert token == "at-FROM-STORE"


def test_unusable_store_fails_closed_on_a_keyless_alias() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        missing = pathlib.Path(tmp) / "absent"
        try:
            resolve_model_backend_auth_token(
                "gpt-6.1-sol-native",
                _cfg(),
                principal_id="",
                config_store=_ConfigStore({"model.openai_oauth_store": str(missing)}),
                mint_client=None,
            )
        except BackendAuthUnavailableError:
            pass
        else:
            raise AssertionError("a keyless direct-oauth alias must not fall through")


def test_failure_message_carries_no_credential() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed_store(root, expires_in=-10)
        # A parked store is the loud-failure case.
        data = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
        data["state"] = "needs_reauth"
        (root / "credentials.json").write_text(json.dumps(data), encoding="utf-8")
        try:
            resolve_model_backend_auth_token(
                "sol",
                _cfg(),
                principal_id="",
                config_store=_ConfigStore({"model.openai_oauth_store": str(root)}),
                mint_client=None,
            )
        except BackendAuthUnavailableError as exc:
            assert "rt-SECRET" not in str(exc)
            assert "at-FROM-STORE" not in str(exc)
        else:
            raise AssertionError("a parked store must refuse")


def test_default_store_path_is_the_canonical_runtime_location() -> None:
    from turnstone.core.model_backend_auth import DEFAULT_OPENAI_OAUTH_STORE

    assert DEFAULT_OPENAI_OAUTH_STORE == "/srv/turnstone/credentials/runtime/openai-siwc"


def test_static_alias_is_untouched_by_the_new_mode() -> None:
    """Regression: a static row must not be routed through the OAuth branch."""
    assert (
        resolve_model_backend_auth_token(
            "plain",
            _cfg(mode="static", obo_audience=""),
            principal_id="",
            config_store=_ConfigStore(),
            mint_client=None,
        )
        is None
    )


# ---------------------------------------------------------------------------
# fail-closed is unconditional for direct OAuth
# ---------------------------------------------------------------------------


def test_static_key_never_enables_a_fallback() -> None:
    """A row carrying ``api_key`` must STILL refuse when the store fails.

    The shared dynamic formula is ``configured_fail_closed or not api_key``.
    Inheriting it here would let a subscriber row with a leftover static key
    silently serve through that key whenever the OAuth store was unusable —
    the credential would appear to work while requests left on a different
    identity than the operator configured.
    """
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        missing = pathlib.Path(tmp) / "absent"
        try:
            resolve_model_backend_auth_token(
                "gpt-6.1-sol-native",
                _cfg(api_key="sk-not-a-real-key"),  # nonempty static key
                principal_id="",
                config_store=_ConfigStore(
                    {
                        "model.openai_oauth_store": str(missing),
                        "model.auth_fail_closed": False,  # global fallback OFF
                    }
                ),
                mint_client=None,
            )
        except BackendAuthUnavailableError:
            pass
        else:
            raise AssertionError("a direct-oauth row must not fall back to its static key")


def test_global_fail_closed_flag_is_irrelevant() -> None:
    """Turning the global flag ON must not be what makes it fail closed."""
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed_store(root, expires_in=-10)
        data = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
        data["state"] = "needs_reauth"
        (root / "credentials.json").write_text(json.dumps(data), encoding="utf-8")
        for fail_closed in (False, True):
            try:
                resolve_model_backend_auth_token(
                    "sol",
                    _cfg(api_key="sk-not-a-real-key"),
                    principal_id="",
                    config_store=_ConfigStore(
                        {
                            "model.openai_oauth_store": str(root),
                            "model.auth_fail_closed": fail_closed,
                        }
                    ),
                    mint_client=None,
                )
            except BackendAuthUnavailableError:
                continue
            raise AssertionError(f"fallback was possible with auth_fail_closed={fail_closed}")


def test_missing_resource_refuses_instead_of_returning_none() -> None:
    """No resource means no call — not "use the static key".

    The shared prelude returns ``None`` for a dynamic mode without an
    audience. Returning ``None`` here would hand the SDK the row's static
    credential, which is precisely the substitution this lane forbids.
    """
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed_store(root)
        try:
            resolve_model_backend_auth_token(
                "sol",
                _cfg(obo_audience=""),
                principal_id="",
                config_store=_ConfigStore({"model.openai_oauth_store": str(root)}),
                mint_client=None,
            )
        except DirectOAuthBindingError as exc:
            assert "no resource" in str(exc).lower()
        else:
            raise AssertionError("a resource-less direct-oauth row must refuse")


# ---------------------------------------------------------------------------
# endpoint and resource binding
# ---------------------------------------------------------------------------


def test_mismatched_resource_is_refused() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed_store(root)
        try:
            resolve_model_backend_auth_token(
                "sol",
                _cfg(obo_audience="https://api.openai.com/v2"),
                principal_id="",
                config_store=_ConfigStore({"model.openai_oauth_store": str(root)}),
                mint_client=None,
            )
        except DirectOAuthBindingError:
            pass
        else:
            raise AssertionError("a mismatched OAuth resource must be refused")


def test_alternate_host_is_refused() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed_store(root)
        try:
            resolve_model_backend_auth_token(
                "sol",
                _cfg(base_url="https://api.openai.com.evil.example/v1"),
                principal_id="",
                config_store=_ConfigStore({"model.openai_oauth_store": str(root)}),
                mint_client=None,
            )
        except DirectOAuthBindingError:
            pass
        else:
            raise AssertionError("an alternate host must not receive the credential")


def test_plain_http_is_refused() -> None:
    assert direct_oauth_endpoint_problem("openai", "http://api.openai.com/v1") is not None


def test_trailing_slash_variant_is_refused() -> None:
    assert direct_oauth_endpoint_problem("openai", "https://api.openai.com/v1/") is not None


def test_credential_bearing_url_is_refused() -> None:
    url = "https://" + "user" + ":" + "pw" + "@api.openai.com/v1"
    assert direct_oauth_endpoint_problem("openai", url) is not None
    assert direct_oauth_endpoint_problem("openai", "https://api.openai.com/v1?x=1") is not None
    assert direct_oauth_endpoint_problem("openai", "https://api.openai.com/v1#frag") is not None


def test_non_default_port_is_refused() -> None:
    assert direct_oauth_endpoint_problem("openai", "https://api.openai.com:8443/v1") is not None


def test_compat_provider_is_refused() -> None:
    """``openai-compatible`` accepts any base_url, so it cannot carry this grant."""
    assert direct_oauth_endpoint_problem("openai-compatible", DIRECT_OAUTH_RESOURCE) is not None
    assert direct_oauth_endpoint_problem("openai", DIRECT_OAUTH_RESOURCE) is None


def test_binding_is_checked_before_the_store_is_read() -> None:
    """A mis-bound row never touches the credential at all."""

    class _ExplodingStore:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("the store must not be opened for a mis-bound row")

    import turnstone.core.oauth_token_store as ots

    original = ots.OAuthTokenStore
    ots.OAuthTokenStore = _ExplodingStore  # type: ignore[assignment]
    try:
        try:
            resolve_model_backend_auth_token(
                "sol",
                _cfg(base_url="https://api.openai.com/v1/"),
                principal_id="",
                config_store=_ConfigStore(),
                mint_client=None,
            )
        except DirectOAuthBindingError:
            pass
        else:
            raise AssertionError("expected a binding refusal")
    finally:
        ots.OAuthTokenStore = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# the other modes keep their contracts
# ---------------------------------------------------------------------------


def test_entra_fallback_contract_is_unchanged() -> None:
    """An Entra row with a static key still falls back when minting fails.

    Only the direct-OAuth branch changed. If this starts raising, the
    fail-closed correction has leaked into modes it was not meant to touch.
    """
    assert (
        resolve_model_backend_auth_token(
            "entra-model",
            _cfg(mode="entra_obo", api_key="sk-static"),
            principal_id="user-1",
            config_store=_ConfigStore(),
            mint_client=_NoTokenMint(),
        )
        is None
    )


def test_direct_oauth_client_disables_redirects() -> None:
    """The bearer's transport must not follow a 30x to another host."""
    registry = mr.ModelRegistry(models={"sol": _cfg()}, default="sol")
    client = registry.get_client("sol")
    http_client = getattr(client, "_client", None)
    assert http_client is not None
    assert getattr(http_client, "follow_redirects", None) is False


def test_static_client_keeps_the_sdk_redirect_default() -> None:
    registry = mr.ModelRegistry(
        models={"plain": _cfg(mode="static", obo_audience="", api_key="sk-x")}, default="plain"
    )
    client = registry.get_client("plain")
    http_client = getattr(client, "_client", None)
    assert http_client is not None
    assert getattr(http_client, "follow_redirects", None) is True


# ---------------------------------------------------------------------------
# the write path mirrors the runtime binding
# ---------------------------------------------------------------------------


def test_console_create_and_update_both_enforce_the_binding() -> None:
    """Defence in depth: neither write path may accept a mis-bound row.

    The runtime seam is authoritative, but a row that is refused only at
    serve time is a silent misconfiguration; both admin paths call the same
    predicate the runtime uses.
    """
    src = (
        pathlib.Path(mr.__file__).resolve().parent.parent
        / "console"
        / "server.py"
    ).read_text(encoding="utf-8")
    assert "from turnstone.core.model_backend_auth import direct_oauth_endpoint_problem" in src
    assert src.count("_direct_oauth_endpoint_error(") >= 3, (
        "expected the definition plus both call sites (create and update)"
    )
    # The update call must read the POST-merge provider/base_url, not the
    # request's own fields: re-pointing an armed row is the edit that matters.
    window = src[src.index("# Endpoint binding is enforced on the POST-merge row") :]
    assert 'updates.get("provider"' in window
    assert 'updates.get("base_url"' in window





# ---------------------------------------------------------------------------
# boot/store requirements: direct OAuth must not inherit the MCP key guard
# ---------------------------------------------------------------------------


class _BareState:
    """app_state without an MCP token store, as on a keyless deployment."""


def test_direct_oauth_only_does_not_require_the_mcp_token_store() -> None:
    """The lane reads a file store, so it must not force the MCP cipher.

    ``has_dynamic_auth`` and the shared key guard both used to treat every
    dynamic mode as an MCP-cached mint. A direct-OAuth-only deployment would
    then abort at boot demanding an encryption key it never touches.
    """
    models = {"sol": _cfg()}
    registry = mr.ModelRegistry(models=models, default="sol")
    assert registry.has_dynamic_auth() is True
    assert registry.has_token_store_dynamic_auth() is False
    assert mr.dynamic_auth_key_error(models, _BareState()) == ""


def test_entra_still_requires_the_mcp_token_store() -> None:
    models = {"entra": _cfg(mode="entra_obo")}
    registry = mr.ModelRegistry(models=models, default="entra")
    assert registry.has_token_store_dynamic_auth() is True
    assert mr.dynamic_auth_key_error(models, _BareState()) != ""


def test_a_mixed_registry_still_requires_the_store() -> None:
    models = {"sol": _cfg(), "entra": _cfg(mode="entra_obo", alias="entra")}
    registry = mr.ModelRegistry(models=models, default="sol")
    assert registry.has_token_store_dynamic_auth() is True


def _run_all() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
