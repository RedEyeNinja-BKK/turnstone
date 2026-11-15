"""Storage CRUD tests for the per-(user, server) MCP OAuth pending-state table.

Validates the storage-protocol additions for the per-(user, server)
OAuth flow:

- ``create_mcp_oauth_pending_state``
- ``pop_mcp_oauth_pending_state`` (atomic, with TTL)
- ``cleanup_expired_mcp_oauth_pending_states``
- ``get_mcp_oauth_client_secret_ct``
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import sqlalchemy as sa


def _create(
    backend: Any, state: str, user_id: str = "u", server_name: str = "s", **binding: Any
) -> None:
    """Insert a pending row; *binding* overrides the recorded issuer and server fields."""
    fields: dict[str, Any] = {
        "as_issuer": "https://as.example.com",
        "iss_parameter_supported": False,
        "server_id": "srv-id",
        "server_url": "https://mcp.example.com/sse",
        "audience": "https://mcp.example.com",
    }
    fields.update(binding)
    backend.create_mcp_oauth_pending_state(state, user_id, server_name, "v", "/r", **fields)


class TestCreateAndPop:
    def test_round_trip(self, backend) -> None:
        backend.create_mcp_oauth_pending_state(
            "state-1",
            "user-a",
            "srv-x",
            "verifier-blob",
            "/admin/mcp-servers",
            as_issuer="https://as.example.com",
            iss_parameter_supported=False,
            server_id="srv-id",
            server_url="https://mcp.example.com/sse",
            audience="https://mcp.example.com",
        )
        row = backend.pop_mcp_oauth_pending_state("state-1", max_age_seconds=600)
        assert row is not None
        assert row["state"] == "state-1"
        assert row["user_id"] == "user-a"
        assert row["server_name"] == "srv-x"
        assert row["code_verifier"] == "verifier-blob"
        assert row["return_url"] == "/admin/mcp-servers"

    def test_the_flow_binding_round_trips_with_its_types(self, backend) -> None:
        """The pop returns what /start recorded, typed: a stringified flag or a missing value
        would read as true."""
        _create(
            backend,
            "promised",
            as_issuer="https://as.example.com/t1",
            iss_parameter_supported=True,
            server_id="srv-id-9",
            server_url="https://mcp.example.com/mcp",
            audience="api://mcp",
        )
        _create(backend, "unpromised")

        promised = backend.pop_mcp_oauth_pending_state("promised")
        unpromised = backend.pop_mcp_oauth_pending_state("unpromised")

        assert promised is not None and unpromised is not None
        assert promised["as_issuer"] == "https://as.example.com/t1"
        assert promised["iss_parameter_supported"] is True
        assert promised["server_id"] == "srv-id-9"
        assert promised["server_url"] == "https://mcp.example.com/mcp"
        assert promised["audience"] == "api://mcp"
        assert unpromised["iss_parameter_supported"] is False

    def test_pop_consumes_row(self, backend) -> None:
        _create(backend, "s2")
        first = backend.pop_mcp_oauth_pending_state("s2")
        assert first is not None
        # Second pop must miss — row was consumed.
        second = backend.pop_mcp_oauth_pending_state("s2")
        assert second is None

    def test_pop_missing_returns_none(self, backend) -> None:
        assert backend.pop_mcp_oauth_pending_state("never-existed") is None


class TestTTL:
    def test_pop_rejects_expired_row(self, backend) -> None:
        _create(backend, "old-state")
        # Backdate it so it's older than the TTL window.
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text(
                    "UPDATE mcp_oauth_pending SET created_at = '2020-01-01T00:00:00' "
                    "WHERE state = 'old-state'"
                )
            )
            conn.commit()

        # Default TTL is 600s — the row is decades old.
        row = backend.pop_mcp_oauth_pending_state("old-state")
        assert row is None

        # Even though pop returned None, the row must have been wiped — a
        # second pop with a giant TTL must still see nothing.
        again = backend.pop_mcp_oauth_pending_state("old-state", max_age_seconds=10**9)
        assert again is None

    def test_pop_accepts_fresh_row(self, backend) -> None:
        _create(backend, "fresh")
        row = backend.pop_mcp_oauth_pending_state("fresh", max_age_seconds=600)
        assert row is not None
        assert row["state"] == "fresh"


class TestCleanup:
    def test_cleanup_deletes_only_expired(self, backend) -> None:
        _create(backend, "old")
        _create(backend, "new")
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text(
                    "UPDATE mcp_oauth_pending SET created_at = '2020-01-01T00:00:00' "
                    "WHERE state = 'old'"
                )
            )
            conn.commit()

        deleted = backend.cleanup_expired_mcp_oauth_pending_states(max_age_seconds=600)
        assert deleted == 1
        # Old gone, new still around.
        assert backend.pop_mcp_oauth_pending_state("old") is None
        survivor = backend.pop_mcp_oauth_pending_state("new")
        assert survivor is not None

    def test_cleanup_no_rows(self, backend) -> None:
        assert backend.cleanup_expired_mcp_oauth_pending_states() == 0


class TestGetOAuthClientSecretCt:
    def test_returns_none_when_unset(self, backend) -> None:
        backend.create_mcp_server(
            server_id="srv-id",
            name="srv-x",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
        )
        assert backend.get_mcp_oauth_client_secret_ct("srv-id") is None

    def test_returns_ciphertext_after_set(self, backend) -> None:
        backend.create_mcp_server(
            server_id="srv-id",
            name="srv-x",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
        )
        ct = b"\x00\xff\x42encrypted-blob"
        ok = backend.set_mcp_oauth_client_secret_ct("srv-id", ct)
        assert ok is True
        out = backend.get_mcp_oauth_client_secret_ct("srv-id")
        assert out == ct

    def test_returns_none_for_missing_server(self, backend) -> None:
        assert backend.get_mcp_oauth_client_secret_ct("does-not-exist") is None


def _create_user_token_row(
    backend,
    *,
    user_id: str,
    token_key: str,
    created: str,
) -> None:
    """Insert a token row + backdate ``created`` so ordering is deterministic.

    The storage helper stamps ``created`` from ``datetime.now(UTC)``; for
    multi-row ordering tests we backdate via raw SQL so the inserts stay
    independent of clock resolution.
    """
    backend.create_oauth_token(
        user_id,
        token_key,
        access_token_ct=b"ct-access",
        refresh_token_ct=b"ct-refresh",
        expires_at="2026-05-04T12:00:00",
        scopes="openid",
        as_issuer="https://auth.example.com",
        audience="https://mcp.example.com",
    )
    with backend._engine.connect() as conn:
        conn.execute(
            sa.text(
                "UPDATE oauth_tokens SET created = :created "
                "WHERE user_id = :uid AND token_key = :sn"
            ),
            {"created": created, "uid": user_id, "sn": token_key},
        )
        conn.commit()


class TestListOAuthTokenMetadataByUser:
    def test_list_oauth_token_metadata_by_user_empty(self, backend) -> None:
        assert backend.list_oauth_token_metadata_by_user("nobody") == []

    def test_list_oauth_token_metadata_by_user_single_server(self, backend) -> None:
        _create_user_token_row(
            backend, user_id="u1", token_key="srv-a", created="2026-05-01T00:00:00"
        )
        rows = backend.list_oauth_token_metadata_by_user("u1")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "u1"
        assert rows[0]["token_key"] == "srv-a"
        assert rows[0]["as_issuer"] == "https://auth.example.com"
        assert rows[0]["audience"] == "https://mcp.example.com"
        assert rows[0]["scopes"] == "openid"
        # Projection MUST omit ciphertext columns — the SQL no longer
        # selects them, so the TypedDict has no key.
        assert "access_token_ct" not in rows[0]
        assert "refresh_token_ct" not in rows[0]

    def test_list_oauth_token_metadata_by_user_multiple_servers(self, backend) -> None:
        _create_user_token_row(
            backend, user_id="u1", token_key="srv-c", created="2026-05-03T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="u1", token_key="srv-a", created="2026-05-01T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="u1", token_key="srv-b", created="2026-05-02T00:00:00"
        )
        rows = backend.list_oauth_token_metadata_by_user("u1")
        assert [r["token_key"] for r in rows] == ["srv-a", "srv-b", "srv-c"]

    def test_list_oauth_token_metadata_by_user_isolates_by_user(self, backend) -> None:
        _create_user_token_row(
            backend, user_id="user-a", token_key="srv-a", created="2026-05-01T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="user-a", token_key="srv-b", created="2026-05-02T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="user-b", token_key="srv-a", created="2026-05-03T00:00:00"
        )
        rows_a = backend.list_oauth_token_metadata_by_user("user-a")
        assert {r["token_key"] for r in rows_a} == {"srv-a", "srv-b"}
        assert all(r["user_id"] == "user-a" for r in rows_a)

        rows_b = backend.list_oauth_token_metadata_by_user("user-b")
        assert len(rows_b) == 1
        assert rows_b[0]["user_id"] == "user-b"
        assert rows_b[0]["token_key"] == "srv-a"


_BINDING = ("server_id", "auth_type", "url", "oauth_authorization_server_url", "oauth_audience")


class TestConsentedGrantWrite:
    """A consent's grant is stored only while the server under the name is the one the consent
    was exchanged for, checked in the same transaction as the write."""

    SERVER = "srv-a"

    def _server(self, backend: Any, server_id: str = "id-a") -> dict[str, Any]:
        assert backend.create_mcp_server(
            server_id=server_id,
            name=self.SERVER,
            transport="streamable-http",
            url="https://mcp.example.com/mcp",
            auth_type="oauth_user",
        )
        row: dict[str, Any] | None = backend.get_mcp_server_by_name(self.SERVER)
        assert row is not None
        return row

    def _replace(
        self,
        backend: Any,
        expected: dict[str, Any],
        *,
        access: bytes = b"new",
        pins: tuple[str | None, ...] = (None,),
    ) -> bool:
        stored: bool = backend.replace_consented_oauth_token(
            "user-a",
            self.SERVER,
            access_token_ct=access,
            refresh_token_ct=b"refresh",
            expires_at="2099-01-01T00:00:00",
            scopes="openid",
            as_issuer="https://as.example.com",
            audience="https://mcp.example.com/mcp",
            expected_server=expected,
            pins=pins,
        )
        return stored

    def test_a_later_consent_replaces_the_grant_while_the_server_is_unchanged(
        self, backend: Any
    ) -> None:
        row = self._server(backend)
        expected = {column: row.get(column) for column in _BINDING}
        _create_user_token_row(
            backend, user_id="user-a", token_key=self.SERVER, created="2026-01-01T00:00:00"
        )

        assert self._replace(backend, expected, access=b"first")
        assert self._replace(backend, expected, access=b"second")

        token = backend.get_oauth_token("user-a", self.SERVER)
        assert token is not None
        assert (token["access_token_ct"], token["refresh_token_ct"]) == (b"second", b"refresh")
        assert token["created"] != "2026-01-01T00:00:00"

    @pytest.mark.parametrize(
        "change",
        ["url", "authorization-server", "audience", "auth-type", "pin", "deleted", "recreated"],
    )
    def test_a_server_changed_since_the_check_gets_no_grant(
        self, backend: Any, change: str
    ) -> None:
        row = self._server(backend)
        expected = {column: row.get(column) for column in _BINDING}
        if change == "url":
            backend.update_mcp_server("id-a", url="https://mcp2.example.com/mcp")
        elif change == "authorization-server":
            backend.update_mcp_server(
                "id-a", oauth_authorization_server_url="https://other-as.example.com"
            )
        elif change == "audience":
            backend.update_mcp_server("id-a", oauth_audience="api://other")
        elif change == "auth-type":
            backend.update_mcp_server("id-a", auth_type="oauth_obo")
        elif change == "pin":
            backend.update_mcp_server("id-a", oauth_as_issuer_cached="https://other-as.example.com")
        else:
            assert backend.delete_mcp_server("id-a")
            if change == "recreated":
                self._server(backend, server_id="id-b")

        assert not self._replace(backend, expected)
        assert backend.get_oauth_token("user-a", self.SERVER) is None

    def test_the_issuer_this_consent_pinned_is_accepted(self, backend: Any) -> None:
        row = self._server(backend)
        expected = {column: row.get(column) for column in _BINDING}
        backend.update_mcp_server("id-a", oauth_as_issuer_cached="https://as.example.com")

        assert not self._replace(backend, expected, pins=(None,))
        assert self._replace(backend, expected, pins=(None, "https://as.example.com"))

    def test_a_boolean_matches_its_stored_flag(self, backend: Any) -> None:
        row = self._server(backend)
        assert row["enabled"] is True
        expected = {"server_id": "id-a", "enabled": True}

        assert self._replace(backend, expected)
        backend.update_mcp_server("id-a", enabled=False)
        assert not self._replace(backend, expected)

    def test_a_consent_waits_out_a_server_change_in_flight(self, backend: Any) -> None:
        """The console's update holds the server row until it commits, its purge already run, so
        the consent waits for it and then finds the server changed, on either backend."""
        from turnstone.core.storage._schema import mcp_servers, oauth_tokens

        row = self._server(backend)
        expected = {column: row.get(column) for column in _BINDING}
        result: dict[str, bool] = {}
        writer = threading.Thread(
            target=lambda: result.update(stored=self._replace(backend, expected))
        )
        with backend._engine.connect() as change:
            if backend._engine.dialect.name == "sqlite":
                change.execute(sa.text("BEGIN IMMEDIATE"))
            else:
                change.execute(
                    sa.select(mcp_servers.c.server_id)
                    .where(mcp_servers.c.server_id == "id-a")
                    .with_for_update()
                )
            change.execute(
                sa.update(mcp_servers)
                .where(mcp_servers.c.server_id == "id-a")
                .values(url="https://mcp2.example.com/mcp")
            )
            change.execute(sa.delete(oauth_tokens).where(oauth_tokens.c.token_key == self.SERVER))
            writer.start()
            writer.join(0.5)
            assert writer.is_alive()
            change.commit()
        writer.join(10)

        assert result == {"stored": False}
        assert backend.get_oauth_token("user-a", self.SERVER) is None
