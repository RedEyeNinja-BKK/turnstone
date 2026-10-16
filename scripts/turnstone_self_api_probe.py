#!/usr/bin/env python3
"""Read-only validation of staged Turnstone Admin API token on local 1.8.7 services.

Usage: sudo python3 scripts/turnstone_self_api_probe.py

No writes, no token disclosure, no redirects, no proxies, no external calls.
Results prove read access only, not write or agent MCP integration.
"""
from __future__ import annotations
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path("/srv/turnstone/credentials/api/turnstone_admin_token")
ORIGINS = {
    "console": "http://127.0.0.1:8090",
    "head": "http://127.0.0.1:8080",
    "worker_2": "http://127.0.0.1:8082",
    "worker_3": "http://127.0.0.1:8083",
    "worker_4": "http://127.0.0.1:8084",
    "worker_5": "http://127.0.0.1:8085",
}

class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), NoRedirects()
)

def get_token() -> str:
    if os.geteuid() != 0:
        raise RuntimeError("Run with sudo so the token remains root-only")
    st = TOKEN_FILE.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or (st.st_mode & 0o077):
        raise RuntimeError("Token file must be root-owned regular file with mode 0600")
    with TOKEN_FILE.open("rb") as inp:
        token = inp.read(8193)
    if not re.fullmatch(rb"ts_[0-9a-f]{64}", token):
        raise RuntimeError("Token must be the raw Turnstone ts_ token, with no whitespace")
    return token.decode("ascii")

def call(base: str, path: str, token: str) -> tuple[int | str, dict | None]:
    req = urllib.request.Request(
        base + path,
        method="GET",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with OPENER.open(req, timeout=8) as resp:
            # Parse only the whoami response. Never print other response bodies.
            if path == "/v1/api/auth/whoami":
                return resp.status, json.loads(resp.read(16384))
            resp.read(1)
            return resp.status, None
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "CONNECTION_ERROR", None

def parse_permissions(raw: object) -> tuple[set[str], str]:
    """Normalize Turnstone's comma-separated RBAC string; distinguish absent/invalid."""
    if raw is None:
        return set(), "NOT_REPORTED"
    if isinstance(raw, str):
        return {p.strip() for p in raw.split(",") if p.strip()}, "REPORTED"
    if isinstance(raw, (list, tuple, set)):
        if not all(isinstance(p, str) for p in raw):
            return set(), "UNEXPECTED_SHAPE"
        return {p.strip() for p in raw if p.strip()}, "REPORTED"
    return set(), "UNEXPECTED_SHAPE"

def main():
    token = get_token()
    needed = (
        "admin.skills", "admin.mcp", "admin.users", "admin.roles",
        "admin.settings", "admin.models", "admin.coordinator",
        "model.skills.write", "coordinator.trust.send",
    )
    for label, base in ORIGINS.items():
        status, data = call(base, "/v1/api/auth/whoami", token)
        print(f"{label}.whoami: {status}")
        if status != 200:
            continue
        if not isinstance(data, dict):
            print(f"{label}.identity: unexpected response shape")
            continue
        scopes = data.get("scopes", [])
        perms, perm_status = parse_permissions(data.get("permissions"))
        if isinstance(scopes, str):
            scopes = [x.strip() for x in scopes.split(",") if x.strip()]
        if not isinstance(scopes, (list, tuple, set)):
            scopes = []
        print(f"{label}.scopes: {sorted(str(x) for x in scopes)}")
        print(f"{label}.permissions_format: {perm_status}")
        print(f"{label}.permissions_count: {len(perms) if perm_status == 'REPORTED' else 'UNKNOWN'}")
        for p in needed:
            verdict = (
                ("YES" if p in perms else "NO")
                if perm_status == "REPORTED" else "UNKNOWN"
            )
            print(f"{label}.perm.{p}: {verdict}")
    base = ORIGINS["console"]
    endpoints = [
        ("cluster_overview", "/v1/api/cluster/overview"),
        ("cluster_nodes", "/v1/api/cluster/nodes?limit=1"),
        ("admin_skills", "/v1/api/admin/skills"),
        ("admin_mcp", "/v1/api/admin/mcp-servers"),
        ("admin_models", "/v1/api/admin/model-definitions"),
        ("admin_settings", "/v1/api/admin/settings"),
        ("admin_roles", "/v1/api/admin/roles"),
        ("admin_users", "/v1/api/admin/users"),
    ]
    for name, path in endpoints:
        status, _ = call(base, path, token)
        print(f"console.{name}: {status}")
    print("No changes made; API POST/PUT/DELETE and agent MCP tool calls UNTESTED.")

if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, UnicodeError) as exc:
        # Never expose error details from OS paths or secret-bearing operations.
        print(f"REFUSED: {type(exc).__name__} (token and details withheld)", file=sys.stderr)
        sys.exit(1)
