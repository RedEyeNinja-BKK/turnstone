# SPDX-License-Identifier: Apache-2.0
"""Coordinated OAuth token lifecycle: locking, atomicity, rotation, identity.

The claim under test is the one that matters operationally: **when several
processes share one rotating refresh token, exactly one refresh is issued per
rotation and the others reuse its result.**  That is proven here with real
child processes against a real local HTTP endpoint, not with a mock.

Also covers the operator-mandated lifecycle corrections: the provider's
``earliest_refresh_at`` floor, failure classification, scope and identity
validation on renewal, the clock re-read under the lock, and the distributed
rotation window that atomic persistence cannot close.

All temp roots use /var/tmp: /tmp on this host is a small tmpfs.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_oauth_token_store.py
"""
from __future__ import annotations

import http.server
import json
import multiprocessing
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core.oauth_token_store import (  # noqa: E402
    OAuthClientConfigError,
    OAuthIdentityChangedError,
    OAuthNotAuthorizedError,
    OAuthPolicyError,
    OAuthTokenError,
    OAuthTokenStore,
    OAuthTransientError,
    _parse_timestamp,
)

SECRET_REFRESH = "rt-SECRET-DO-NOT-LEAK-0123456789"
SECRET_ACCESS = "at-SECRET-DO-NOT-LEAK-9876543210"
_TMP = "/var/tmp"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _seed(root: pathlib.Path, *, expires_in: int = 3600, **overrides: Any) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    creds: dict[str, Any] = {
        "client_id": "oaiapp_TEST",
        "access_token": "at-OLD",
        "refresh_token": SECRET_REFRESH,
        "expires_at": int(time.time()) + expires_in,
        "granted_scopes": "openid profile offline_access resource.invoke chatgpt.tokens.use.direct",
        "sub": "sub-AAA",
        "email": "operator@example.invalid",
        "resource": "https://api.openai.com/v1",
    }
    creds.update(overrides)
    (root / "credentials.json").write_text(json.dumps(creds), encoding="utf-8")
    os.chmod(root / "credentials.json", 0o600)
    return creds


def _ok_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "access_token": SECRET_ACCESS,
        "refresh_token": "rt-ROTATED-1",
        "expires_in": 3600,
        "scope": "openid profile offline_access resource.invoke chatgpt.tokens.use.direct",
    }
    payload.update(overrides)
    return payload


class _CountingPost:
    """Records every refresh request; returns a rotating token."""

    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.status = status
        self.payload = payload

    def __call__(self, url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls.append(dict(form))
        if self.payload is not None:
            return self.status, dict(self.payload)
        return self.status, _ok_payload()


def _stored(root: pathlib.Path) -> dict[str, Any]:
    parsed = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError("stored credential is not an object")
    return parsed


# ---------------------------------------------------------------------------
# baseline behaviour
# ---------------------------------------------------------------------------


def test_fresh_token_is_used_without_refresh() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root)
        post = _CountingPost()
        assert OAuthTokenStore(root, post_form=post).access_token() == "at-OLD"
        assert post.calls == []


def test_expired_token_triggers_refresh_and_persists() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-10)
        post = _CountingPost()
        assert OAuthTokenStore(root, post_form=post).access_token() == SECRET_ACCESS
        assert len(post.calls) == 1
        stored = _stored(root)
        # The ROTATED refresh token is the one persisted: keeping the old one
        # would leave the cluster holding a token the provider has retired.
        assert stored["refresh_token"] == "rt-ROTATED-1"
        assert stored["access_token"] == SECRET_ACCESS


def test_renewal_margin_triggers_early_refresh() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=120)  # valid, but inside the 300 s margin
        post = _CountingPost()
        OAuthTokenStore(root, post_form=post).access_token()
        assert len(post.calls) == 1


