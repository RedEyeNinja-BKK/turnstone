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

The token is persisted atomically (temp file + ``fsync`` + ``os.replace`` +
directory ``fsync``), so a crash mid-write cannot leave a truncated credential.

A DISTRIBUTED FAILURE THIS MODULE CANNOT ELIMINATE
--------------------------------------------------
Atomic persistence protects the *file*.  It cannot protect the *rotation*.  If
the provider rotates the refresh token and the process dies before committing
the replacement, the only valid refresh token exists nowhere on this host: the
stored one has already been retired upstream.  The next refresh therefore fails
with ``invalid_grant`` and the store is parked in ``needs_reauth`` (see
:attr:`OAuthTokenStore.STATE_NEEDS_REAUTH`), requiring interactive
re-authorization.  The window is small — one HTTP round trip — but it is real,
and no amount of local atomicity removes it.  Operators should treat an
unexplained ``needs_reauth`` on a healthy account as this failure mode.

CREDENTIAL HYGIENE
------------------
The refresh token is read from a file into memory and sent in a request body.
It is never placed on a command line, never exported into the environment, never
logged, and never interpolated into an exception message — error detail is
restricted to the provider's ``error`` code and the HTTP status.  Log lines carry
only non-secret metadata (state, account, seconds to expiry).

The module deliberately does NOT decide *where* the credential lives: the store
path is injected, so authoritative storage and service-account runtime
consumption stay separable.
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
from datetime import UTC, datetime
from typing import Any

from turnstone.core.log import get_logger

log = get_logger(__name__)

# Default provider endpoints (documented direct-account OAuth).
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"

# Refresh this many seconds before nominal expiry. A streamed call must not be
# handed a token that expires mid-flight. Never applied before the provider's
# own ``earliest_refresh_at`` floor.
DEFAULT_RENEWAL_MARGIN_S = 300

# Scope without which a renewed token cannot serve native inference. Checked on
# every refresh: a silent downgrade would otherwise surface as an opaque 401.
REQUIRED_SCOPES = ("chatgpt.tokens.use.direct",)

# Store states.
STATE_NEEDS_REAUTH = "needs_reauth"

REQUIRED_CREDENTIAL_FIELDS = ("client_id", "refresh_token", "expires_at")

# A store must never be pointed at the Codex CLI's own credential directory.
CODEX_HOME_MARKER = ".codex"
_FORBIDDEN_STORE_PREFIXES = (str(pathlib.Path.home() / ".codex"),)

# Provider error codes, classified. Anything unrecognised is treated as
# transient *except* an explicit policy/authorization code, because parking a
# healthy credential on a misread is worse than retrying a real failure.
_REVOKED_CODES = frozenset({"invalid_grant", "revoked", "expired_token"})
_CONFIG_CODES = frozenset(
    {"invalid_client", "invalid_request", "unauthorized_client", "unsupported_grant_type"}
)
_POLICY_CODES = frozenset({"access_denied", "insufficient_scope", "consent_required"})
_TRANSIENT_CODES = frozenset({"temporarily_unavailable", "server_error", "slow_down"})


class OAuthTokenError(RuntimeError):
    """Base class. Never carries credential material in its message."""


class OAuthNotAuthorizedError(OAuthTokenError):
    """The grant is revoked or expired. Interactive re-authorization required."""


class OAuthIdentityChangedError(OAuthTokenError):
    """The refreshed grant belongs to a different account or client."""


class OAuthClientConfigError(OAuthTokenError):
    """The client configuration was rejected. Not retryable; fix configuration."""


class OAuthPolicyError(OAuthTokenError):
    """Policy/authorization refused the grant, or a required scope was dropped."""


class OAuthTransientError(OAuthTokenError):
    """Network, 5xx or rate limiting. Retryable with backoff."""


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
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Never echo the URL or body — only the failure class.
        raise OAuthTransientError(
            f"token endpoint unreachable ({type(exc).__name__})"
        ) from None
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    return status, payload


