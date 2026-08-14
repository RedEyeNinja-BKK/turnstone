"""E2E handler-level tests for the human-only manual resolver boundary.

Gate III-B-F: a manual approval cycle may be resolved only by an
authenticated HUMAN operator holding ``tools.approve``.  These tests drive
the real ``make_approve_handler`` HTTP handler (not direct
``ui.resolve_approval`` calls) with synthetic AuthResult states to prove the
server-side authority gate:

- service-scoped caller → 403 (even though service normally bypasses RBAC)
- ``admin.coordinator``-only caller → 403
- coordinator/service automation (token_source=coordinator) → 403
- caller lacking ``tools.approve`` → 403
- qualifying human (jwt/console-proxy + tools.approve) → allowed (resolution
  reaches resolve_approval)
- ordinary non-manual ``ask`` semantics are NOT broken by the manual-only gate
"""

from __future__ import annotations

from unittest.mock import MagicMock


class _FakeAuthResult:
    """Minimal AuthResult-like double (frozen dataclass, but we construct directly)."""

    def __init__(self, user_id, scopes, token_source, permissions):
        self.user_id = user_id
        self.scopes = frozenset(scopes)
        self.token_source = token_source
        self.permissions = frozenset(permissions)
        self.extra_claims = {}

    def has_scope(self, scope):
        return scope in self.scopes

    def has_permission(self, perm):
        return perm in self.permissions


class _FakeRequest:
    """Minimal Starlette Request-like double carrying state.auth_result."""

    def __init__(self, auth_result, body=None, path_params=None):
        self.state = MagicMock()
        self.state.auth_result = auth_result
        self._body = body if body is not None else {}
        self.path_params = path_params or {"ws_id": "ws1"}

    async def json(self):
        return self._body

    async def body(self):
        import json as _json

        return _json.dumps(self._body).encode()


class _FakeUI:
    """Session UI double with resolve_approval spy + find_approval_cycle."""

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
    """Build a real make_approve_handler with a stub manager."""
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
        auth_result, body=body or {"approved": True, "cycle_id": "cyc1", "call_id": "call1"}
    )
    return asyncio.run(handler(req))


HUMAN = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="jwt",
    permissions={"tools.approve"},
)
HUMAN_PROXY = _FakeAuthResult(
    user_id="10eb9569fe8047e7857eefe2682ecff5",
    scopes={"read", "write", "approve"},
    token_source="console-proxy",
    permissions={"tools.approve"},
)
SERVICE = _FakeAuthResult(
    user_id="svc",
    scopes={"read", "write", "approve", "service"},
    token_source="console",
    permissions=set(),
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
NO_APPROVE = _FakeAuthResult(
    user_id="user2",
    scopes={"read", "write"},
    token_source="jwt",
    permissions={"workstreams.create"},
)


class TestManualHumanOnlyResolver:
    def test_service_scoped_caller_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, SERVICE)
        assert resp.status_code == 403
        assert ui.calls == []

    def test_coordinator_automation_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, COORD)
        assert resp.status_code == 403
        assert ui.calls == []

    def test_admin_coordinator_only_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, COORD_ONLY)
        assert resp.status_code == 403
        assert ui.calls == []

    def test_missing_tools_approve_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, NO_APPROVE)
        assert resp.status_code == 403
        assert ui.calls == []

    def test_human_direct_allowed(self):
        handler, ui = _make_handler()
        resp = _run(handler, HUMAN)
        assert resp.status_code == 200
        assert len(ui.calls) == 1
        # provenance should flow into resolve_approval kwargs
        kwargs = ui.calls[0][1]
        assert kwargs["resolver_token_source"] == "jwt"
        assert kwargs["resolver_service_scope"] is False

    def test_human_proxy_allowed(self):
        handler, ui = _make_handler()
        resp = _run(handler, HUMAN_PROXY)
        assert resp.status_code == 200
        assert len(ui.calls) == 1
        kwargs = ui.calls[0][1]
        assert kwargs["resolver_token_source"] == "console-proxy"
        assert kwargs["resolver_service_scope"] is False

    def test_unauthenticated_rejected(self):
        handler, ui = _make_handler()
        resp = _run(handler, None)
        assert resp.status_code == 401
        assert ui.calls == []


class TestNonManualAskUnaffected:
    def test_ask_cycle_still_resolvable_by_approve_holder(self):
        # A NON-manual (ordinary ask) cycle must retain existing semantics:
        # a caller with tools.approve can still resolve it.  The manual-only
        # gate must not break ordinary approvals.
        from turnstone.core.session_routes import SessionEndpointConfig, make_approve_handler

        ui = _FakeUI(manual=False)

        # find_approval_cycle for non-manual: our _FakeUI.pending has
        # approval_mode=ask → manual_cycle False.
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
        handler = make_approve_handler(
            cfg, accepted_permissions=("tools.approve", "admin.coordinator")
        )
        resp = _run(handler, HUMAN)
        assert resp.status_code == 200
        assert len(ui.calls) == 1

    def test_ask_cycle_service_bypass_still_allowed(self):
        # Ordinary (non-manual) ask cycles retain the pre-existing service
        # bypass contract — the hardening is MANUAL-specific.
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
        handler = make_approve_handler(
            cfg, accepted_permissions=("tools.approve", "admin.coordinator")
        )
        resp = _run(handler, SERVICE)
        assert resp.status_code == 200
        assert len(ui.calls) == 1
