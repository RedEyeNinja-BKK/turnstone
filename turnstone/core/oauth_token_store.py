# SPDX-License-Identifier: Apache-2.0
"""Coordinated lifecycle for a rotating OAuth refresh token.

WHY THIS MODULE EXISTS
----------------------
An OAuth provider that rotates refresh tokens invalidates the previous token the
moment a new one is issued.  Several Turnstone processes share one account
credential (the head server, the execution nodes and the console all need an
access token for the same provider), so an uncoordinated refresh is not merely
wasteful: the first refresher retires the token every other process still holds,
and the account ends up silently revoked.

The invariant this module enforces is therefore **at most one refresh per
rotation**, with the *others reusing the winner's result*.  It is enforced with
two mechanisms, both required:

* an exclusive ``flock`` on a sibling lock file, so only one process refreshes;
* a **re-read after acquiring the lock**, so a process that was waiting on a peer
  discovers the peer's fresh token and returns it instead of refreshing again.

The token itself is persisted atomically (temp file + ``fsync`` + ``os.replace``),
so a crash mid-refresh can never leave a truncated credential file — which for a
rotating token would destroy the only credential that can recover the account.

CREDENTIAL HYGIENE
------------------
The refresh token is read from a file into memory and sent in a request body.
It is never placed on a command line, never exported into the environment, never
logged, and never echoed in an exception message.  ``repr``-safe error types are
used so a credential cannot leak through a traceback or a log formatter.

This module deliberately does NOT decide *where* the credential lives: the store
path is injected.  Pointing it at a service-account runtime store keeps
authoritative storage and runtime consumption separable, which is what the
credential architecture requires.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# Default provider endpoints (documented direct-account OAuth).
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"

# Refresh this many seconds before nominal expiry. A streamed call must not be
# handed a token that expires mid-flight.
DEFAULT_RENEWAL_MARGIN_S = 300

# A store that has failed with ``invalid_grant`` is not retried in a loop; it is
# parked in this state until an operator re-authorizes.
STATE_NEEDS_REAUTH = "needs_reauth"

REQUIRED_CREDENTIAL_FIELDS = ("client_id", "refresh_token", "expires_at")

# A store must never be pointed at the Codex CLI's own credential directory.
CODEX_HOME_MARKER = ".codex"
_FORBIDDEN_STORE_PREFIXES = (str(pathlib.Path.home() / ".codex"),)


class OAuthTokenError(RuntimeError):
    """Base class. Never carries credential material in its message."""


class OAuthNotAuthorizedError(OAuthTokenError):
    """No usable credential, or the grant was revoked. Re-authorization required."""


class OAuthIdentityChangedError(OAuthTokenError):
    """The refreshed grant belongs to a different account than the stored one."""


PostForm = Callable[[str, dict[str, str]], "tuple[int, dict[str, Any]]"]


def _default_post_form(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """POST an application/x-www-form-urlencoded body; return (status, json)."""
    body = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - operator-configured endpoint
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        # The error body is provider JSON; a rejected grant is a normal outcome.
        raw = exc.read()
        status = exc.code
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    return status, payload


def _atomic_write_json(path: pathlib.Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    """Persist *payload* atomically and durably.

    temp file in the same directory -> fsync the file -> os.replace -> fsync the
    directory. A reader therefore sees either the old file or the new one, never
    a partial write, even across a crash or power loss.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class OAuthTokenStore:
    """A single rotating refresh token, shared safely by cooperating processes."""

    def __init__(
        self,
        root: str | pathlib.Path,
        *,
        token_endpoint: str = TOKEN_ENDPOINT,
        resource: str = RESOURCE,
        renewal_margin_s: int = DEFAULT_RENEWAL_MARGIN_S,
        post_form: PostForm | None = None,
    ) -> None:
        self.root = pathlib.Path(root).expanduser().resolve()
        resolved = str(self.root)
        for bad in _FORBIDDEN_STORE_PREFIXES:
            if resolved.startswith(bad):
                raise OAuthTokenError(
                    "REFUSING: store overlaps the Codex CLI credential directory"
                )
        if CODEX_HOME_MARKER in self.root.parts:
            raise OAuthTokenError("REFUSING: store path looks like a Codex home")
        self.token_endpoint = token_endpoint
        self.resource = resource
        self.renewal_margin_s = int(renewal_margin_s)
        self._post = post_form or _default_post_form

    # -- paths ---------------------------------------------------------------
    @property
    def credentials_path(self) -> pathlib.Path:
        return self.root / "credentials.json"

    @property
    def lock_path(self) -> pathlib.Path:
        return self.root / "credentials.lock"

    # -- reading -------------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """Return the stored credential, or raise if absent/unusable."""
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise OAuthNotAuthorizedError("no stored credential; authorization required") from exc
        try:
            creds = json.loads(raw)
        except ValueError as exc:
            raise OAuthNotAuthorizedError("stored credential is not valid JSON") from exc
        if not isinstance(creds, dict):
            raise OAuthNotAuthorizedError("stored credential is not an object")
        missing = [f for f in REQUIRED_CREDENTIAL_FIELDS if not creds.get(f)]
        if missing:
            raise OAuthNotAuthorizedError(f"stored credential missing fields: {', '.join(missing)}")
        return creds

    def is_usable(self, creds: dict[str, Any], *, now: float | None = None) -> bool:
        """True when ``creds`` can serve a call without refreshing."""
        now = time.time() if now is None else now
        return int(creds.get("expires_at", 0)) - now > self.renewal_margin_s

    # -- the coordinated path ------------------------------------------------
    def access_token(self, *, now: float | None = None) -> str:
        """Return a usable access token, refreshing at most once cluster-wide.

        Fast path takes no lock.  The slow path serialises on an exclusive lock
        and re-reads the store before refreshing, so a peer that already
        refreshed is detected and reused rather than retired a second time.
        """
        now = time.time() if now is None else now
        creds = self.read()
        self._reject_parked(creds)
        if self.is_usable(creds, now=now):
            return str(creds["access_token"])

        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                # A peer may have refreshed while we waited for the lock.
                creds = self.read()
                self._reject_parked(creds)
                if self.is_usable(creds, now=now):
                    return str(creds["access_token"])
                return self._refresh_locked(creds)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _reject_parked(self, creds: dict[str, Any]) -> None:
        if creds.get("state") == STATE_NEEDS_REAUTH:
            raise OAuthNotAuthorizedError(
                "authorization revoked or expired; re-authorization required"
            )

    def _refresh_locked(self, creds: dict[str, Any]) -> str:
        """Refresh under the lock and persist atomically. Never leaks the token."""
        status, payload = self._post(
            self.token_endpoint,
            {
                "grant_type": "refresh_token",
                "client_id": str(creds["client_id"]),
                "refresh_token": str(creds["refresh_token"]),
                "resource": self.resource,
            },
        )
        if status != 200 or "access_token" not in payload:
            # ``invalid_grant`` is terminal: park the store so the whole cluster
            # fails fast and loudly instead of issuing a retry storm.
            is_terminal = status == 400 and str(payload.get("error", "")) in {
                "invalid_grant",
                "invalid_request",
            } or status in (401, 403)
            if is_terminal:
                parked = dict(creds)
                parked["state"] = STATE_NEEDS_REAUTH
                parked.pop("access_token", None)
                _atomic_write_json(self.credentials_path, parked)
                raise OAuthNotAuthorizedError(
                    f"refresh rejected (HTTP {status}); re-authorization required"
                )
            raise OAuthTokenError(f"refresh failed (HTTP {status})")

        self._verify_identity(creds, payload)

        updated = dict(creds)
        updated["access_token"] = payload["access_token"]
        # The provider rotates the refresh token; persist the NEW one, and keep
        # the old one only if the provider did not issue a replacement.
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        if payload.get("id_token"):
            updated["id_token"] = payload["id_token"]
        updated["expires_at"] = int(time.time()) + int(payload.get("expires_in") or 0)
        for field in ("scope", "granted_scopes"):
            if payload.get(field):
                updated["granted_scopes" if field == "scope" else field] = payload[field]
        updated.pop("state", None)
        _atomic_write_json(self.credentials_path, updated)
        return str(updated["access_token"])

    @staticmethod
    def _verify_identity(creds: dict[str, Any], payload: dict[str, Any]) -> None:
        """Refuse a refresh that silently swapped the underlying account."""
        for field in ("sub", "email"):
            before = creds.get(field)
            after = payload.get(field)
            if before and after and before != after:
                raise OAuthIdentityChangedError(
                    f"refreshed grant identifies a different account ({field} changed)"
                )

    # -- introspection -------------------------------------------------------
    def status(self, *, now: float | None = None) -> dict[str, Any]:
        """Non-secret summary for inventory and diagnostics."""
        now = time.time() if now is None else now
        try:
            creds = self.read()
        except OAuthNotAuthorizedError as exc:
            return {"state": "unusable", "detail": str(exc)}
        return {
            "state": creds.get("state") or ("usable" if self.is_usable(creds, now=now) else "expired"),
            "account": creds.get("email") or creds.get("sub") or "",
            "seconds_until_expiry": int(creds.get("expires_at", 0)) - int(now),
            "refresh_token_present": bool(creds.get("refresh_token")),
            "granted_scopes": creds.get("granted_scopes", ""),
        }