def _parse_timestamp(value: Any) -> int | None:
    """Accept an epoch number or an ISO-8601 string; return epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())
    return None


def _classify_failure(status: int, payload: dict[str, Any]) -> OAuthTokenError:
    """Map a provider refusal to a typed error. Message stays non-secret.

    An explicit provider ``error`` code is always more specific than the HTTP
    status, so response codes are consulted first: a 403 carrying
    ``insufficient_scope`` is a policy refusal, not a revoked credential, and
    must not send the operator through a needless re-authorization.
    """
    code = str(payload.get("error") or "").strip()
    detail = f"refresh rejected (HTTP {status}{', ' + code if code else ''})"
    if code in _REVOKED_CODES:
        return OAuthNotAuthorizedError(f"{detail}; re-authorization required")
    if code in _CONFIG_CODES:
        return OAuthClientConfigError(f"{detail}; client configuration problem")
    if code in _POLICY_CODES:
        return OAuthPolicyError(f"{detail}; authorization or policy refused the grant")
    if code in _TRANSIENT_CODES:
        return OAuthTransientError(f"{detail}; retryable")
    # No recognised code: fall back to the status class.
    if status in (401, 403):
        return OAuthNotAuthorizedError(f"{detail}; re-authorization required")
    if 400 <= status < 500:
        # A revoked grant is already covered by its explicit code, so blind
        # retrying here would only hammer the provider.
        return OAuthClientConfigError(f"{detail}; unexpected refusal")
    return OAuthTransientError(f"{detail}; retryable")


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
        required_scopes: tuple[str, ...] = REQUIRED_SCOPES,
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
        self.required_scopes = tuple(required_scopes)
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

    def _refresh_floor(self, creds: dict[str, Any]) -> int:
        """Epoch seconds before which the provider will not accept a refresh.

        ``earliest_refresh_at`` is a provider floor, not an optimisation: asking
        earlier is refused. An absent/unparseable value means no floor.
        """
        return _parse_timestamp(creds.get("earliest_refresh_at")) or 0

    # -- the coordinated path ------------------------------------------------
    def access_token(self, *, now: float | None = None) -> str:
        """Return a usable access token, refreshing at most once cluster-wide.

        Fast path takes no lock.  The slow path serialises on an exclusive lock
        and **re-reads the clock and the store** before refreshing, so a peer
        that already refreshed is detected and reused rather than retired a
        second time, and a long wait for the lock cannot leave the decision based
        on a stale ``now``.
        """
        creds = self.read()
        self._reject_parked(creds)
        current = time.time() if now is None else now
        if self.is_usable(creds, now=current):
            return str(creds["access_token"])

        self._ensure_root()
        # Create the lock at 0600 rather than inheriting the process umask: the
        # file holds no data, but a world-readable artifact inside a 0700
        # credential store is an invitation to confuse it with content.
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with contextlib.suppress(OSError):
            os.chmod(self.lock_path, 0o600)
        with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                # (1) A peer may have refreshed while we waited for the lock.
                # (2) The clock must be re-read too: waiting can consume the
                #     entire margin, and deciding on the pre-lock time would
                #     hand out a token that expires mid-call.
                creds = self.read()
                self._reject_parked(creds)
                current = time.time()
                if self.is_usable(creds, now=current):
                    log.info(
                        "oauth_token.peer_refresh_reused",
                        account=creds.get("email") or creds.get("sub", ""),
                    )
                    return str(creds["access_token"])
                floor = self._refresh_floor(creds)
                if current < floor:
                    # Only reached when the token is inside the renewal margin
                    # (or already expired) but the provider forbids refreshing
                    # yet. A still-valid token keeps serving calls; only a
                    # genuinely expired one is a hard failure.
                    if int(creds.get("expires_at", 0)) > current:
                        log.info(
                            "oauth_token.refresh_deferred_to_floor",
                            seconds_until_allowed=floor - int(current),
                        )
                        return str(creds["access_token"])
                    raise OAuthPolicyError(
                        f"refresh not permitted until {floor - int(current)}s from now "
                        "(provider earliest_refresh_at)"
                    )
                return self._refresh_locked(creds)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _ensure_root(self) -> None:
        """Create the store directory privately; never widen existing modes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)

    def _reject_parked(self, creds: dict[str, Any]) -> None:
        if creds.get("state") == STATE_NEEDS_REAUTH:
            raise OAuthNotAuthorizedError(
                "authorization revoked or expired; re-authorization required"
            )

    def _refresh_locked(self, creds: dict[str, Any]) -> str:
        """Refresh under the lock and persist atomically. Never leaks the token."""
        try:
            status, payload = self._post(
                self.token_endpoint,
                {
                    "grant_type": "refresh_token",
                    "client_id": str(creds["client_id"]),
                    "refresh_token": str(creds["refresh_token"]),
                    "resource": self.resource,
                },
            )
        except OAuthTokenError:
            raise
        if status != 200 or "access_token" not in payload:
            error = _classify_failure(status, payload)
            if isinstance(error, OAuthNotAuthorizedError):
                # Terminal: park the store so the whole cluster fails fast and
                # loudly instead of issuing a retry storm.
                parked = dict(creds)
                parked["state"] = STATE_NEEDS_REAUTH
                parked.pop("access_token", None)
                _atomic_write_json(self.credentials_path, parked)
                log.warning("oauth_token.parked_needs_reauth", http_status=status)
            else:
                log.warning(
                    "oauth_token.refresh_failed",
                    http_status=status,
                    error_type=type(error).__name__,
                )
            raise error

        self._verify_identity(creds, payload)
        self._verify_scopes(creds, payload)

        updated = dict(creds)
        updated["access_token"] = payload["access_token"]
        # The provider rotates the refresh token; persist the NEW one, and keep
        # the old one only if the provider did not issue a replacement.
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        if payload.get("id_token"):
            updated["id_token"] = payload["id_token"]
        lifetime = int(payload.get("expires_in") or 0)
        updated["expires_at"] = int(time.time()) + lifetime
        for field in ("scope", "granted_scopes"):
            if payload.get(field):
                updated["granted_scopes" if field == "scope" else field] = payload[field]
        earliest = _parse_timestamp(payload.get("earliest_refresh_at"))
        if earliest is not None:
            updated["earliest_refresh_at"] = earliest
        elif "earliest_refresh_at" not in updated:
            updated.pop("earliest_refresh_at", None)
        updated.pop("state", None)
        _atomic_write_json(self.credentials_path, updated)
        log.info(
            "oauth_token.refreshed",
            account=updated.get("email") or updated.get("sub", ""),
            expires_in=lifetime,
        )
        return str(updated["access_token"])

    @staticmethod
    def _verify_identity(creds: dict[str, Any], payload: dict[str, Any]) -> None:
        """Refuse a refresh that silently swapped the account or client."""
        for field in ("sub", "email", "client_id"):
            before = creds.get(field)
            after = payload.get(field)
            if before and after and before != after:
                raise OAuthIdentityChangedError(
                    f"refreshed grant identifies a different principal ({field} changed)"
                )

    def _verify_scopes(self, creds: dict[str, Any], payload: dict[str, Any]) -> None:
        """A renewal must not silently drop a required direct-use permission."""
        reported = payload.get("scope") or payload.get("granted_scopes")
        if not reported:
            return
        granted = {s for s in str(reported).replace(",", " ").split() if s}
        missing = [s for s in self.required_scopes if s not in granted]
        if missing:
            raise OAuthPolicyError(
                "renewed grant is missing required scope(s): " + ", ".join(missing)
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
            "seconds_until_refresh_allowed": max(0, self._refresh_floor(creds) - int(now)),
            "refresh_token_present": bool(creds.get("refresh_token")),
            "granted_scopes": creds.get("granted_scopes", ""),
        }
