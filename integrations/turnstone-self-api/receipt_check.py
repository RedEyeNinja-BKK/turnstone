#!/usr/bin/env python3
"""Validate a self-MCP smoke receipt: non-secret assertions + disclosure scan.

Usage: receipt_check.py /run/turnstone-self-mcp-smoke/receipt.txt

Asserts the operator's required smoke properties and refuses to pass if the
captured output contains anything credential-shaped.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TARGETS = ("console", "head", "worker_2", "worker_3", "worker_4", "worker_5")

REQUIRED = [
    ("MCP initialize handshake", "MCP handshake: PASS"),
    ("tool discovery + both tool names", "MCP tool discovery: PASS"),
    ("tool input schemas present", "MCP tool schemas: PASS"),
    ("zero writes", "zero writes performed"),
    ("clean overall result", "MCP smoke PASS"),
]

DISCLOSURE = [
    ("turnstone token", re.compile(r"ts_[0-9a-fA-F]{16,}")),
    ("bearer credential", re.compile(r"(?i)\bBearer[ \t]+\S{8,}")),
    ("gitlab-style token", re.compile(r"glsa_[A-Za-z0-9_-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: receipt_check.py <receipt.txt>")
        return 2
    p = Path(sys.argv[1])
    if not p.is_file():
        print(f"FAIL: receipt not found: {p}")
        return 2
    text = p.read_text(errors="replace")
    fails: list[str] = []

    print("=== required properties ===")
    for label, needle in REQUIRED:
        ok = needle in text
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            fails.append(label)

    print("\n=== per-target authenticated reads (expect 6/6) ===")
    hits = 0
    for t in TARGETS:
        ok = bool(re.search(rf"^{t}: MCP read .* HTTP 200 \(authenticated\)$", text, re.M))
        hits += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {t}")
    if hits != len(TARGETS):
        fails.append(f"authenticated reads {hits}/{len(TARGETS)}")

    print("\n=== credential-disclosure scan (must be empty) ===")
    leaked = False
    for label, rx in DISCLOSURE:
        m = rx.search(text)
        print(f"  [{'LEAK' if m else 'clean'}] {label}")
        if m:
            leaked = True
            fails.append(f"credential disclosed: {label}")
    if leaked:
        print("\n  REFUSING TO PASS: receipt contains credential-shaped content. Rotate per policy.")

    print("\n=== also assert no write verbs were attempted ===")
    if re.search(r"\b(POST|PUT|PATCH|DELETE)\b.*HTTP (200|201|204)", text):
        print("  [WARN] a mutating HTTP verb appears with a success status — investigate")
        fails.append("mutating verb succeeded in a read-only smoke")
    else:
        print("  [PASS] no successful mutating verb in receipt")

    print("\n=== RESULT ===")
    if fails:
        print("RECEIPT INVALID:")
        for f in fails:
            print("  -", f)
        return 1
    print("RECEIPT VALID: handshake, both tools, 6/6 authenticated reads, zero writes, no disclosure.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
