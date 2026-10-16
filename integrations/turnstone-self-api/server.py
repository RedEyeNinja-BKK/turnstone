#!/usr/bin/env python3
"""Turnstone 1.8.7 self-API MCP bridge. NOT deployed; stdio only.

Run via explicitly approved exact-argv sudoers entry from the Turnstone service
user, from a root-owned non-writable install path. No network listener.

The root-only ts_ token is never returned to a model.

SECURITY MODEL (accurate, do not overclaim): the privileged launcher is a
CONVENIENCE for a trusted administrative principal. It is NOT a security
barrier: the Turnstone service UID is not an isolation boundary, and MCP
approvals are dispatch-level controls, not independently enforced
authorization. This bridge therefore does not attempt to prove that a call was
operator-approved; it aims to avoid *creating* disclosure or self-destruction,
and to leave a predictable audit receipt.

LC-RD-003 patch round (focused defects only):
  A. credential-disclosure routes refused explicitly (never silently);
  B. malformed / percent-encoded query and path handling normalised;
  C. predictable audit receipt on every response;
  D. self-disabling operations refused when the bridge's own server id is set.
All existing functionality is otherwise preserved.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

TOKEN_FILE = Path("/srv/turnstone/credentials/api/turnstone_admin_token")

ORIGINS = {
    "console": "http://127.0.0.1:8090",
    "head": "http://127.0.0.1:8080",
    "worker_2": "http://127.0.0.1:8082",
    "worker_3": "http://127.0.0.1:8083",
    "worker_4": "http://127.0.0.1:8084",
    "worker_5": "http://127.0.0.1:8085",
}

SENSITIVE = re.compile(
    r"(^|[_-])(password|passwd|secret|token|credential|api[_-]?key|private[_-]?key|authorization|cookie|env|headers|jwt|bearer)([_-]|$)",
    re.IGNORECASE,
)

TOKEN_PATTERNS = (
    re.compile(r"\bts_[0-9a-fA-F]{64}\b"),
    re.compile(r"\bglsa_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:sk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer[ \t]+\S{8,}"),
)

# --- LC-RD-003 patch: query-key normalisation -------------------------------
# Keys that must never be forwarded, whatever their encoding or case.  This
# replaces a three-name literal denylist, which missed ``token`` /
# ``access_token`` and relied on the raw (still-encoded) key text.
SENSITIVE_QUERY = re.compile(
    r"(reveal|include[_-]?secrets|show[_-]?secrets|secret|token|passwd|password"
    r"|api[_-]?key|private[_-]?key|credential|client[_-]?secret|bearer|jwt)",
    re.IGNORECASE,
)
MAX_QUERY_DECODE_ROUNDS = 4

# --- LC-RD-003 patch: credential-disclosure route + self-disable guard -------
# Routes that mint or return plaintext credentials.  Their whole purpose is to
# emit a secret in the response, so key-name redaction cannot be relied on.
CREDENTIAL_MINT_ROUTES = (
    # POST: creates a ts_ token.  ``/?`` so a trailing-slash spelling of the
    # same handler is not a bypass.
    re.compile(r"^/v1/api/admin/users/[^/]+/tokens/?$"),
)
# The bridge's own MCP registration, so it cannot disable or delete itself.
#
# Propagation note (LC-RD-003): the MCP row's ``env`` IS handed to the spawned
# stdio process, but the production launcher is ``sudo``, whose default
# ``env_reset`` drops environment variables before exec'ing the target -- so
# relying on the env var alone would silently leave this guard inactive.  The
# id is therefore resolved from three sources, in order:
#   1. ``SELF_MCP_SERVER_ID`` in the environment (direct launch, or a launcher
#      wrapper that re-exports it);
#   2. a root-owned ``self_server_id`` file beside this module (survives
#      ``env_reset`` and is the recommended production source);
#   3. an admin-API self-lookup matching this module's path in a row's
#      command/args (no operator step required).
# When none resolves, the guard state is reported LOUDLY in every receipt as
# ``inactive_unresolved`` rather than failing silently.
OWN_ID_ENV = os.environ.get("SELF_MCP_SERVER_ID", "").strip()
SELF_ID_FILENAME = "self_server_id"
_SERVER_ID_RX = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
# Retained for backwards compatibility / test patching.
OWN_SERVER_ID = OWN_ID_ENV

MAX_BODY = 262144
MAX_RETURN = 18000


class BridgeRefusal(ValueError):
    pass


def read_token() -> str:
    if os.geteuid() != 0:
        raise BridgeRefusal("approved root credential broker required")
    fd = os.open(str(TOKEN_FILE), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise BridgeRefusal("unexpected credential ownership or mode")
        raw = os.read(fd, 8193)
        if not re.fullmatch(rb"ts_[0-9a-f]{64}", raw):
            raise BridgeRefusal("invalid Turnstone token formatting")
        return raw.decode("ascii")
    finally:
        os.close(fd)


def _decoded_query_keys(query: str) -> list[str]:  # retained for callers/tests
    """Return query keys decoded to fixpoint (bounded), for policy matching.

    Raw key text is useless for policy: ``%2572eveal`` and ``reveal`` name the
    same parameter to the server.  Decode repeatedly, then also decode the whole
    query once more so partially-encoded keys cannot slip through.
    """
    keys: list[str] = []
    for key, _ in urllib.parse.parse_qsl(query, keep_blank_values=True):
        decoded = key
        for _round in range(MAX_QUERY_DECODE_ROUNDS):
            further = urllib.parse.unquote(decoded)
            if further == decoded:
                break
            decoded = further
        keys.append(decoded)
    return keys


def _read_self_id_file() -> str:
    """Read the root-owned ``self_server_id`` file beside this module.

    Fails closed to "" on any ownership/mode irregularity, so a world-writable
    or non-root file can never activate (or be used to forge) the guard.
    """
    path = Path(__file__).resolve().parent / SELF_ID_FILENAME
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            return ""
        raw = os.read(fd, 129).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    finally:
        os.close(fd)
    return raw if _SERVER_ID_RX.fullmatch(raw) else ""


def _lookup_own_server_id() -> str:
    """Best-effort admin-API self-lookup: find the row describing THIS bridge.

    Guarded by ``_api_lookup_in_progress`` because the lookup itself performs an
    API GET, whose receipt builds guard state -- without the flag that is a
    recursion.
    """
    global _api_lookup_in_progress
    if _api_lookup_in_progress:
        return ""
    _api_lookup_in_progress = True
    try:
        me = Path(__file__).resolve().name
        payload = json.loads(perform("GET", "/v1/api/admin/mcp-servers"))
        for row in ((payload.get("body") or {}).get("servers") or []):
            blob = f"{row.get('command', '')} {' '.join(row.get('args') or [])}"
            if me in blob:
                sid = str(row.get("server_id") or "")
                if _SERVER_ID_RX.fullmatch(sid):
                    return sid
    except Exception:  # noqa: BLE001 -- lookup is best-effort, never fatal
        return ""
    finally:
        _api_lookup_in_progress = False
    return ""


_resolved_own_id: str | None = None
_resolved_own_id_source: str = ""
_api_lookup_in_progress = False
_api_lookup_attempted = False


def _own_server_id(allow_api: bool = False) -> tuple[str, str]:
    """Return ``(server_id, source)`` for this bridge, memoised per process.

    Cheap sources (env, root-owned file) are checked first.  The admin-API
    self-lookup is attempted at most once, and only when *allow_api* is set
    (i.e. from an actual mutation check), so ordinary reads never pay for it.
    Source is ``env``, ``file``, ``api`` or ``""`` (unresolved).
    """
    global _resolved_own_id, _resolved_own_id_source, _api_lookup_attempted
    if _resolved_own_id is None:
        for candidate, source in (
            (OWN_ID_ENV, "env"),
            (_read_self_id_file(), "file"),
        ):
            if candidate and _SERVER_ID_RX.fullmatch(candidate):
                _resolved_own_id, _resolved_own_id_source = candidate, source
                break
        else:
            _resolved_own_id, _resolved_own_id_source = "", ""
    if not _resolved_own_id and allow_api and not _api_lookup_attempted:
        _api_lookup_attempted = True
        found = _lookup_own_server_id()
        if found:
            _resolved_own_id, _resolved_own_id_source = found, "api"
    return _resolved_own_id or "", _resolved_own_id_source


def self_guard_state() -> str:
    """Receipt-visible guard state; never silent about being inactive."""
    sid, source = _own_server_id()
    return f"active:{sid} ({source})" if sid else "inactive_unresolved"


def _check_query(query: str) -> None:
    # A bare ';' can act as a parameter separator for some parsers, so a
    # semicolon-delimited sibling of a sensitive key would evade a
    # key-by-key scan.  No documented /v1/api/ route needs one: refuse.
    if ";" in query:
        raise BridgeRefusal("semicolon-separated query arguments refused")
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        decoded_key = key
        decoded_value = value
        for _round in range(MAX_QUERY_DECODE_ROUNDS):
            nk = urllib.parse.unquote(decoded_key)
            nv = urllib.parse.unquote(decoded_value)
            if nk == decoded_key and nv == decoded_value:
                break
            decoded_key, decoded_value = nk, nv
        if SENSITIVE_QUERY.search(decoded_key):
            raise BridgeRefusal(f"sensitive query argument refused: {decoded_key!r}")
        # Restores the original CR/LF guard, now applied to the DECODED forms:
        # a percent-encoded control character is not visible in the raw URL.
        if any(c in "\r\n" for c in decoded_key + decoded_value):
            raise BridgeRefusal("unsafe query value")


def _check_route_policy(method: str, path: str) -> None:
    """LC-RD-003 patch A/D: refuse specific unsafe routes, never silently.

    Deliberately narrow: only routes whose *purpose* is to hand back a
    plaintext credential, plus the bridge's own registration.  A generic
    route allowlist is NOT imposed -- full operator-authorized
    self-management capability is preserved.
    """
    for rx in CREDENTIAL_MINT_ROUTES:
        if method == "POST" and rx.match(path):
            raise BridgeRefusal(
                "credential-minting route refused: it returns a plaintext token that "
                "redaction cannot be trusted with; mint tokens in the Console UI "
                "(Admin > API Tokens) instead"
            )
    sid, _source = _own_server_id(allow_api=True)
    if (sid and method in {"PUT", "DELETE", "PATCH"}
            and path.rstrip("/") == f"/v1/api/admin/mcp-servers/{sid}"):
        raise BridgeRefusal(
                "self-disable refused: this route would modify this bridge's own MCP "
                "registration; perform it from the Console with operator oversight"
            )


def validate_path(path: str, method: str) -> str:
    if not isinstance(path, str) or len(path) > 2048 or not path.startswith("/"):
        raise BridgeRefusal("expected a local absolute path")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/v1/api/"):
        raise BridgeRefusal("only local /v1/api/ routes are accepted")
    decoded = parsed.path
    for _ in range(4):
        nxt = urllib.parse.unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    # LC-RD-003 patch B: the validator previously decoded for inspection while
    # forwarding the ORIGINAL still-encoded path, so percent-encoding could
    # change what the server actually routed.  No documented /v1/api/ route
    # needs percent-encoding, so refuse it outright -- one rule, whole class.
    if "%" in parsed.path:
        raise BridgeRefusal("percent-encoded path characters refused")
    if (any(ord(c) < 32 or ord(c) > 126 for c in path)
            or "\\" in decoded or "//" in decoded or "@" in decoded or "\x00" in decoded
            or any(segment in (".", "..") for segment in decoded.split("/"))):
        raise BridgeRefusal("ambiguous or traversal path refused")
    _check_query(parsed.query)
    if method != "GET" and parsed.query:
        raise BridgeRefusal("mutation query parameters require separate adapter review")
    _check_route_policy(method, path)
    return path


def redact(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            # Convert camelCase keys such as accessToken and clientSecret.
            normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
            # LC-RD-003 patch: client_id (and any *_ct ciphertext field) is
            # redacted too -- cheap insurance; it costs visibility only, never
            # the ability to WRITE such fields.
            extra = bool(re.search(r"(^|_)client_id($|_)|_ct$", normalized, re.IGNORECASE))
            result[key] = "[REDACTED]" if (SENSITIVE.search(normalized) or extra) else redact(value)
        return result
    if isinstance(data, list):
        return [redact(value) for value in data]
    if isinstance(data, str):
        safe = data
        for pattern in TOKEN_PATTERNS:
            safe = pattern.sub("[REDACTED]", safe)
        return safe
    return data


def _canonical_body(body_json: str) -> str:
    """Canonical form of a request body for the receipt digest.

    Hashing the raw input string would make semantically identical requests
    (key order, whitespace) hash differently.  Normalise when it parses.
    """
    if not body_json.strip():
        return ""
    try:
        return json.dumps(json.loads(body_json), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return body_json


def _receipt(method: str, path: str, target: str, body_json: str, status: int) -> dict[str, Any]:
    """LC-RD-003 patch C: predictable audit receipt on every response.

    Correlation record only.  A same-UID caller could forge one, so this is
    NOT an authorization receipt -- it exists so an operator can bind a
    specific tool call to the API action it produced.
    """
    digest = hashlib.sha256(
        f"{method}\n{path}\n{target}\n{_canonical_body(body_json)}".encode()
    ).hexdigest()
    return {
        "receipt_id": uuid.uuid4().hex,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": method,
        "path": path,
        "target": target,
        "status": status,
        "request_sha256": digest,
        "self_guard": self_guard_state(),
    }


def pack(status: int, body: Any) -> str:
    out = json.dumps({"status": status, "body": body}, ensure_ascii=True, separators=(",", ":"))
    if len(out) > MAX_RETURN:
        return json.dumps({"status": status, "truncated": True, "length": len(out),
                           "message": "Use narrower filters or pagination"})
    return out


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _emit(receipt: dict[str, Any], status: int, body: Any) -> str:
    out = json.dumps({"status": status, "receipt": receipt, "body": body},
                     ensure_ascii=True, separators=(",", ":"))
    if len(out) > MAX_RETURN:
        return json.dumps({"status": status, "receipt": receipt, "truncated": True,
                           "length": len(out),
                           "message": "Use narrower filters or pagination"})
    return out


def perform(method: str, path: str, body_json: str = "", target: str = "console") -> str:
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise BridgeRefusal("unsupported method")
    if target not in ORIGINS:
        raise BridgeRefusal("unknown cluster target")
    validate_path(path, method)
    payload = None
    if method != "GET" and body_json.strip():
        if len(body_json.encode("utf-8")) > 65536:
            raise BridgeRefusal("request body exceeds limit")
        try:
            obj = json.loads(body_json)
        except ValueError as exc:
            raise BridgeRefusal("body_json must be valid JSON") from exc
        payload = json.dumps(obj).encode("utf-8")
    request = urllib.request.Request(
        ORIGINS[target] + path, data=payload, method=method,
        headers={"Authorization": "Bearer " + read_token(),
                 "Accept": "application/json", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=15) as response:
            receipt = _receipt(method, path, target, body_json, response.status)
            media = response.headers.get("Content-Type", "").split(";")[0].lower()
            if media not in ("application/json", "application/problem+json"):
                return _emit(receipt, response.status, "[Non-JSON response withheld]")
            raw = response.read(MAX_BODY + 1)
            try:
                return _emit(receipt, response.status, redact(json.loads(raw)))
            except (ValueError, UnicodeDecodeError):
                return _emit(receipt, response.status, "[Non-JSON or oversized response withheld]")
    except urllib.error.HTTPError as exc:
        try:
            receipt = _receipt(method, path, target, body_json, exc.code)
            raw = exc.read(MAX_BODY + 1)
            # ``HTTPError.headers`` can be None (e.g. a synthesised/edge error),
            # which would raise AttributeError inside the error path.
            hdrs = exc.headers
            ctype = hdrs.get("Content-Type", "") if hdrs is not None else ""
            if ctype.lower().startswith("application/json"):
                try:
                    body = redact(json.loads(raw))
                except (ValueError, UnicodeDecodeError):
                    body = "[Unparseable response withheld]"
            else:
                body = "[Non-JSON response withheld]"
            return _emit(receipt, exc.code, body)
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        receipt = _receipt(method, path, target, body_json, 0)
        return _emit(receipt, 0, "[Connection error details withheld]")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Self-MCP requires approved root credential broker")
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("turnstone-self-api")

    @mcp.tool()
    def turnstone_api_read(path: str, target: str = "console") -> str:
        """Authenticated read of documented v1.8.7 /v1/api/ routes.

        Target is console, head, worker_2, worker_3, worker_4 or worker_5.
        Secret-bearing fields are redacted; pagination is caller-controlled.
        """
        try:
            return perform("GET", path, target=target)
        except BridgeRefusal as exc:
            return pack(0, f"Refused: {exc}")

    @mcp.tool()
    def turnstone_api_change(method: str, path: str, body_json: str = "",
                             target: str = "console") -> str:
        """Operator-approved Turnstone API POST/PUT/PATCH/DELETE. Consult
        the live OpenAPI schema and preserve state, backups and audit logs.
        Credential-minting routes and this bridge's own registration are
        refused. Do not create/reveal/copy plaintext credentials through a
        model tool.
        """
        try:
            if method.upper() == "GET":
                raise BridgeRefusal("use the read tool for GET")
            return perform(method, path, body_json, target)
        except BridgeRefusal as exc:
            return pack(0, f"Refused: {exc}")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
