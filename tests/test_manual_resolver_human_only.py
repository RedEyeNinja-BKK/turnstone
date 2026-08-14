"""Handler-level + real-auth tests for the eligible-operator-session manual
resolver boundary (Gate III-B-F-R1).

A strict ``manual`` approval cycle may be resolved only by an authenticated
OPERATOR SESSION holding ``tools.approve``:

- direct eligible sources: ``password`` (username/password login), ``oidc``
  (OIDC/SSO login);
- proxied eligible source: ``console-proxy`` with a trusted signed
  ``orig_src`` claim (minted by the console proxy) in {password, oidc};
- rejected: ``database`` (API token), ambiguous ``jwt`` (missing src),
  ``coordinator``, ``console``/service, ``cli``, blank/unknown, missing
  ``tools.approve``, ``admin.coordinator``-only, service scope, and any
  console-proxied request whose trusted original source is not password/oidc.

Part 1 drives the real ``make_approve_handler`` with synthetic AuthResults
(handler branching).  Part 2 exercises the real ``create_jwt`` /
``validate_jwt`` primitives (integration provenance contract).  Part 3
exercises the console-proxy re-mint provenance end-to-end.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from turnstone.core.auth import (
    ELIGIBLE_HUMAN_TOKEN_SOURCES,
    create_jwt,
    is_eligible_manual_resolver,
    validate_jwt,
)


class _FakeAuthResult:
    """Minimal AuthResult-like double (token_source + scopes + permissions +
    extra_claims)."""

    def __init__(self, user_id, scopes, token_source, permissions, extra_claims=None):
        self.user_id = user_id
        self.scopes = frozenset(scopes)
        self.token_source = token_source
        self.permissions = frozenset(permissions)
        self.extra_claims = extra_claims or {}

    def has_scope(self, scope):
        return scope in self.scopes

    def has_permission(self, perm):
        return perm in self.permissions


class _FakeRequest:
    def __init__(self, auth_result, body=None, path_params=None):
        self.state = MagicMock()
        self.state.auth_result = auth_result
        self._body = body if body is not None else {}
        self.path_params = path_params or {"ws_id": "ws1"}

    async def json(self):
        return self._body

    async def body(self):
        return json.dumps(self._body).encode()


class _FakeUI:
    def __init__(self, manual=False):
        self.calls = []
        self.pending = {
            "cycle_id": "cyc1",
            "items": [
                {
                    "call_id": "call1",
                    "func_name": "mcp__x__tool",
                    "approval_label": "mcp__x__tool",
                    "needs_approval": True,
                    "approval_mode": "manual" if manual else "ask",
                }
            ],
        }

    def find_approval_cycle(self, cycle_id=None, call_id=None):
        if cycle_id == "cyc1" or call_id == "call1" or (cycle_id is None and call_id is None):
            return self.pending
        return None

    def resolve_approval(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "cyc1"


def _make_handler(
    permission_gate=None, accepted_permissions=("tools.approve", "admin.coordinator")
):
    from turnstone.core.session_routes import SessionEndpointConfig, make_approve_handler

    ui = _FakeUI(manual=True)

    class _FakeMgr:
        def get(self, ws_id):
            return MagicMock(ui=ui)

    cfg = SessionEndpointConfig(
        manager_lookup=lambda request: (_FakeMgr(), None),
        permission_gate=permission_gate,
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
    )
    handler = make_approve_handler(cfg, accepted_permissions=accepted_permissions)
    return handler, ui


def _run(handler, auth_result, body=None):
    import asyncio

    req = _FakeRequest(
        auth_result,
        body=body or {"approved": True, "cycle_id": "cyc1", "call_id": "call1"},
    )
    return asyncio.run(handler(req))


def _body():
    return {"approved": True, "cycle_id": "cyc1", "call_id": "call1"}


# --- synthetic AuthResult fixtures -----------------------------------------

PASSWORD_HUMAN = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="password",
    permissions={"tools.approve"},
)
OIDC_HUMAN = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="oidc",
    permissions={"tools.approve"},
)
PROXY_PASSWORD = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="console-proxy",
    permissions={"tools.approve"},
    extra_claims={"orig_src": "password"},
)
PROXY_OIDC = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="console-proxy",
    permissions={"tools.approve"},
    extra_claims={"orig_src": "oidc"},
)
PROXY_NO_ORIG = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="console-proxy",
    permissions={"tools.approve"},
    extra_claims={},
)
PROXY_DB_ORIG = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="console-proxy",
    permissions={"tools.approve"},
    extra_claims={"orig_src": "database"},
)
PROXY_SVC_ORIG = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="console-proxy",
    permissions={"tools.approve"},
    extra_claims={"orig_src": "console"},
)
DATABASE_TOKEN = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="database",
    permissions={"tools.approve"},
)
JWT_AMBIGUOUS = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="jwt",
    permissions={"tools.approve"},
)
COORD = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="coordinator",
    permissions={"admin.coordinator"},
)
COORD_ONLY = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="jwt",
    permissions={"admin.coordinator"},
)
CONSOLE_SERVICE = _FakeAuthResult(
    user_id="svc",
    scopes={"read", "write", "approve", "service"},
    token_source="console",
    permissions=set(),
)
CLI = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="cli",
    permissions={"tools.approve"},
)
NO_APPROVE = _FakeAuthResult(
    user_id="user2",
    scopes={"read", "write"},
    token_source="password",
    permissions={"workstreams.create"},
)
UNKNOWN = _FakeAuthResult(
    user_id="u",
    scopes={"read", "write", "approve"},
    token_source="",
    permissions={"tools.approve"},
)


class TestManualHumanOnlyResolverHandler:
    """Handler-level branching (synthetic AuthResults)."""

    def test_password_human_allowed(self):
        handler, ui = _make_handler()
        resp = _run(handler, PASSWORD_HUMAN, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1

    def test_oidc_human_allowed(self):
        handler, ui = _make_handler()
        resp = _run(handler, OIDC_HUMAN, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1

    def test_proxy_password_origin_allowed(self):
        handler, ui = _make_handler()
        resp = _run(handler, PROXY_PASSWORD, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1
        kwargs = ui.calls[0][1]
        assert kwargs["resolver_token_source"] == "console-proxy"
        assert kwargs["resolver_orig_src"] == "password"

    def test_proxy_oidc_origin_allowed(self):
        handler, ui = _make_handler()
        resp = _run(handler, PROXY_OIDC, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1

    def test_proxy_missing_orig_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, PROXY_NO_ORIG, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_proxy_database_origin_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, PROXY_DB_ORIG, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_proxy_service_origin_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, PROXY_SVC_ORIG, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_database_api_token_rejected_even_with_tools_approve(self):
        handler, ui = _make_handler()
        resp = _run(handler, DATABASE_TOKEN, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_ambiguous_jwt_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, JWT_AMBIGUOUS, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_coordinator_automation_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, COORD, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_admin_coordinator_only_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, COORD_ONLY, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_console_service_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, CONSOLE_SERVICE, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_cli_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, CLI, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_missing_tools_approve_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, NO_APPROVE, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_blank_source_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, UNKNOWN, _body())
        assert resp.status_code == 403
        assert ui.calls == []

    def test_unauthenticated_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, None, _body())
        assert resp.status_code == 401
        assert ui.calls == []

    def test_deny_allowed_for_eligible_human(self):
        # A deliberate DENY from an eligible human must still work (fail-closed
        # deny should not be blocked by the resolver gate).
        handler, ui = _make_handler()
        resp = _run(handler, PASSWORD_HUMAN, {**_body(), "approved": False})
        assert resp.status_code == 200
        assert len(ui.calls) == 1
        assert ui.calls[0][0][0] is False


class TestEligiblePredicate:
    """Unit coverage of the canonical helper."""

    def test_direct_eligible(self):
        for src in ELIGIBLE_HUMAN_TOKEN_SOURCES:
            a = _FakeAuthResult("u", {"read", "approve"}, src, {"tools.approve"})
            assert is_eligible_manual_resolver(a)

    def test_proxy_eligible_with_trusted_orig(self):
        for orig in ELIGIBLE_HUMAN_TOKEN_SOURCES:
            a = _FakeAuthResult(
                "u",
                {"read", "approve"},
                "console-proxy",
                {"tools.approve"},
                extra_claims={"orig_src": orig},
            )
            assert is_eligible_manual_resolver(a)

    def test_proxy_ineligible_without_orig(self):
        a = _FakeAuthResult("u", {"read", "approve"}, "console-proxy", {"tools.approve"})
        assert not is_eligible_manual_resolver(a)

    def test_proxy_ineligible_with_machine_orig(self):
        for orig in ("database", "jwt", "coordinator", "console", "cli", ""):
            a = _FakeAuthResult(
                "u",
                {"read", "approve"},
                "console-proxy",
                {"tools.approve"},
                extra_claims={"orig_src": orig},
            )
            assert not is_eligible_manual_resolver(a)

    def test_service_scope_never_eligible(self):
        a = _FakeAuthResult("u", {"read", "approve", "service"}, "password", {"tools.approve"})
        assert not is_eligible_manual_resolver(a)

    def test_database_jwt_coordinator_console_cli_blank_rejected(self):
        for src in ("database", "jwt", "coordinator", "console", "cli", ""):
            a = _FakeAuthResult("u", {"read", "approve"}, src, {"tools.approve"})
            assert not is_eligible_manual_resolver(a), src

    def test_none_rejected(self):
        assert not is_eligible_manual_resolver(None)


class TestRealJwtIntegration:
    """Integration through the real create_jwt / validate_jwt primitives."""

    SECRET = "test-secret-0123456789abcdef0123456789abcdef"
    AUD = "turnstone-test"

    def _auth(self, source, scopes, permissions, extra=None):
        token = create_jwt(
            user_id="10eb9569fe8047e7857eefe2682ecff5",
            scopes=frozenset(scopes),
            source=source,
            secret=self.SECRET,
            audience=self.AUD,
            permissions=frozenset(permissions),
            extra_claims=extra,
        )
        return validate_jwt(token, self.SECRET, audience=self.AUD)

    def test_real_password_jwt_allowed(self):
        auth = self._auth("password", {"read", "write", "approve"}, {"tools.approve"})
        assert auth is not None and auth.token_source == "password"
        assert is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 200

    def test_real_oidc_jwt_allowed(self):
        auth = self._auth("oidc", {"read", "write", "approve"}, {"tools.approve"})
        assert auth is not None and auth.token_source == "oidc"
        assert is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 200

    def test_real_database_jwt_rejected(self):
        # An API-token exchange produces src=database; even with tools.approve
        # it must NOT qualify.
        auth = self._auth("database", {"read", "write", "approve"}, {"tools.approve"})
        assert auth is not None and auth.token_source == "database"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_real_jwt_missing_src_rejected(self):
        # A signed JWT with no explicit src → token_source "jwt" (ambiguous).
        import jwt as pyjwt

        now = __import__("time").time()
        token = pyjwt.encode(
            {
                "sub": "10eb9569fe8047e7857eefe2682ecff5",
                "scopes": "approve,read,write",
                "permissions": "tools.approve",
                "iss": "turnstone",
                "iat": int(now),
                "exp": int(now) + 3600,
                "aud": self.AUD,
            },
            self.SECRET,
            algorithm="HS256",
        )
        auth = validate_jwt(token, self.SECRET, audience=self.AUD)
        assert auth is not None and auth.token_source == "jwt"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_real_service_jwt_rejected(self):
        auth = self._auth("console", {"read", "write", "approve", "service"}, set())
        assert auth is not None and auth.has_scope("service")
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_real_coordinator_jwt_rejected(self):
        auth = self._auth("coordinator", {"read", "write", "approve"}, {"admin.coordinator"})
        assert auth is not None and auth.token_source == "coordinator"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_real_proxy_jwt_with_trusted_orig_allowed(self):
        # Console-proxy re-mint carries orig_src=password (signed claim).
        auth = self._auth(
            "console-proxy",
            {"read", "write", "approve"},
            {"tools.approve"},
            extra={"orig_src": "password"},
        )
        assert auth is not None and auth.token_source == "console-proxy"
        assert is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 200

    def test_real_proxy_jwt_without_trusted_orig_rejected(self):
        auth = self._auth("console-proxy", {"read", "write", "approve"}, {"tools.approve"})
        assert auth is not None and auth.token_source == "console-proxy"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403


class TestConsoleProxyProvenance:
    """Prove the console proxy re-mint preserves the TRUE outer provenance as a
    signed claim, and that a generic caller cannot self-assert it."""

    SECRET = "test-secret-0123456789abcdef0123456789abcdef"
    AUD = "turnstone-server-aud"

    def _mint_proxy_token(self, outer_source, scopes, permissions, extra=None):
        """Replicate the console _proxy_auth_headers re-mint for a given outer
        AuthResult: source=console-proxy (unless coordinator/service), with
        orig_src = outer token_source."""
        is_coord = outer_source == "coordinator"
        is_console_service = outer_source == "console" and "service" in scopes
        source = "coordinator" if is_coord else "console" if is_console_service else "console-proxy"
        extra_claims = dict(extra or {})
        extra_claims["orig_src"] = outer_source
        return create_jwt(
            user_id="10eb9569fe8047e7857eefe2682ecff5",
            scopes=frozenset(scopes),
            source=source,
            secret=self.SECRET,
            audience=self.AUD,
            permissions=frozenset(permissions),
            extra_claims=extra_claims,
        )

    def _proxy_auth(self, outer_source, scopes, permissions):
        token = self._mint_proxy_token(outer_source, scopes, permissions)
        auth = validate_jwt(token, self.SECRET, audience=self.AUD)
        assert auth is not None
        return auth

    def test_password_origin_via_proxy_allowed(self):
        auth = self._proxy_auth("password", {"read", "write", "approve"}, {"tools.approve"})
        assert auth.token_source == "console-proxy"
        assert is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 200

    def test_oidc_origin_via_proxy_allowed(self):
        auth = self._proxy_auth("oidc", {"read", "write", "approve"}, {"tools.approve"})
        assert is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 200

    def test_database_origin_via_proxy_rejected(self):
        auth = self._proxy_auth("database", {"read", "write", "approve"}, {"tools.approve"})
        assert auth.token_source == "console-proxy"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_service_origin_via_proxy_rejected(self):
        auth = self._proxy_auth("console", {"read", "write", "approve", "service"}, set())
        assert auth.has_scope("service")
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_coordinator_origin_via_proxy_rejected(self):
        auth = self._proxy_auth("coordinator", {"read", "write", "approve"}, {"admin.coordinator"})
        assert auth.token_source == "coordinator"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_ambiguous_origin_via_proxy_rejected(self):
        auth = self._proxy_auth("jwt", {"read", "write", "approve"}, {"tools.approve"})
        assert auth.token_source == "console-proxy"
        assert auth.extra_claims.get("orig_src") == "jwt"
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403

    def test_missing_origin_via_proxy_rejected(self):
        # A proxy token WITHOUT the trusted orig_src claim (e.g. forged old
        # format or a direct mint lacking the claim) must fail closed.
        token = create_jwt(
            user_id="10eb9569fe8047e7857eefe2682ecff5",
            scopes=frozenset({"read", "write", "approve"}),
            source="console-proxy",
            secret=self.SECRET,
            audience=self.AUD,
            permissions=frozenset({"tools.approve"}),
        )
        auth = validate_jwt(token, self.SECRET, audience=self.AUD)
        assert auth is not None
        assert not is_eligible_manual_resolver(auth)
        handler, ui = _make_handler()
        resp = _run(handler, auth, _body())
        assert resp.status_code == 403


class TestNonManualAskUnaffected:
    """Ordinary (non-manual) ask semantics must remain unchanged."""

    def _ask_handler(self):
        from turnstone.core.session_routes import SessionEndpointConfig, make_approve_handler

        ui = _FakeUI(manual=False)

        class _FakeMgr:
            def get(self, ws_id):
                return MagicMock(ui=ui)

        cfg = SessionEndpointConfig(
            manager_lookup=lambda request: (_FakeMgr(), None),
            permission_gate=None,
            tenant_check=None,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
        )
        return make_approve_handler(
            cfg, accepted_permissions=("tools.approve", "admin.coordinator")
        ), ui

    def test_ask_human_allowed(self):
        handler, ui = self._ask_handler()
        resp = _run(handler, PASSWORD_HUMAN, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1

    def test_ask_service_bypass_still_allowed(self):
        # Pre-existing contract: non-manual ask cycles allow service bypass.
        handler, ui = self._ask_handler()
        resp = _run(handler, CONSOLE_SERVICE, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1

    def test_ask_admin_coordinator_allowed(self):
        handler, ui = self._ask_handler()
        resp = _run(handler, COORD, _body())
        assert resp.status_code == 200
        assert len(ui.calls) == 1
