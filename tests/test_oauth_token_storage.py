"""Shared token lifecycle and MCP projections on the selected storage backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from turnstone.core.model_oauth import model_app_cache_server, model_obo_cache_server
from turnstone.core.token_store.store import MODEL_APP_MINT_PRINCIPAL

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend


def _seed_token(
    storage: StorageBackend, user: str, key: str, *, expires: str | None = None
) -> None:
    storage.create_oauth_token(
        user,
        key,
        access_token_ct=b"access",
        refresh_token_ct=None,
        expires_at=expires,
        scopes="scope",
        as_issuer="https://idp.example.com",
        audience="api://resource",
    )


def _seed_auth_state(storage: StorageBackend, key: str) -> None:
    storage.create_mcp_oauth_pending_state(
        f"state-{key}",
        "u1",
        key,
        "verifier",
        "/settings",
        as_issuer="https://as.example.com",
        iss_parameter_supported=False,
        server_id=f"id-{key}",
        server_url="https://mcp.example.com/mcp",
        audience="https://mcp.example.com",
    )
    storage.upsert_mcp_pending_consent("u1", key, "needs_auth", None, "2026-01-01T00:00:00")


def test_model_lifecycle_purge_only_deletes_the_alias_tokens(backend: StorageBackend) -> None:
    from turnstone.console.server import _purge_model_mint_cache

    obo_key = model_obo_cache_server("gateway")
    app_key = model_app_cache_server("gateway")
    sibling = model_obo_cache_server("sibling")
    for key in (obo_key, app_key, sibling, "mcp-server"):
        for user in ("u1", "u2", MODEL_APP_MINT_PRINCIPAL):
            _seed_token(backend, user, key)
        _seed_auth_state(backend, key)
    backend.upsert_oidc_user_credential(
        "u1", "https://idp.example.com", refresh_token_ct=b"credential"
    )
    credential = backend.get_oidc_user_credential("u1", "https://idp.example.com")
    pending_consent = backend.list_mcp_pending_consent_by_user("u1")

    _purge_model_mint_cache(backend, "definition-1", "gateway")

    for user in ("u1", "u2", MODEL_APP_MINT_PRINCIPAL):
        assert backend.get_oauth_token(user, obo_key) is None
        assert backend.get_oauth_token(user, app_key) is None
        assert backend.get_oauth_token(user, sibling) is not None
        assert backend.get_oauth_token(user, "mcp-server") is not None
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") == credential
    assert backend.list_mcp_pending_consent_by_user("u1") == pending_consent
    for key in (obo_key, app_key, sibling, "mcp-server"):
        assert backend.pop_mcp_oauth_pending_state(f"state-{key}") is not None
    assert backend.delete_oauth_tokens_by_key(obo_key) == 0
    assert backend.delete_oauth_tokens_by_key(sibling) == 3


def test_mcp_purge_rolls_back_both_deletes_and_preserves_pending_consent(backend: Any) -> None:
    for key in ("mcp-server", "other-server"):
        for user in ("u1", "u2"):
            _seed_token(backend, user, key)
        _seed_auth_state(backend, key)
    backend.upsert_oidc_user_credential(
        "u1", "https://idp.example.com", refresh_token_ct=b"credential"
    )
    pending_consent = backend.list_mcp_pending_consent_by_user("u1")
    credential = backend.get_oidc_user_credential("u1", "https://idp.example.com")
    rows = [backend.get_oauth_token(user, "mcp-server") for user in ("u1", "u2")]

    def fail_pending_delete(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if statement.startswith("DELETE FROM mcp_oauth_pending"):
            raise RuntimeError("controlled pending-state delete failure")

    sa.event.listen(backend._engine, "before_cursor_execute", fail_pending_delete)
    try:
        with pytest.raises(RuntimeError, match="controlled pending-state delete failure"):
            backend.delete_mcp_oauth_rows_by_server_name("mcp-server")
    finally:
        sa.event.remove(backend._engine, "before_cursor_execute", fail_pending_delete)
    assert [backend.get_oauth_token(user, "mcp-server") for user in ("u1", "u2")] == rows

    assert backend.delete_mcp_oauth_rows_by_server_name("mcp-server") == 3
    assert backend.delete_mcp_oauth_rows_by_server_name("mcp-server") == 0
    for user in ("u1", "u2"):
        assert backend.get_oauth_token(user, "mcp-server") is None
        assert backend.get_oauth_token(user, "other-server") is not None
    assert backend.pop_mcp_oauth_pending_state("state-mcp-server") is None
    assert backend.pop_mcp_oauth_pending_state("state-other-server") is not None
    assert backend.list_mcp_pending_consent_by_user("u1") == pending_consent
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") == credential


def test_user_delete_removes_shared_tokens_and_credentials(backend: StorageBackend) -> None:
    keys = (
        "mcp-custodial",
        "mcp-obo",
        model_obo_cache_server("gateway"),
        "__model_obo__:api://legacy",
    )
    for user in ("u1", "u2"):
        backend.create_user(user, user, user, "hash")
        for key in keys:
            _seed_token(backend, user, key)
        backend.upsert_oidc_user_credential(
            user, "https://idp.example.com", refresh_token_ct=b"credential"
        )
    app_key = model_app_cache_server("gateway")
    _seed_token(backend, MODEL_APP_MINT_PRINCIPAL, app_key)
    other_rows = backend.list_oauth_token_metadata_by_user("u2")
    app_row = backend.get_oauth_token(MODEL_APP_MINT_PRINCIPAL, app_key)

    assert backend.delete_user("u1") is True

    assert backend.list_oauth_token_metadata_by_user("u1") == []
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") is None
    assert backend.list_oauth_token_metadata_by_user("u2") == other_rows
    assert backend.get_oidc_user_credential("u2", "https://idp.example.com") is not None
    assert backend.get_oauth_token(MODEL_APP_MINT_PRINCIPAL, app_key) == app_row


def test_mcp_counts_and_sweep_use_the_renamed_token_key(backend: StorageBackend) -> None:
    for key, auth_type in (("mcp-custodial", "oauth_user"), ("mcp-obo", "oauth_obo")):
        backend.create_mcp_server(
            key, key, "streamable-http", url="https://mcp.example.com", auth_type=auth_type
        )
    _seed_token(backend, "no-expiry", "mcp-custodial")
    _seed_token(backend, "fresh", "mcp-custodial", expires="2099-01-01T00:00:00")
    _seed_token(backend, "expired", "mcp-custodial", expires="2000-01-01T00:00:00")
    _seed_token(backend, "u1", "mcp-obo")
    _seed_token(backend, "u1", model_obo_cache_server("gateway"))
    _seed_token(backend, MODEL_APP_MINT_PRINCIPAL, model_app_cache_server("gateway"))

    assert backend.count_mcp_consented_users_by_server("mcp-custodial") == 2
    assert backend.count_mcp_consented_users_grouped_by_server()["mcp-custodial"] == 2
    targets = backend.list_mcp_user_token_reconcile_targets()
    assert {(user, key) for user, key, _ in targets} == {
        ("no-expiry", "mcp-custodial"),
        ("fresh", "mcp-custodial"),
        ("expired", "mcp-custodial"),
    }
    for user, key, exercised in targets:
        row = backend.get_oauth_token(user, key)
        assert row is not None and exercised == row["created"]


def _rows_under(backend: Any, key: str) -> tuple[bool, bool]:
    """Whether a token row and a pending OAuth flow keyed by *key* are present."""
    from turnstone.core.storage._schema import mcp_oauth_pending

    with backend._engine.connect() as conn:
        pending = conn.execute(
            sa.select(mcp_oauth_pending.c.state).where(mcp_oauth_pending.c.server_name == key)
        ).fetchone()
    return backend.get_oauth_token("u1", key) is not None, pending is not None


def _plant(backend: Any, *keys: str) -> None:
    for key in keys:
        _seed_token(backend, "u1", key)
        _seed_auth_state(backend, key)


def _create_server(backend: Any, name: str) -> str:
    server_id = f"id-{name}"
    assert backend.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url="https://mcp.example.com/mcp",
        auth_type="oauth_user",
    )
    return server_id


def _failing_on(prefix: str) -> Any:
    def listener(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if statement.startswith(prefix):
            raise RuntimeError(f"controlled failure on {prefix}")

    return listener


class TestServerWritesCarryTheirOAuthRows:
    """Creating, updating and deleting an MCP server purge the name-keyed OAuth rows in the same
    transaction as the change, so neither lands without the other."""

    def test_an_update_that_purges_takes_the_rows_with_it(self, backend: Any) -> None:
        server_id = _create_server(backend, "srv")
        _plant(backend, "srv", "other")
        pending_consent = backend.list_mcp_pending_consent_by_user("u1")

        assert backend.update_mcp_server(
            server_id, purge_oauth_rows=True, url="https://mcp2.example.com/mcp"
        )

        assert _rows_under(backend, "srv") == (False, False)
        assert _rows_under(backend, "other") == (True, True)
        assert backend.list_mcp_pending_consent_by_user("u1") == pending_consent

    def test_an_update_without_the_purge_keeps_the_rows(self, backend: Any) -> None:
        server_id = _create_server(backend, "srv")
        _plant(backend, "srv")

        assert backend.update_mcp_server(server_id, enabled=False)

        assert _rows_under(backend, "srv") == (True, True)

    def test_a_rename_purges_the_old_name_and_rows_left_under_the_new_one(
        self, backend: Any
    ) -> None:
        server_id = _create_server(backend, "old")
        _plant(backend, "old", "new", "other")

        assert backend.update_mcp_server(server_id, name="new")

        assert _rows_under(backend, "old") == (False, False)
        assert _rows_under(backend, "new") == (False, False)
        assert _rows_under(backend, "other") == (True, True)

    def test_a_rename_onto_a_taken_name_purges_nothing(self, backend: Any) -> None:
        from turnstone.core.storage._protocol import StorageConflictError

        first = _create_server(backend, "a")
        _create_server(backend, "b")
        _plant(backend, "a", "b")

        with pytest.raises(StorageConflictError):
            backend.update_mcp_server(first, purge_oauth_rows=True, name="b")

        assert _rows_under(backend, "a") == (True, True)
        assert _rows_under(backend, "b") == (True, True)
        row = backend.get_mcp_server(first)
        assert row is not None and row["name"] == "a"

    def test_a_purge_that_fails_takes_the_update_back(self, backend: Any) -> None:
        server_id = _create_server(backend, "srv")
        _plant(backend, "srv")
        listener = _failing_on("DELETE FROM mcp_oauth_pending")

        sa.event.listen(backend._engine, "before_cursor_execute", listener)
        try:
            with pytest.raises(RuntimeError, match="controlled failure"):
                backend.update_mcp_server(
                    server_id, purge_oauth_rows=True, url="https://mcp2.example.com/mcp"
                )
        finally:
            sa.event.remove(backend._engine, "before_cursor_execute", listener)

        row = backend.get_mcp_server(server_id)
        assert row is not None and row["url"] == "https://mcp.example.com/mcp"
        assert _rows_under(backend, "srv") == (True, True)

    def test_a_delete_takes_its_rows_with_it(self, backend: Any) -> None:
        server_id = _create_server(backend, "srv")
        _plant(backend, "srv", "other")

        assert backend.delete_mcp_server(server_id)

        assert backend.get_mcp_server(server_id) is None
        assert _rows_under(backend, "srv") == (False, False)
        assert _rows_under(backend, "other") == (True, True)

    def test_a_delete_that_fails_keeps_the_rows(self, backend: Any) -> None:
        server_id = _create_server(backend, "srv")
        _plant(backend, "srv")
        listener = _failing_on("DELETE FROM mcp_servers")

        sa.event.listen(backend._engine, "before_cursor_execute", listener)
        try:
            with pytest.raises(RuntimeError, match="controlled failure"):
                backend.delete_mcp_server(server_id)
        finally:
            sa.event.remove(backend._engine, "before_cursor_execute", listener)

        assert backend.get_mcp_server(server_id) is not None
        assert _rows_under(backend, "srv") == (True, True)

    def test_deleting_a_missing_server_changes_nothing(self, backend: Any) -> None:
        _plant(backend, "srv")

        assert backend.delete_mcp_server("id-missing") is False

        assert _rows_under(backend, "srv") == (True, True)

    def test_a_new_server_never_inherits_rows_left_under_its_name(self, backend: Any) -> None:
        _plant(backend, "srv", "other")

        _create_server(backend, "srv")

        assert _rows_under(backend, "srv") == (False, False)
        assert _rows_under(backend, "other") == (True, True)

    def test_a_create_that_finds_the_name_taken_purges_nothing(self, backend: Any) -> None:
        _create_server(backend, "srv")
        _plant(backend, "srv")

        assert not backend.create_mcp_server(
            server_id="id-second",
            name="srv",
            transport="streamable-http",
            url="https://mcp.example.com/mcp",
            auth_type="oauth_user",
        )

        assert _rows_under(backend, "srv") == (True, True)


class TestUnlinkOIDCIdentity:
    """Unlinking deletes the identity and the delegated access it carried, in one transaction."""

    ISSUER = "https://idp.example.com"

    def _seed(self, backend: Any) -> None:
        backend.create_oidc_identity(self.ISSUER, "sub-1", "u1", "u1@example.com")
        backend.upsert_oidc_user_credential("u1", self.ISSUER, refresh_token_ct=b"credential")
        backend.upsert_oidc_user_credential(
            "u1", "https://other-idp.example.com", refresh_token_ct=b"c2"
        )
        backend.create_mcp_server(
            server_id="id-obo",
            name="obo-srv",
            transport="streamable-http",
            url="https://mcp.example.com/obo",
            auth_type="oauth_obo",
        )
        _create_server(backend, "user-srv")
        for key in (
            "obo-srv",
            "user-srv",
            "__model_obo__:gateway",
            # An unescaped LIKE would read the prefix's underscores as wildcards and match this.
            "XXmodelYoboZZ:gateway",
        ):
            _seed_token(backend, "u1", key)
        _seed_token(backend, "u2", "obo-srv")
        _seed_token(backend, MODEL_APP_MINT_PRINCIPAL, model_app_cache_server("gateway"))

    def test_the_identity_goes_with_its_credential_and_delegated_tokens(self, backend: Any) -> None:
        self._seed(backend)

        result = backend.unlink_oidc_identity(
            self.ISSUER, "sub-1", model_token_prefix="__model_obo__:"
        )

        assert result == {
            "user_id": "u1",
            "credential_deleted": True,
            "delegated_token_rows_deleted": 2,
        }
        assert backend.get_oidc_identity(self.ISSUER, "sub-1") is None
        assert backend.get_oidc_user_credential("u1", self.ISSUER) is None
        assert backend.get_oidc_user_credential("u1", "https://other-idp.example.com") is not None
        assert backend.get_oauth_token("u1", "obo-srv") is None
        assert backend.get_oauth_token("u1", "__model_obo__:gateway") is None
        assert backend.get_oauth_token("u1", "user-srv") is not None
        assert backend.get_oauth_token("u1", "XXmodelYoboZZ:gateway") is not None
        assert backend.get_oauth_token("u2", "obo-srv") is not None
        assert (
            backend.get_oauth_token(MODEL_APP_MINT_PRINCIPAL, model_app_cache_server("gateway"))
            is not None
        )

    def test_a_missing_identity_changes_nothing(self, backend: Any) -> None:
        self._seed(backend)

        assert (
            backend.unlink_oidc_identity(self.ISSUER, "sub-x", model_token_prefix="__model_obo__:")
            is None
        )

        assert backend.get_oidc_user_credential("u1", self.ISSUER) is not None
        assert backend.get_oauth_token("u1", "obo-srv") is not None

    def test_a_failure_partway_changes_nothing(self, backend: Any) -> None:
        self._seed(backend)
        listener = _failing_on("DELETE FROM oidc_identities")

        sa.event.listen(backend._engine, "before_cursor_execute", listener)
        try:
            with pytest.raises(RuntimeError, match="controlled failure"):
                backend.unlink_oidc_identity(
                    self.ISSUER, "sub-1", model_token_prefix="__model_obo__:"
                )
        finally:
            sa.event.remove(backend._engine, "before_cursor_execute", listener)

        assert backend.get_oidc_identity(self.ISSUER, "sub-1") is not None
        assert backend.get_oidc_user_credential("u1", self.ISSUER) is not None
        assert backend.get_oauth_token("u1", "obo-srv") is not None
        assert backend.get_oauth_token("u1", "__model_obo__:gateway") is not None


class TestGuardedDelegatedWrites:
    """A delegated token and a sign-in's credential are written only while what they depend on
    still exists, checked in the same transaction as the write."""

    ISSUER = "https://idp.example.com"

    def _replace(self, backend: Any, access: bytes = b"new") -> str:
        result: str = backend.replace_delegated_oauth_token(
            "u1",
            "obo-srv",
            access_token_ct=access,
            expires_at="2099-01-01T00:00:00",
            scopes=None,
            as_issuer=self.ISSUER,
            audience="api://new",
        )
        return result

    def test_a_token_replaces_the_row_while_the_credential_exists(self, backend: Any) -> None:
        backend.upsert_oidc_user_credential("u1", self.ISSUER, refresh_token_ct=b"credential")
        _seed_token(backend, "u1", "obo-srv")

        assert self._replace(backend) == "stored"

        row = backend.get_oauth_token("u1", "obo-srv")
        assert row is not None
        assert row["audience"] == "api://new"
        assert row["refresh_token_ct"] is None

    def test_a_token_minted_across_an_unlink_is_never_written(self, backend: Any) -> None:
        backend.create_oidc_identity(self.ISSUER, "sub-1", "u1", "u1@example.com")
        backend.upsert_oidc_user_credential("u1", self.ISSUER, refresh_token_ct=b"credential")
        # The mint read the credential; the unlink commits; then the mint persists.
        backend.unlink_oidc_identity(self.ISSUER, "sub-1", model_token_prefix="__model_obo__:")

        assert self._replace(backend) == "credential_gone"

        assert backend.get_oauth_token("u1", "obo-srv") is None

    def test_a_token_for_a_server_no_longer_passthrough_is_never_written(
        self, backend: Any
    ) -> None:
        backend.upsert_oidc_user_credential("u1", self.ISSUER, refresh_token_ct=b"credential")
        backend.create_mcp_server(
            server_id="s-obo",
            name="obo-srv",
            transport="streamable-http",
            url="https://mcp.example.com/obo",
            auth_type="oauth_user",
        )

        result = backend.replace_delegated_oauth_token(
            "u1",
            "obo-srv",
            access_token_ct=b"new",
            expires_at="2099-01-01T00:00:00",
            scopes=None,
            as_issuer=self.ISSUER,
            audience="api://new",
            obo_server="obo-srv",
        )

        assert result == "binding_changed"
        assert backend.get_oauth_token("u1", "obo-srv") is None

    def test_a_credential_is_captured_only_for_a_live_identity(self, backend: Any) -> None:
        assert not backend.upsert_oidc_user_credential_for_identity(
            "u1", self.ISSUER, "sub-1", refresh_token_ct=b"rt"
        )
        assert backend.get_oidc_user_credential("u1", self.ISSUER) is None

        backend.create_oidc_identity(self.ISSUER, "sub-1", "u2", "u2@example.com")
        assert not backend.upsert_oidc_user_credential_for_identity(
            "u1", self.ISSUER, "sub-1", refresh_token_ct=b"rt"
        )
        assert backend.get_oidc_user_credential("u1", self.ISSUER) is None

        assert backend.upsert_oidc_user_credential_for_identity(
            "u2", self.ISSUER, "sub-1", refresh_token_ct=b"rt"
        )
        credential = backend.get_oidc_user_credential("u2", self.ISSUER)
        assert credential is not None and credential["refresh_token_ct"] == b"rt"


def test_the_freshness_sweep_skips_a_disabled_servers_grants(backend: Any) -> None:
    for server_id, name in (("s-on", "on-srv"), ("s-off", "off-srv")):
        backend.create_mcp_server(
            server_id=server_id,
            name=name,
            transport="streamable-http",
            url=f"https://{name}.example.com/mcp",
            auth_type="oauth_user",
        )
        _seed_token(backend, "u1", name)
    backend.update_mcp_server("s-off", enabled=False)

    targets = backend.list_mcp_user_token_reconcile_targets()

    assert [(user, server) for user, server, _ in targets] == [("u1", "on-srv")]


def test_a_server_update_applies_only_while_the_row_holds_what_was_read(backend: Any) -> None:
    backend.create_mcp_server(
        server_id="s1",
        name="srv",
        transport="streamable-http",
        url="https://a.example.com/mcp",
        auth_type="oauth_user",
    )

    assert not backend.update_mcp_server(
        "s1", expected={"url": "https://other.example.com/mcp"}, enabled=False
    )
    assert backend.get_mcp_server("s1")["enabled"] is True

    assert backend.update_mcp_server(
        "s1", expected={"url": "https://a.example.com/mcp", "oauth_audience": None}, enabled=False
    )
    assert backend.get_mcp_server("s1")["enabled"] is False


def test_a_server_update_writes_or_clears_the_client_secret_with_the_change(
    backend: Any,
) -> None:
    backend.create_mcp_server(
        server_id="s1",
        name="srv",
        transport="streamable-http",
        url="https://a.example.com/mcp",
        auth_type="oauth_user",
        oauth_client_secret_ct=b"first",
    )

    assert backend.update_mcp_server("s1", enabled=False)
    assert backend.get_mcp_oauth_client_secret_ct("s1") == b"first"
    assert backend.update_mcp_server("s1", oauth_client_secret_ct=b"second")
    assert backend.get_mcp_oauth_client_secret_ct("s1") == b"second"
    assert backend.update_mcp_server("s1", oauth_client_secret_ct=None)
    assert backend.get_mcp_oauth_client_secret_ct("s1") is None
