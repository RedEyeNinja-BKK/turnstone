#!/usr/bin/env python3
"""Read-only status of native Turnstone memories and external development notes.

No memory bodies, tokens or individual memory names printed. No DB/file writes.
Run: sudo python3 scripts/turnstone_memory_status_probe.py
Optional external clone path: --external-path /path/to/turnstone-memory
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = Path("/srv/turnstone/credentials/api/turnstone_admin_token")
CONSOLE = "http://127.0.0.1:8090"
CANDIDATES = (
    Path("/srv/turnstone/reference/turnstone-memory"),
    Path("/opt/turnstone-memory"),
    Path("/home/vincent/turnstone-memory"),
)

class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None

def token_from_file() -> str:
    if os.geteuid() != 0:
        raise RuntimeError("root required for root-owned API credential")
    fd = os.open(str(TOKEN), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise RuntimeError("invalid credential file ownership or mode")
        raw = os.read(fd, 8193)
        if not re.fullmatch(rb"ts_[0-9a-f]{64}", raw):
            raise RuntimeError("invalid token file format")
        return raw.decode("ascii")
    finally:
        os.close(fd)

def read_endpoint(path: str, token: str) -> tuple[int | str, dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RefuseRedirect())
    request = urllib.request.Request(
        CONSOLE + path, method="GET",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                return "OVERSIZED", {}
            obj = json.loads(raw)
            return response.status, obj if isinstance(obj, dict) else {}
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
        return "CONNECTION_ERROR", {}

def can_turnstone_read(path: Path) -> bool | None:
    if not path.exists():
        return False
    try:
        p = subprocess.run(
            ["runuser", "-u", "turnstone", "--", "test", "-r", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None

def examine_reference(path: Path) -> None:
    base = path / "collections" / "turnstone"
    if not base.is_dir() or not (path / "scripts" / "index.py").is_file():
        print(f"external_repository_candidate: {path} PRESENT_BUT_NOT_EXPECTED_LAYOUT")
        return
    index = base / "MEMORY.md"
    items = list(base.glob("*.md"))
    shared_count = sum(f.is_file() and not f.is_symlink() and f.name != "MEMORY.md" for f in items)
    print(f"external_repository_candidate: {path} EXPECTED_LAYOUT")
    print(f"external_shared_memory_file_count: {shared_count}")
    print(f"external_generated_index_exists: {index.is_file()}")
    print(f"external_index_readable_by_turnstone: {can_turnstone_read(index)}")
    print(f"external_sample_file_readable_by_turnstone: {can_turnstone_read(base / 'AGENTS.md') if (base / 'AGENTS.md').exists() else can_turnstone_read(next((f for f in items if f.name != 'MEMORY.md'), index))}")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-path", type=Path)
    args = parser.parse_args()
    token = token_from_file()
    status, who = read_endpoint("/v1/api/auth/whoami", token)
    print(f"native_console_whoami: {status}")
    if status != 200:
        print("native_memory_status: unavailable until authenticated")
        return
    raw = who.get("permissions")
    if isinstance(raw, str):
        perms = set(raw.split(","))
        print(f"native_permission_admin_memories: {('YES' if 'admin.memories' in perms else 'NO')}")
    else:
        print("native_permission_admin_memories: NOT_REPORTED")
    status, data = read_endpoint("/v1/api/admin/memories?limit=1", token)
    print(f"native_admin_memories: {status}")
    if status == 200:
        print(f"native_db_memory_count: {data.get('total', 'UNKNOWN')}")
    status, health = read_endpoint("/v1/api/admin/memories/index-health", token)
    print(f"native_index_health: {status}")
    if status == 200:
        for key in ("over_budget", "max_entry_count", "invalid_description_count", "envelope_count"):
            print(f"native_index_{key}: {health.get(key, 'UNKNOWN')}")
    print("external_repository_candidates_checked:")
    candidates = (args.external_path,) if args.external_path else CANDIDATES
    present = [candidate for candidate in candidates if candidate.exists()]
    if not present:
        print("external_clone_location: NOT_FOUND_IN_KNOWN_CANDIDATES (not proof of absence)")
    for candidate in present:
        examine_reference(candidate)
    print("Read-only status complete. No memories modified or imported.")

if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, UnicodeError):
        print("REFUSED: Credential or probe error (details withheld)", file=sys.stderr)
        sys.exit(1)
