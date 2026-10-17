#!/usr/bin/env python3
"""Emit the exact additive authorized_keys onboarding commands for the Turnstone
management peers.

READ-ONLY AND LOCAL. This tool never connects anywhere. It prints:
  * the public key line that must be appended on each peer, and
  * an idempotent append command that cannot replace an existing file.

WHY A PRINTER AND NOT AN INSTALLER
  Installing a key requires writing to the remote account's authorized_keys,
  which is an operator action on that host (and, for system accounts, a
  privilege boundary this project does not cross automatically). Printing the
  exact command keeps the change reviewable and keeps the evidence local.

DESIGN RULES BAKED INTO THE COMMAND
  * `grep -qxF` before appending => idempotent; re-running is harmless.
  * `>>` only, never `>` => existing entries and existing management accounts
    are preserved. This is the operator's standing constraint.
  * no sudoers, no ownership, no mode changes, no `authorized_keys` rewrite.
  * the public half only; the private key never leaves the canonical store.

Usage:
    python3 scripts/turnstone_ssh_onboarding.py            # commands
    python3 scripts/turnstone_ssh_onboarding.py --check    # local readiness
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import stat
import subprocess

KEY_DIR = pathlib.Path("/srv/turnstone/credentials/ssh/keys")
PEERS = (
    # peer, key basename, tailnet address, pinned-hostname-in-known_hosts
    ("agent-vm", "turnstone-agent-vm-ed25519", "100.127.26.70"),
    ("switchyard-vm", "turnstone-switchyard-vm-ed25519", "100.65.106.53"),
)


def _pubkey(peer: str, base: str) -> str:
    path = KEY_DIR / f"{base}.pub"
    if not path.is_file():
        raise SystemExit(f"missing public key: {path}")
    line = path.read_text(encoding="utf-8").strip()
    parts = line.split()
    if len(parts) < 2 or not parts[0].startswith("ssh-ed25519"):
        raise SystemExit(f"unexpected public key format: {path}")
    # Normalise the trailing comment so the entry is identifiable on the peer.
    return f"{parts[0]} {parts[1]} {peer}-turnstone-mgmt"


def _fingerprint(base: str) -> str:
    proc = subprocess.run(  # noqa: S603
        ["ssh-keygen", "-lf", str(KEY_DIR / f"{base}.pub")],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = proc.stdout.split()
    return parts[1] if len(parts) >= 2 else "?"


def check() -> int:
    ok = True
    for peer, base, ip in PEERS:
        priv = KEY_DIR / base
        pub = KEY_DIR / f"{base}.pub"
        problems: list[str] = []
        if not priv.is_file():
            problems.append("private key missing")
        else:
            mode = stat.S_IMODE(priv.stat().st_mode)
            if mode != 0o600:
                problems.append(f"private key mode {oct(mode)} != 0600")
        if not pub.is_file():
            problems.append("public key missing")
        if not shutil.which("ssh"):
            problems.append("ssh client absent")
        status = "OK" if not problems else "; ".join(problems)
        if problems:
            ok = False
        print(f"{peer:<14} {ip:<16} {_fingerprint(base)}  {status}")
    print()
    print("KEYS_READY" if ok else "KEYS_NOT_READY")
    print("note: a present key is not access; remote authorization is separate")
    return 0 if ok else 2


def emit() -> int:
    print("# Additive authorized_keys onboarding for the Turnstone management peers.")
    print("# Run these on EACH PEER, as the account that should accept the key.")
    print("# Nothing here replaces an existing file or modifies sudoers.")
    print()
    for peer, base, ip in PEERS:
        key = _pubkey(peer, base)
        print(f"# ---- {peer} ({ip}) — client key {_fingerprint(base)}")
        print(f"# private key stays at {KEY_DIR / base} and is never copied")
        print("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        print("touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys")
        # Inline, never via a shell variable: a NAME=... assignment trips secret
        # scanners and is redacted in transcripts, which destroys the command the
        # operator needs. A public key is not a secret.
        print(f"grep -qxF '{key}' ~/.ssh/authorized_keys \\")
        print(f"  || printf '%s\\n' '{key}' >> ~/.ssh/authorized_keys")
        print('echo "entries now: $(wc -l < ~/.ssh/authorized_keys)"')
        print()
    print("# Verification FROM turnstone-vm (strict host checking, explicit identity):")
    for _peer, base, ip in PEERS:
        print(
            f'ssh -i {KEY_DIR / base} -o IdentitiesOnly=yes -o BatchMode=yes '
            f'-o StrictHostKeyChecking=yes turnstone@{ip} true'
        )
    print()
    print("# A successful `true` proves authentication only. Do not infer sudo.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="local key readiness only")
    args = parser.parse_args(argv)
    return check() if args.check else emit()


if __name__ == "__main__":
    raise SystemExit(main())
