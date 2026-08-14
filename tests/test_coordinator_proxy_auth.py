"""Tests for console _proxy_auth_headers preserving the coordinator src claim.

Verifies C8 of the coordinator plan: when a console handler processes an
inbound request authenticated with a coordinator-minted JWT (``src ==
"coordinator"``), the upstream JWT the console mints for the proxied
request preserves that source plus the ``coord_ws_id`` custom claim.
For non-coordinator inbound tokens the re-mint still uses
``"console-proxy"`` as before — the existing behaviour is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt as pyjwt

from turnstone.console.server import _proxy_auth_headers
from turnstone.core.auth import JWT_AUD_SERVER, AuthResult, is_eligible_manual_resolver

_SECRET = "x" * 64


def _build_request(auth_result: AuthResult | None):
    """Minimal Request-alike for _proxy_auth_headers."""
    state = SimpleNamespace(auth_result=auth_result)
    app_state = SimpleNamespace(jwt_secret=_SECRET, proxy_token_mgr=None)
    app = MagicMock()
    app.state = app_state
    req = MagicMock()
    req.state = state
    req.app = app
    return req


def _decode(headers: dict[str, str]) -> dict:
    token = headers["Authorization"].removeprefix("Bearer ")
    return pyjwt.decode(token, _SECRET, algorithms=["HS256"], audience=JWT_AUD_SERVER)


def test_console_proxy_uses_console_proxy_source_by_default():
    """Non-coordinator inbound tokens still mint src='console-proxy'."""
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"write"}),
        token_source="jwt",
        permissions=frozenset(),
    )
    headers = _proxy_auth_headers(_build_request(auth))
    payload = _decode(headers)
    assert payload["src"] == "console-proxy"
    assert "coord_ws_id" not in payload


def test_console_service_source_is_preserved_for_trusted_forwarding():
    """Only the console service identity may retain ``src=console``."""
    auth = AuthResult(
        user_id="console-service",
        scopes=frozenset({"read", "write", "service"}),
        token_source="console",
        permissions=frozenset({"workstreams.create"}),
    )
    payload = _decode(_proxy_auth_headers(_build_request(auth)))
    assert payload["src"] == "console"
    assert set(payload["scopes"].split(",")) == {"read", "write", "service"}


def test_unscoped_console_claim_is_demoted_to_console_proxy():
    """An ordinary principal cannot gain owner-override trust through ``src``."""
    auth = AuthResult(
        user_id="ordinary-user",
        scopes=frozenset({"read", "write"}),
        token_source="console",
        permissions=frozenset({"workstreams.create"}),
    )
    payload = _decode(_proxy_auth_headers(_build_request(auth)))
    assert payload["src"] == "console-proxy"


def test_coordinator_source_is_preserved_on_remint():
    """Inbound src='coordinator' → outbound src='coordinator'."""
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"approve"}),
        token_source="coordinator",
        permissions=frozenset({"admin.coordinator"}),
        extra_claims={"coord_ws_id": "coord-42"},
    )
    headers = _proxy_auth_headers(_build_request(auth))
    payload = _decode(headers)
    assert payload["src"] == "coordinator"
    assert payload["coord_ws_id"] == "coord-42"


def test_coord_ws_id_absent_when_not_in_inbound_claims():
    """Defensive: if the inbound token is src=coordinator but missing the
    coord_ws_id claim (shouldn't happen in practice), the re-mint skips
    the custom claim rather than panicking."""
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"write"}),
        token_source="coordinator",
        permissions=frozenset(),
    )
    headers = _proxy_auth_headers(_build_request(auth))
    payload = _decode(headers)
    assert payload["src"] == "coordinator"
    assert "coord_ws_id" not in payload


def test_empty_auth_falls_back_to_service_token_or_empty():
    """Without auth_result.user_id, falls through to ServiceTokenManager."""
    auth = AuthResult(
        user_id="",
        scopes=frozenset(),
        token_source="config",
        permissions=frozenset(),
    )
    # No proxy_token_mgr configured → empty headers.
    headers = _proxy_auth_headers(_build_request(auth))
    assert headers == {}


# ---------------------------------------------------------------------------
# Gate III-B-F-R2 - direct _proxy_auth_headers trusted-origin regression
#
# These exercise the REAL production helper (turnstone.console.server.
# _proxy_auth_headers), not a reimplementation.  They pin the signed
# ``orig_src`` claim that the node uses to enforce the eligible-operator-
# session manual-resolver invariant: a proxied request may only be treated
# as a human operator session when the trusted console attested the outer
# source was password/oidc.
# ---------------------------------------------------------------------------


def _proxy_auth_result(auth: AuthResult) -> AuthResult:
    """Run the REAL _proxy_auth_headers and validate the emitted node JWT."""
    import turnstone.core.auth as auth_mod

    headers = _proxy_auth_headers(_build_request(auth))
    token = headers["Authorization"].removeprefix("Bearer ")
    result = auth_mod.validate_jwt(token, _SECRET, audience=JWT_AUD_SERVER)
    assert result is not None, "node-facing JWT must validate"
    return result


def test_direct_password_origin_via_real_proxy_allowed():
    """Outer password login → actual proxy → src=console-proxy, signed
    orig_src=password, user+permissions preserved, no service scope, eligible."""
    auth = AuthResult(
        user_id="10eb9569fe8047e7857eefe2682ecff5",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="password",
        permissions=frozenset({"tools.approve"}),
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "console-proxy"
    assert node.extra_claims.get("orig_src") == "password"
    assert node.user_id == "10eb9569fe8047e7857eefe2682ecff5"
    assert "tools.approve" in node.permissions
    assert not node.has_scope("service")
    assert is_eligible_manual_resolver(node) is True


def test_direct_oidc_origin_via_real_proxy_allowed():
    auth = AuthResult(
        user_id="10eb9569fe8047e7857eefe2682ecff5",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="oidc",
        permissions=frozenset({"tools.approve"}),
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "console-proxy"
    assert node.extra_claims.get("orig_src") == "oidc"
    assert is_eligible_manual_resolver(node) is True


def test_direct_database_origin_via_real_proxy_rejected():
    """API-token origin must NOT become human-qualified through the proxy."""
    auth = AuthResult(
        user_id="10eb9569fe8047e7857eefe2682ecff5",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="database",
        permissions=frozenset({"tools.approve"}),
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "console-proxy"
    assert node.extra_claims.get("orig_src") == "database"
    assert is_eligible_manual_resolver(node) is False


def test_direct_ambiguous_jwt_origin_via_real_proxy_rejected():
    auth = AuthResult(
        user_id="user-1",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="jwt",
        permissions=frozenset({"tools.approve"}),
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "console-proxy"
    assert node.extra_claims.get("orig_src") == "jwt"
    assert is_eligible_manual_resolver(node) is False


def test_direct_coordinator_origin_via_real_proxy_rejected_and_src_preserved():
    auth = AuthResult(
        user_id="10eb9569fe8047e7857eefe2682ecff5",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="coordinator",
        permissions=frozenset({"admin.coordinator"}),
        extra_claims={"coord_ws_id": "coord1"},
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "coordinator"  # src preserved, not console-proxy
    assert node.extra_claims.get("orig_src") == "coordinator"
    assert node.extra_claims.get("coord_ws_id") == "coord1"
    assert is_eligible_manual_resolver(node) is False


def test_direct_console_service_origin_via_real_proxy_rejected():
    auth = AuthResult(
        user_id="console-service",
        scopes=frozenset({"read", "write", "approve", "service"}),
        token_source="console",
        permissions=frozenset(),
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "console"  # service source preserved
    assert node.extra_claims.get("orig_src") == "console"
    assert node.has_scope("service")
    assert is_eligible_manual_resolver(node) is False


def test_spoofed_incoming_orig_src_is_overwritten_by_trusted_proxy():
    """A malicious/foreign inbound claim orig_src=password must NOT survive
    the re-mint: the trusted console overwrites orig_src from the AUTHENTICATED
    outer token_source (database), so the node sees database and rejects."""
    auth = AuthResult(
        user_id="10eb9569fe8047e7857eefe2682ecff5",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="database",
        permissions=frozenset({"tools.approve"}),
        extra_claims={"orig_src": "password"},  # spoof attempt
    )
    node = _proxy_auth_result(auth)
    assert node.extra_claims.get("orig_src") == "database", (
        "proxy must overwrite orig_src from authenticated outer token_source"
    )
    assert node.extra_claims.get("orig_src") != "password"
    assert is_eligible_manual_resolver(node) is False


def test_spoofed_incoming_orig_src_coordinator_overwritten():
    auth = AuthResult(
        user_id="10eb9569fe8047e7857eefe2682ecff5",
        scopes=frozenset({"read", "write", "approve"}),
        token_source="coordinator",
        permissions=frozenset({"admin.coordinator"}),
        extra_claims={"orig_src": "password"},  # spoof attempt
    )
    node = _proxy_auth_result(auth)
    assert node.token_source == "coordinator"
    assert node.extra_claims.get("orig_src") == "coordinator"
    assert is_eligible_manual_resolver(node) is False