def test_refresh_request_carries_documented_grant() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost()
        OAuthTokenStore(root, post_form=post).access_token()
        form = post.calls[0]
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == "oaiapp_TEST"
        assert form["resource"] == "https://api.openai.com/v1"
        assert form["refresh_token"] == SECRET_REFRESH


# ---------------------------------------------------------------------------
# (1) earliest_refresh_at floor
# ---------------------------------------------------------------------------


def test_timestamp_parser_accepts_epoch_and_iso() -> None:
    assert _parse_timestamp(1770000000) == 1770000000
    assert _parse_timestamp("1770000000") == 1770000000
    assert _parse_timestamp("2026-10-17T12:00:00Z") == 1792238400
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("not-a-date") is None


def test_refresh_deferred_while_earliest_refresh_at_is_future() -> None:
    """Inside the margin but before the floor: keep serving the valid token."""
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=200, earliest_refresh_at=int(time.time()) + 120)
        post = _CountingPost()
        assert OAuthTokenStore(root, post_form=post).access_token() == "at-OLD"
        assert post.calls == [], "must not ask the provider before earliest_refresh_at"


def test_expired_token_before_floor_is_a_policy_error() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-5, earliest_refresh_at=int(time.time()) + 60)
        post = _CountingPost()
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthPolicyError:
            pass
        else:
            raise AssertionError("an expired token before the floor cannot be served")
        assert post.calls == []


def test_earliest_refresh_at_is_persisted_from_the_response() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        future = int(time.time()) + 900
        post = _CountingPost(payload=_ok_payload(earliest_refresh_at=future))
        OAuthTokenStore(root, post_form=post).access_token()
        assert _stored(root)["earliest_refresh_at"] == future


# ---------------------------------------------------------------------------
# (2) failure classification
# ---------------------------------------------------------------------------


def test_transient_5xx_is_retryable_and_does_not_park() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=503, payload={})
        store = OAuthTokenStore(root, post_form=post)
        try:
            store.access_token()
        except OAuthTransientError:
            pass
        else:
            raise AssertionError("a 503 must be retryable, not success")
        assert _stored(root).get("state") != "needs_reauth"


def test_network_failure_is_transient_and_never_echoes_details() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)

        def boom(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
            raise OAuthTransientError("token endpoint unreachable (URLError)")

        try:
            OAuthTokenStore(root, post_form=boom).access_token()
        except OAuthTransientError as exc:
            assert SECRET_REFRESH not in str(exc)
        else:
            raise AssertionError("expected a transient failure")


def test_invalid_client_is_a_config_error_not_a_revocation() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=400, payload={"error": "invalid_client"})
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthClientConfigError:
            pass
        else:
            raise AssertionError("invalid_client must be a configuration error")
        assert _stored(root).get("state") != "needs_reauth", (
            "a client misconfiguration must not force the operator to re-authorize"
        )


def test_insufficient_scope_is_a_policy_error() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=403, payload={"error": "insufficient_scope"})
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthPolicyError:
            pass
        else:
            raise AssertionError("insufficient_scope must be a policy error")


# ---------------------------------------------------------------------------
# (3) required direct-use permission survives renewal
# ---------------------------------------------------------------------------


def test_renewal_dropping_direct_use_scope_is_rejected() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(payload=_ok_payload(scope="openid profile offline_access"))
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthPolicyError as exc:
            assert "chatgpt.tokens.use.direct" in str(exc)
        else:
            raise AssertionError("a renewal that drops direct-use must not be accepted")


def test_renewal_keeping_required_scopes_is_accepted() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost()
        assert OAuthTokenStore(root, post_form=post).access_token() == SECRET_ACCESS


# ---------------------------------------------------------------------------
# (4) identity binding
# ---------------------------------------------------------------------------


def test_account_switch_is_rejected() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(payload=_ok_payload(sub="sub-DIFFERENT"))
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthIdentityChangedError:
            pass
        else:
            raise AssertionError("an account switch must never be absorbed silently")


