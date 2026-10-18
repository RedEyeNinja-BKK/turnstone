"""Opaque request correlation for gateway-hosted model lanes.

Turnstone's provider call carries no correlation identity of its own, so a gateway
that logs sessions cannot attribute a request — or a candidate fallback inside it —
to the turn that produced it. This module supplies the identifiers Switchyard
already understands, and nothing else.

Three properties are deliberate:

* **Opaque.** Every value is a random hex token or the workstream id, never a
  user name, address, prompt, or model content.
* **Opt-in per host.** Headers are emitted only for base URLs whose host appears
  in the configured allowlist. A direct provider endpoint never receives
  Turnstone-specific headers, because nothing is sent unless an operator names
  that endpoint.
* **Stable across an internal retry.** ``operation()`` is resolved once per
  logical provider operation and reused by every retry of that operation, so a
  retried request is visibly the same work rather than several new requests.

The sensor is unaffected: nothing here enters the sensor state fingerprint.
"""

from __future__ import annotations

import contextvars
import os
import secrets
from urllib.parse import urlsplit

#: Reported as ``x-switchyard-origin``. A constant: the gateway needs to know
#: which host class called it, not which operator or session.
ORIGIN = "turnstone"

SESSION_HEADER = "x-switchyard-session-id"
ORIGIN_HEADER = "x-switchyard-origin"
TURN_HEADER = "x-switchyard-turn-id"
REQUEST_HEADER = "x-switchyard-request-id"

#: Comma-separated ``host`` or ``host:port`` entries. Empty means correlation is
#: off, which is the default: enabling it is an operator decision about a
#: specific gateway, not a global behaviour change.
HOSTS_ENV = "TURNSTONE_CORRELATION_HOSTS"

_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "turnstone_correlation_session", default=""
)
_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "turnstone_correlation_turn", default=""
)
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "turnstone_correlation_request", default=""
)


def opaque_id() -> str:
    """A 128-bit opaque identifier. Random per call, never derived from content."""
    return secrets.token_hex(16)


def start_session(session_id: str = "") -> str:
    """Bind the session identity for the current task. Empty mints a new one."""
    value = session_id or opaque_id()
    _session_id.set(value)
    return value


def start_turn(turn_id: str = "") -> str:
    """Bind a turn identity. One assistant reply is one turn."""
    value = turn_id or opaque_id()
    _turn_id.set(value)
    return value


def start_operation(operation_id: str = "") -> str:
    """Bind the identity of one logical provider operation.

    Call once per operation, *outside* any retry loop: every retry of the same
    operation must carry the same id so the gateway can show one request that was
    attempted several times rather than several unrelated requests.
    """
    value = operation_id or opaque_id()
    _request_id.set(value)
    return value


def reset() -> None:
    """Clear every binding in the current task. Test and teardown helper."""
    _session_id.set("")
    _turn_id.set("")
    _request_id.set("")


def current() -> tuple[str, str, str]:
    """The identifiers bound to the current task, as ``(session, turn, request)``."""
    return _session_id.get(), _turn_id.get(), _request_id.get()


def allowed_hosts(raw: str | None = None) -> frozenset[str]:
    """Parse the host allowlist from its string form."""
    source = os.environ.get(HOSTS_ENV, "") if raw is None else raw
    return frozenset(part.strip().lower() for part in source.split(",") if part.strip())


def headers_for(
    base_url: str | None,
    *,
    hosts: frozenset[str] | None = None,
) -> dict[str, str] | None:
    """Headers for *base_url*, or ``None`` when correlation does not apply.

    ``None`` is the common case and is not an error: it is what keeps a direct
    provider from receiving identifiers it has no use for.
    """
    if not base_url:
        return None
    allowed = allowed_hosts() if hosts is None else hosts
    if not allowed:
        return None
    split = urlsplit(base_url)
    candidates = {
        value.lower()
        for value in (
            split.netloc,
            split.hostname or "",
        )
        if value
    }
    if not candidates & allowed:
        return None
    session_id, turn_id, request_id = current()
    if not session_id and not request_id:
        return None
    headers = {ORIGIN_HEADER: ORIGIN}
    if session_id:
        headers[SESSION_HEADER] = session_id
    if turn_id:
        headers[TURN_HEADER] = turn_id
    if request_id:
        headers[REQUEST_HEADER] = request_id
    return headers
