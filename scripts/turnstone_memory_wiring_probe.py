#!/usr/bin/env python3
"""Read-only checks of engineering memory source provenance and persona pointers.

Run on turnstone-vm:
  sudo python3 scripts/turnstone_memory_wiring_probe.py

Reads existing root-only ts_ token from the companion probe.
Only HTTP GETs, local file stat/reads and Git rev-parse/config lookups.
Does not echo persona prompts, secrets, native memory names or bodies.
Does not import public memory files into the native database.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from turnstone_memory_status_probe import read_endpoint, token_from_file

REPO = Path("/opt/turnstone-memory")
INDEX = REPO / "collections/turnstone/MEMORY.md"
MARKERS = ("/opt/turnstone-memory/", "turnstone-memory/collections/turnstone", "turnstone development memories")

def command(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=6,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def repo_state() -> None:
    print(f"external_repo_directory: {'PRESENT' if REPO.is_dir() else 'ABSENT'}")
    if not REPO.is_dir():
        return
    sha = command("git", "-C", str(REPO), "rev-parse", "--verify", "HEAD")
    print(f"external_git_commit: {sha if sha and len(sha) == 40 and all(x in '0123456789abcdef' for x in sha) else 'UNKNOWN'}")
    remote = command("git", "-C", str(REPO), "config", "--get", "remote.origin.url")
    # Do not display a configured Git remote because it can embed credentials.
    raw = remote or ""
    normalized = raw.removesuffix(".git").rstrip("/")
    allowed = (
        normalized == "git@github.com:eous/turnstone-memory"
        or (normalized.startswith(("https://", "http://")) and
            (lambda p: (p.hostname or "").lower() == "github.com" and p.path == "/eous/turnstone-memory")(urlsplit(normalized)))
    )
    print(f"external_origin_matches_eous_turnstone_memory: {'YES' if allowed else 'NO_OR_UNAVAILABLE'}")
    print(f"external_index_exists: {INDEX.is_file()}")
    if INDEX.is_file():
        st = INDEX.stat()
        print(f"external_index_bytes: {st.st_size}")
        # Check index contents, not just file existence. Never dump full text.
        with INDEX.open("r", encoding="utf-8", errors="replace") as inp:
            index_head = inp.read(4096)
        print(f"external_index_has_expected_heading: {'YES' if index_head.startswith('# turnstone memory') else 'NO'}")

def inspect_personas(token: str) -> None:
    status, result = read_endpoint("/v1/api/admin/personas", token)
    print(f"native_admin_personas: {status}")
    if status != 200:
        return
    personas = result.get("personas")
    if not isinstance(personas, list):
        print("native_personas_response_shape: UNEXPECTED")
        return
    print(f"native_personas_count: {len(personas)}")
    for p in personas:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if name not in ("engineer", "orchestrator"):
            continue
        prefix = "persona_" + name
        print(f"{prefix}_enabled: {p.get('enabled', 'UNKNOWN')}")
        print(f"{prefix}_default: {p.get('is_default', 'UNKNOWN')}")
        print(f"{prefix}_memory_enabled: {p.get('memory_enabled', 'UNKNOWN')}")
        tools = p.get("tool_allowlist")
        visible = "UNRESTRICTED" if tools is None else ("YES" if isinstance(tools, list) and "read_file" in tools else "NO_OR_UNKNOWN")
        print(f"{prefix}_read_file_visibility: {visible}")
        prompt = p.get("base_prompt")
        is_explicit = isinstance(prompt, str) and any(marker.lower() in prompt.lower() for marker in MARKERS)
        print(f"{prefix}_inline_prompt_external_memory_pointer: {'YES' if is_explicit else 'NO_OR_FILE_BACKED'}")
    print("NOTE: Presence/absence of inline markers cannot prove effective composed prompt for an already-created workstream.")

def inspect_native_pointer(token: str) -> None:
    status, result = read_endpoint("/v1/api/admin/memories?limit=25", token)
    print(f"native_admin_memory_metadata: {status}")
    if status != 200:
        return
    memories = result.get("memories")
    if not isinstance(memories, list):
        print("native_memory_list_shape: UNEXPECTED")
        return
    # Metadata only; do not fetch bodies or print names/descriptions.
    matches = 0
    for item in memories:
        if not isinstance(item, dict):
            continue
        fields = (str(item.get("name", "")), str(item.get("description", "")))
        if any("turnstone_memory" in x.lower() or "turnstone-memory" in x.lower() for x in fields):
            matches += 1
    total = result.get("total")
    print(f"native_total_memories: {total if isinstance(total, int) else 'UNKNOWN'}")
    print(f"native_external_pointer_metadata_candidates_in_page: {matches}")
    print("NOTE: A pointer may exist under another name or inside a memory body; this is a metadata-only hint.")

def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Root required for credential lookup")
    token = token_from_file()
    repo_state()
    inspect_personas(token)
    inspect_native_pointer(token)
    print("Read-only diagnostics complete. No memories, personas, or MCP servers changed.")

if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        print("REFUSED: Wiring preflight error (details withheld)", file=sys.stderr)
        sys.exit(1)
