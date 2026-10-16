# SPDX-License-Identifier: Apache-2.0
"""Coordinated OAuth token lifecycle: locking, atomicity, rotation, identity.

The claim under test is the one that matters operationally: **when several
processes share one rotating refresh token, exactly one refresh is issued per
rotation and the others reuse its result.**  That is proven here with real
child processes against a real local HTTP endpoint, not with a mock.

Run standalone::

    /opt/turnstone-venv/bin/python tests/test_oauth_token_store.py
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
    OAuthIdentityChangedError,
    OAuthNotAuthorizedError,
    OAuthTokenError,
    OAuthTokenStore,
)

SECRET_REFRESH = "rt-SECRET-DO-NOT-LEAK-0123456789"
SECRET_ACCESS = "at-SECRET-DO-NOT-LEAK-9876543210"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _seed(root: pathlib.Path, *, expires_in: int = 3600, **overrides: Any) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    creds: dict[str, Any] = {
        "client_id": "oaiapp_TEST",
        "access_token": "at-OLD",
        "refresh_token": SECRET_REFRESH,
        "expires_at": int(time.time()) + expires_in,
        "granted_scopes": "openid profile offline_access resource.invoke",
        "sub": "sub-AAA",
        "email": "operator@example.invalid",
        "resource": "https://api.openai.com/v1",
    }
    creds.update(overrides)
    (root / "credentials.json").write_text(json.dumps(creds), encoding="utf-8")
    os.chmod(root / "credentials.json", 0o600)
    return creds


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
        return self.status, {
            "access_token": SECRET_ACCESS,
            "refresh_token": "rt-ROTATED-1",
            "expires_in": 3600,
            "scope": "openid profile offline_access resource.invoke",
        }


def test_fresh_token_is_used_without_refresh() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root)
        post = _CountingPost()
        store = OAuthTokenStore(root, post_form=post)
        assert store.access_token() == "at-OLD"
        assert post.calls == []


def test_expired_token_triggers_refresh_and_persists() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-10)
        post = _CountingPost()
        store = OAuthTokenStore(root, post_form=post)
        assert store.access_token() == SECRET_ACCESS
        assert len(post.calls) == 1
        # The ROTATED refresh token is the one persisted: keeping the old one
        # would leave the cluster holding a token the provider has retired.
        stored = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
        assert stored["refresh_token"] == "rt-ROTATED-1"
        assert stored["access_token"] == SECRET_ACCESS


def test_renewal_margin_triggers_early_refresh() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=120)  # valid, but inside the 300 s margin
        post = _CountingPost()
        store = OAuthTokenStore(root, post_form=post)
        store.access_token()
        assert len(post.calls) == 1


def test_refresh_request_carries_documented_grant() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost()
        OAuthTokenStore(root, post_form=post).access_token()
        form = post.calls[0]
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == "oaiapp_TEST"
        assert form["resource"] == "https://api.openai.com/v1"
        assert form["refresh_token"] == SECRET_REFRESH


def test_nonterminal_failure_does_not_park_the_store() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=503, payload={})
        store = OAuthTokenStore(root, post_form=post)
        try:
            store.access_token()
        except OAuthTokenError as exc:
            assert not isinstance(exc, OAuthNotAuthorizedError)
        else:
            raise AssertionError("a 503 refresh must not be reported as success")
        stored = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
        assert stored.get("state") != "needs_reauth", "transient failure must not park the store"


def test_invalid_grant_parks_the_store() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(status=400, payload={"error": "invalid_grant"})
        store = OAuthTokenStore(root, post_form=post)
        try:
            store.access_token()
        except OAuthNotAuthorizedError:
            pass
        else:
            raise AssertionError("invalid_grant must raise OAuthNotAuthorizedError")
        stored = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
        assert stored["state"] == "needs_reauth"


def test_parked_store_fails_fast_without_calling_the_provider() -> None:
    """A revoked grant must not become a retry storm across the cluster."""
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
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


def test_account_switch_is_rejected() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        post = _CountingPost(payload={
            "access_token": SECRET_ACCESS,
            "refresh_token": "rt-ROTATED-1",
            "expires_in": 3600,
            "sub": "sub-DIFFERENT",
        })
        store = OAuthTokenStore(root, post_form=post)
        try:
            store.access_token()
        except OAuthIdentityChangedError:
            pass
        else:
            raise AssertionError("an account switch must never be absorbed silently")


def test_atomic_write_is_private_and_leaves_no_temp_file() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root, expires_in=-1)
        OAuthTokenStore(root, post_form=_CountingPost()).access_token()
        path = root / "credentials.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        leftovers = [p.name for p in root.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"atomic write left temp files: {leftovers}"
        json.loads(path.read_text(encoding="utf-8"))  # always a complete document


def test_store_refuses_to_overlap_codex_credentials() -> None:
    try:
        OAuthTokenStore(pathlib.Path.home() / ".codex" / "turnstone-oauth")
    except OAuthTokenError:
        return
    raise AssertionError("store must refuse to live inside the Codex credential directory")


def test_status_is_non_secret() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp) / "store"
        _seed(root)
        summary = OAuthTokenStore(root).status()
        blob = json.dumps(summary)
        assert SECRET_REFRESH not in blob
        assert "at-OLD" not in blob
        assert summary["account"] == "operator@example.invalid"
        assert summary["refresh_token_present"] is True


def test_errors_never_contain_credential_material() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
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
        store = OAuthTokenStore(root, token_endpoint=endpoint)
        queue.put(("ok", store.access_token()))
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_processes_issue_exactly_one_refresh() -> None:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
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
            ctx.Process(
                target=_worker,
                args=(str(root), f"http://127.0.0.1:{port}/token", queue),
            )
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
        # Every worker still got a usable token (the winner's).
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