def test_client_switch_is_rejected() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(payload=_ok_payload(client_id="oaiapp_OTHER"))
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthIdentityChangedError:
            pass
        else:
            raise AssertionError("a client switch must never be absorbed silently")


# ---------------------------------------------------------------------------
# (5) clock is re-read after acquiring the lock
# ---------------------------------------------------------------------------


def test_clock_is_rechecked_after_acquiring_the_lock() -> None:
    """A stale ``now`` must not force a refresh of a token that is still good.

    The caller passes a two-hour-old timestamp, which makes the fast path think
    the token is expired. Under the lock the module re-reads the real clock and
    discovers the token is fine, so no refresh is issued.
    """
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=3600)
        post = _CountingPost()
        store = OAuthTokenStore(root, post_form=post)
        assert store.access_token(now=time.time() - 7200) == "at-OLD"
        assert post.calls == [], "the clock must be re-read under the lock"


# ---------------------------------------------------------------------------
# (6) the distributed rotation window atomic persistence cannot close
# ---------------------------------------------------------------------------


class _RotatingProvider:
    """A provider that retires the old refresh token as soon as it rotates."""

    def __init__(self) -> None:
        self.valid = SECRET_REFRESH
        self.calls = 0

    def __call__(self, url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls += 1
        if form["refresh_token"] != self.valid:
            return 400, {"error": "invalid_grant"}
        self.valid = f"rt-ROTATED-{self.calls}"
        return 200, _ok_payload(refresh_token=self.valid)


def test_process_death_after_provider_rotation_requires_reauthorization() -> None:
    """Documents a REAL limitation: local atomicity cannot undo a remote rotation.

    The provider rotates, then the process dies before the new token is
    committed. The stored token is now stale everywhere on this host, so the
    next refresh is refused with invalid_grant and the store is parked. The
    operator must re-authorize; the test asserts that this happens loudly rather
    than silently serving a dead credential.
    """
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        provider = _RotatingProvider()

        class _ProcessDied(BaseException):
            pass

        def dies_after_rotation(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
            status, payload = provider(url, form)  # provider rotates here
            raise _ProcessDied("simulated crash before commit")

        try:
            OAuthTokenStore(root, post_form=dies_after_rotation).access_token()
        except _ProcessDied:
            pass
        else:
            raise AssertionError("the simulated crash must propagate")

        # Nothing was committed: the store still holds the retired token.
        assert _stored(root)["refresh_token"] == SECRET_REFRESH
        assert provider.valid != SECRET_REFRESH, "the provider did rotate"

        store = OAuthTokenStore(root, post_form=provider)
        try:
            store.access_token()
        except OAuthNotAuthorizedError:
            pass
        else:
            raise AssertionError("a stale token must not be silently served")
        assert _stored(root)["state"] == "needs_reauth"
        assert provider.calls == 2, "one rotation plus one refused attempt"
        # Once parked, the cluster must not keep hammering the provider.
        try:
            store.access_token()
        except OAuthNotAuthorizedError:
            pass
        else:
            raise AssertionError("parked store must not hand out a token")
        assert provider.calls == 2, "parked store must not be retried in a loop"


# ---------------------------------------------------------------------------
# (7) revocation, parking, atomicity, privacy
# ---------------------------------------------------------------------------


def test_invalid_grant_parks_the_store() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=400, payload={"error": "invalid_grant"})
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthNotAuthorizedError:
            pass
        else:
            raise AssertionError("invalid_grant must raise OAuthNotAuthorizedError")
        assert _stored(root)["state"] == "needs_reauth"


def test_parked_store_fails_fast_without_calling_the_provider() -> None:
    """A revoked grant must not become a retry storm across the cluster."""
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1, state="needs_reauth")
        post = _CountingPost()
        store = OAuthTokenStore(root, post_form=post)
        for _ in range(5):
            try:
                store.access_token()
            except OAuthNotAuthorizedError:
                continue
            raise AssertionError("parked store must not hand out a token")
        assert post.calls == [], "parked store must not contact the provider"


