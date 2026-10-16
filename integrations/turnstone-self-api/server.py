#!/usr/bin/env python3
"""Turnstone 1.8.7 self-API MCP bridge. NOT deployed; stdio only.

Run via explicitly approved exact-argv sudoers entry from the Turnstone service
user, from a root-owned non-writable install path. No network listener.
The root-only ts_ token is never returned to a model.
"""
from __future__ import annotations
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
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
    re.compile(r"(?i)\bBearer[ \t]+\S{8,}"),
)
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
    if (any(ord(c) < 32 or ord(c) > 126 for c in path)
            or "\\" in decoded or "//" in decoded or "@" in decoded or "\x00" in decoded
            or any(segment in (".", "..") for segment in decoded.split("/"))):
        raise BridgeRefusal("ambiguous or traversal path refused")
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        decoded_key = key
        for _ in range(4):
            further = urllib.parse.unquote(decoded_key)
            if further == decoded_key:
                break
            decoded_key = further
        if decoded_key.lower() in {"reveal", "include_secrets", "show_secrets"}:
            raise BridgeRefusal("secret reveal arguments refused")
        if any("\r" in x or "\n" in x for x in (key, value)):
            raise BridgeRefusal("unsafe query value")
    if method != "GET" and parsed.query:
        raise BridgeRefusal("mutation query parameters require separate adapter review")
    return path

def redact(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            # Convert camelCase keys such as accessToken and clientSecret.
            normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
            result[key] = "[REDACTED]" if SENSITIVE.search(normalized) else redact(value)
        return result
    if isinstance(data, list):
        return [redact(value) for value in data]
    if isinstance(data, str):
        safe = data
        for pattern in TOKEN_PATTERNS:
            safe = pattern.sub("[REDACTED]", safe)
        return safe
    return data

def pack(status: int, body: Any) -> str:
    out = json.dumps({"status": status, "body": body}, ensure_ascii=True, separators=(",", ":"))
    if len(out) > MAX_RETURN:
        return json.dumps({"status": status, "truncated": True, "length": len(out),
                           "message": "Use narrower filters or pagination"})
    return out

class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None

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
            media = response.headers.get("Content-Type", "").split(";")[0].lower()
            if media not in ("application/json", "application/problem+json"):
                return pack(response.status, "[Non-JSON response withheld]")
            raw = response.read(MAX_BODY + 1)
            try:
                return pack(response.status, redact(json.loads(raw)))
            except (ValueError, UnicodeDecodeError):
                return pack(response.status, "[Non-JSON or oversized response withheld]")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_BODY + 1)
            if exc.headers.get("Content-Type", "").lower().startswith("application/json"):
                try:
                    body = redact(json.loads(raw))
                except (ValueError, UnicodeDecodeError):
                    body = "[Unparseable response withheld]"
            else:
                body = "[Non-JSON response withheld]"
            return pack(exc.code, body)
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        return pack(0, "[Connection error details withheld]")

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
        Do not create/reveal/copy plaintext credentials through a model tool.
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
