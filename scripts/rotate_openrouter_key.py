#!/usr/bin/env python3
"""Rotate the OpenRouter API key across every definition that shares it.

WHY THIS EXISTS
    The OpenRouter key was exposed in a prior workstream transcript (see
    LC-RD-004 §5). This script is the prepared, reviewable replacement procedure
    so the change is a single reviewed operation when the operator authorizes it.
    It is NOT run automatically and changes nothing unless --apply is passed.

SCOPE
    Targets exactly the definitions whose base_url is openrouter.ai/api/v1 —
    the pre-existing GLM definition plus the four added in LC-RD-004. Other
    providers, aliases and definitions are never touched.

HOW THE NEW KEY IS SUPPLIED (choose one; never on argv)
    --key-file PATH    read the key from PATH (recommended: root-owned 0600)
    --key-stdin        read one line from stdin
    --new-key-env VAR  read from environment variable VAR

    Passing the key as a command-line argument is deliberately UNSUPPORTED: argv
    is world-readable via /proc/<pid>/cmdline and lands in shell history.
    A --new-key flag is therefore rejected outright.

WHAT IT DOES
    1. Lists the OpenRouter definitions (GET /v1/api/admin/model-definitions).
    2. Proves the NEW key works against OpenRouter directly (GET /models).
    3. PUTs the new key on each definition
       (PUT /v1/api/admin/model-definitions/{definition_id}, {"api_key": ...}).
    4. Runs real inference through Turnstone for each rotated alias to confirm
       the row still serves — a rotation that 401s mid-flight is caught here.

SAFETY
    - Dry-run by default: prints the plan and exits 0.
    - Prints key LENGTH and last 4 chars only — never the value.
    - The API audit row records ``api_key: "(updated)"``, never the secret.
    - Existing ids/aliases/base_urls are preserved; only api_key changes.

USAGE
    # plan only (default)
    python3 scripts/rotate_openrouter_key.py --config /etc/turnstone/config.toml \
        --key-file /srv/turnstone/credentials/openrouter/new.pat

    # execute after operator authorization
    python3 scripts/rotate_openrouter_key.py ... --apply
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import subprocess
import sys
import urllib.error
import urllib.request

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _resolve_token() -> str:
    """Read the Turnstone admin token the same way the bridge does."""
    for path in (
        os.environ.get("TURNSTONE_ADMIN_TOKEN_FILE", ""),
        "/srv/turnstone/credentials/api/turnstone_admin_token",
    ):
        if path and pathlib.Path(path).is_file():
            return pathlib.Path(path).read_text(encoding="utf-8").strip()
    raise SystemExit(
        "REFUSING: no admin token. Set TURNSTONE_ADMIN_TOKEN_FILE or provide the "
        "root-only credential file (this script is intended to run as root)."
    )


def _load_new_key(args) -> str:
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    elif args.new_key_env:
        key = os.environ.get(args.new_key_env, "").strip()
    elif args.key_file:
        p = pathlib.Path(args.key_file)
        if not p.is_file():
            raise SystemExit(f"REFUSING: key file not found: {p}")
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise SystemExit(
                f"REFUSING: {p} is mode {mode:04o}; it must not be group/other "
                "accessible (use 0600)."
            )
        key = p.read_text(encoding="utf-8").strip()
    else:
        raise SystemExit("REFUSING: supply the new key via --key-file/--key-stdin/--new-key-env.")
    if not key:
        raise SystemExit("REFUSING: the new key is empty.")
    return key


def _req(url: str, token: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def _check_new_key(key: str) -> tuple[bool, str]:
    """Prove the new key authenticates against OpenRouter before using it."""
    req = urllib.request.Request(
        f"{OPENROUTER_BASE}/key",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            info = json.loads(r.read() or b"{}").get("data") or {}
            # /key returns usage/limits metadata, not the secret.
            return True, f"login ok (label={info.get('label')!r}, usage={info.get('usage')})"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.environ.get("TURNSTONE_CONFIG", "/etc/turnstone/config.toml"))
    ap.add_argument("--console", default="http://127.0.0.1:8090")
    ap.add_argument("--key-file")
    ap.add_argument("--key-stdin", action="store_true")
    ap.add_argument("--new-key-env", metavar="VAR")
    ap.add_argument("--apply", action="store_true", help="actually perform the rotation")
    ap.add_argument("--skip-verify", action="store_true", help="skip post-rotation inference check")
    args = ap.parse_args()

    if any(a in ("--new-key", "--key") for a in sys.argv[1:]):
        raise SystemExit("REFUSING: keys must not be passed on the command line (argv is readable).")

    new_key = _load_new_key(args)
    base = args.console.rstrip("/")

    print("=== new key ===")
    print(f"  length={len(new_key)}  last4=...{new_key[-4:]}  (value never printed)")

    # Validate the replacement BEFORE resolving the Turnstone admin token or
    # touching any definition: a bad key must fail with zero side effects.
    ok, detail = _check_new_key(new_key)
    print(f"=== new key validated against OpenRouter: {ok} ({detail}) ===")
    if not ok:
        raise SystemExit("REFUSING: the new key does not authenticate. Nothing changed.")

    token = _resolve_token()

    status, listing = _req(f"{base}/v1/api/admin/model-definitions", token)
    if status != 200:
        raise SystemExit(f"REFUSING: could not list definitions (HTTP {status}).")
    rows = listing.get("model_definitions") or listing.get("models") or []
    targets = [r for r in rows if str(r.get("base_url", "")).rstrip("/") == OPENROUTER_BASE]

    if not targets:
        raise SystemExit("REFUSING: no OpenRouter definitions found — nothing to rotate.")

    print(f"\n=== rotation targets ({len(targets)}) ===")
    for r in targets:
        print(f"  {r['definition_id']}  {r.get('alias'):40s} {r.get('model')}")

    if not args.apply:
        print("\nDRY RUN — no changes made. Re-run with --apply after operator authorization.")
        return 0

    print("\n=== applying ===")
    results = []
    for r in targets:
        did, alias = r["definition_id"], r.get("alias")
        st, _ = _req(
            f"{base}/v1/api/admin/model-definitions/{did}",
            token,
            method="PUT",
            body={"api_key": new_key},
        )
        results.append((alias, st))
        print(f"  {alias:40s} HTTP {st}")

    failed = [a for a, st in results if st not in (200, 201)]
    if failed:
        print(f"\nFAILED for: {failed}", file=sys.stderr)
        return 1

    if args.skip_verify:
        print("\nRotation complete (post-rotation inference check skipped).")
        return 0

    print("\n=== post-rotation inference through Turnstone ===")
    harness = pathlib.Path(__file__).with_name("..") / "integrations/turnstone-self-api/tests/test_model_inference.py"
    cmd = [sys.executable, str(harness)] + [a for a, _ in results]
    env = {**os.environ, "TURNSTONE_CONFIG": args.config}
    rc = subprocess.call(cmd, env=env)
    print(f"\npost-rotation verification exit={rc} (0 = all aliases still serve)")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