def test_atomic_write_is_private_and_leaves_no_temp_file() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        OAuthTokenStore(root, post_form=_CountingPost()).access_token()
        path = root / "credentials.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        leftovers = [p.name for p in root.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"atomic write left temp files: {leftovers}"
        json.loads(path.read_text(encoding="utf-8"))  # always a complete document


def test_store_creates_a_private_root() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "newstore"
        _seed(root, expires_in=-1)
        store = OAuthTokenStore(root, post_form=_CountingPost())
        store.access_token()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_store_refuses_to_overlap_codex_credentials() -> None:
    try:
        OAuthTokenStore(pathlib.Path.home() / ".codex" / "turnstone-oauth")
    except OAuthTokenError:
        return
    raise AssertionError("store must refuse to live inside the Codex credential directory")


def test_status_is_non_secret() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root)
        summary = OAuthTokenStore(root).status()
        blob = json.dumps(summary)
        assert SECRET_REFRESH not in blob
        assert "at-OLD" not in blob
        assert summary["account"] == "operator@example.invalid"
        assert summary["refresh_token_present"] is True
        assert "seconds_until_refresh_allowed" in summary


def test_errors_never_contain_credential_material() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=400, payload={"error": "invalid_grant"})
        try:
            OAuthTokenStore(root, post_form=post).access_token()
        except OAuthTokenError as exc:
            assert SECRET_REFRESH not in str(exc)
            assert SECRET_REFRESH not in repr(exc)
        else:
            raise AssertionError("expected a terminal failure")


def test_credentials_file_is_never_world_readable_after_refresh() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        os.chmod(root / "credentials.json", 0o644)  # too open on entry
        OAuthTokenStore(root, post_form=_CountingPost()).access_token()
        assert stat.S_IMODE((root / "credentials.json").stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# the coordination proof: real processes, real HTTP, exactly one refresh
# ---------------------------------------------------------------------------


class _TokenHandler(http.server.BaseHTTPRequestHandler):
    refreshes = 0
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        with _TokenHandler.lock:
            _TokenHandler.refreshes += 1
            serial = _TokenHandler.refreshes
        # A slow response widens the race window the lock must close.
        time.sleep(0.4)
        body = json.dumps({
            "access_token": f"at-{serial}",
            "refresh_token": f"rt-rotated-{serial}",
            "expires_in": 3600,
            "scope": "openid profile offline_access resource.invoke chatgpt.tokens.use.direct",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence the test log
        return


_WORKERS = 6


def _worker(root: str, endpoint: str, queue: Any) -> None:
    try:
        queue.put(("ok", OAuthTokenStore(root, token_endpoint=endpoint).access_token()))
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_processes_issue_exactly_one_refresh() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)  # every worker will want to refresh
        _TokenHandler.refreshes = 0

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ctx = multiprocessing.get_context("fork")
        queue: Any = ctx.Queue()
        procs = [
            ctx.Process(target=_worker, args=(str(root), f"http://127.0.0.1:{port}/token", queue))
            for _ in range(_WORKERS)
        ]
        try:
            for proc in procs:
                proc.start()
            results = [queue.get(timeout=60) for _ in procs]
            for proc in procs:
                proc.join(timeout=60)
        finally:
            server.shutdown()
            server.server_close()

        errors = [r for r in results if r[0] == "err"]
        assert not errors, f"workers failed: {errors}"
        assert _TokenHandler.refreshes == 1, (
            f"{_WORKERS} concurrent workers issued {_TokenHandler.refreshes} refreshes; "
            "the lock + re-read must collapse this to exactly 1"
        )
        tokens = {r[1] for r in results}
        assert tokens == {"at-1"}, f"workers did not all reuse the winner's token: {tokens}"


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
