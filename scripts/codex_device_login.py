#!/usr/bin/env python3
"""Codex (ChatGPT) device-code login for turnstone-vm — operator-run.

WHY THIS EXISTS
    Turnstone 1.8.7 has no Codex/ChatGPT auth mode (``MODEL_AUTH_MODES`` is
    ``{static, entra_obo, entra_app, rfc8693_obo}``) and the host had no Codex
    tooling at all. The official ``openai-codex`` SDK ships its runtime as a
    static binary, so Codex login IS possible here — it just is not the same
    thing as a Turnstone model provider. See LC-RD-005 §2 for that determination.

WHAT THIS DOES
    Starts the ChatGPT device-code flow and prints ONLY the verification URL and
    the user code — never a token. Completion requires the operator's browser.
    On success Codex stores its own credentials under ``CODEX_HOME``.

SETUP (once)
    python3 -m venv /var/lib/turnstone/github/codex-venv
    /var/lib/turnstone/github/codex-venv/bin/pip install openai-codex

USAGE
    # start the flow; prints URL + code, then waits for your approval
    /var/lib/turnstone/github/codex-venv/bin/python scripts/codex_device_login.py

    # status only (after a successful login)
    /var/lib/turnstone/github/codex-venv/bin/python scripts/codex_device_login.py --status

    # optional isolation: keep Codex state out of $HOME
    CODEX_HOME=/var/lib/turnstone/github/codex-home ... scripts/codex_device_login.py

SAFETY
    No token, access token or API key is ever printed. ``--logout`` exists so the
    credential can be removed from this host without hunting for files.
"""
from __future__ import annotations

import argparse
import os
import sys


def _account_summary(codex) -> str:
    """Best-effort, non-secret account summary (never raises)."""
    try:
        acct = codex.account()
    except Exception as exc:  # noqa: BLE001 - status must not crash the tool
        return f"unavailable ({type(exc).__name__})"
    inner = getattr(acct, "account", acct)
    if inner is None:
        return "not logged in"
    # Account objects carry identity/plan metadata, not secrets. Keep it short
    # and never echo anything that looks like a credential.
    fields: list[str] = []
    for name in ("email", "plan_type", "account_id", "account"):
        val = getattr(inner, name, None)
        if val:
            fields.append(f"{name}={val}")
    return ", ".join(fields) if fields else str(inner)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="print login status and exit")
    ap.add_argument("--logout", action="store_true", help="remove stored Codex credentials")
    ap.add_argument(
        "--no-wait",
        action="store_true",
        help="print the URL and code, then exit WITHOUT waiting for approval",
    )
    args = ap.parse_args()

    try:
        from openai_codex import Codex
    except ImportError:
        print(
            "ERROR: openai-codex is not installed for this interpreter.\n"
            "  python3 -m venv /var/lib/turnstone/github/codex-venv\n"
            "  /var/lib/turnstone/github/codex-venv/bin/pip install openai-codex",
            file=sys.stderr,
        )
        return 2

    print(f"CODEX_HOME = {os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')}")

    with Codex() as codex:
        if args.logout:
            codex.logout()
            print("logged out; stored credentials removed")
            return 0

        print(f"account before: {_account_summary(codex)}")

        if args.status:
            return 0

        login = codex.login_chatgpt_device_code()
        url = getattr(login, "verification_url", None)
        code = getattr(login, "user_code", None)

        print()
        print("=" * 68)
        print("  OPERATOR ACTION")
        print(f"    1. open: {url}")
        print(f"    2. enter code: {code}")
        print("  These are not secrets, but they expire quickly.")
        print("=" * 68)
        print()

        if args.no_wait:
            print("--no-wait: exiting without waiting. Re-run to get a fresh code.")
            return 0

        print("waiting for approval in your browser (Ctrl-C to abort)...")
        try:
            result = login.wait()
        except KeyboardInterrupt:
            print("\naborted before approval; no credentials stored.")
            return 130

        if getattr(result, "success", False):
            print(f"LOGIN OK — account now: {_account_summary(codex)}")
            return 0
        print(f"LOGIN FAILED — {getattr(result, 'error', 'no detail')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
