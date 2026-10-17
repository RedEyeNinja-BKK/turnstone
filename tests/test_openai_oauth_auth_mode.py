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
    BackendAuthUnavailableError,
    resolve_model_backend_auth_token,
)
from turnstone.core.model_registry import ModelConfig  # noqa: E402

_TMP = "/var/tmp"


class _ConfigStore:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}

    def get(self, key: str) -> Any:
        return self.values.get(key)


def _cfg(mode: str = "openai_oauth", **over: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "alias": "gpt-6.1-sol-native",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",  # keyless: fail-closed applies
        "model": "gpt-6.1-sol",
        "provider": "openai-responses",
        "auth_mode": mode,
        "obo_audience": "https://api.openai.com/v1",
    }
    base.update(over)
    return ModelConfig(**base)


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
