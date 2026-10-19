#!/usr/bin/env python3
"""PROOF: every digest the handoff states describes the bytes on disk.

The defect this closes (found 2026-10-20, while reading the handoff):

The handoff is the operator interface. Its first line is a single command
that verifies the driver's SHA-256 and then runs it:

    sudo bash -c 'cd .../s6 && echo "<sha>  s6-t2-execute.sh" | sha256sum -c - \
                  && bash s6-t2-execute.sh'

That check is the operator's ONLY protection against running a driver that
changed since it was reviewed. But the handoff was written once and the
artifacts moved on: the driver was edited (the hash chain was closed, the
collect/liveness pins were updated) and the handoff's digests were never
regenerated.

The result is the worst possible failure mode for an operator interface: the
documented command FAILS, before doing anything, with a checksum mismatch -
and the handoff offers no way to tell whether the driver is corrupt or the
document is stale. Both are true, and only one of them is a reason to stop.

Measured on 2026-10-20, the handoff stated a driver digest that matches NO
committed version of the driver, and the documented one-liner exited 1 with
"1 computed checksum did NOT match".

Four properties are asserted here, on the tree itself:

  1. EVERY DIGEST THE HANDOFF STATES DESCRIBES THE FILE NEXT TO IT.
  2. THE DOCUMENTED ONE-LINE COMMAND ACTUALLY PASSES against the real driver.
  3. THE HANDOFF'S STATED ENTRY COUNTS MATCH THE REAL FILES.
  4. THE HANDOFF'S REPORTER DIGESTS EQUAL THE DRIVER'S OWN PINS for the
     same files - one fact, stated once, in two places that cannot drift.

No telemetry, database or service is touched: the driver is only hashed and
the documented command's VERIFY step is run, never the driver itself.

Run: /opt/turnstone-venv/bin/python tools/s6/proofs/handoff_digest_proof.py
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys
from pathlib import Path

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent

PASS = 0
FAIL = 0

#: A digest followed by two spaces and a path, in the manifest format.
DIGEST_PATH = re.compile(r"\b([0-9a-f]{64})  ([A-Za-z0-9._/-]+)\b")

#: The driver pin block: `NAME_SHA=<digest>` on its own line.
DRIVER_PIN = re.compile(r"^([A-Z_]+_SHA)=([0-9a-f]{64})$", re.MULTILINE)

#: Which pin names which file, in the driver's own order.
PIN_TO_FILE = {
    "MANIFEST_SHA": "staging-5427ded/MANIFEST.sha256",
    "INSTALLER_SHA": "staging-5427ded/install-slot.sh",
    "ACTIVATE_SHA": "activate-cluster-5427ded.sh",
    "ROLLBACK_SHA": "rollback-cluster-5427ded.sh",
    "COLLECT_SHA": "collect.py",
    "LIVENESS_SHA": "liveness.py",
    "PROVENANCE_SHA": "provenance.py",
}


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        print(f"PASS  {label}")
        PASS += 1
    else:
        print(f"FAIL  {label}: got {got!r} want {want!r}")
        FAIL += 1


def ok(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"PASS  {label}")
        PASS += 1
    else:
        print(f"FAIL  {label}{': ' + detail if detail else ''}")
        FAIL += 1


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: The handoff states the byte manifests and their entry counts:
#:     s6/SHA256SUMS                   114 entries   sha256sum -c clean
#:     SHA256SUMS (artifact root)      140 entries   sha256sum -c clean
COUNT_LINE = re.compile(r"^(\S*SHA256SUMS(?:\s+\(artifact root\))?)\s+(\d+)\s+entries",
                         re.MULTILINE)
#: The artifact root, as the handoff's own one-line command states it.
ARTIFACT_ROOT = re.compile(r"cd (/var/lib/turnstone/artifacts/[A-Za-z0-9._/-]+)")


def _artifact_root(text: str) -> pathlib.Path | None:
    """The artifact tree the handoff points at, read from the handoff itself.

    The one-line command cds into the `s6/` directory inside the tree; the
    byte manifests are named relative to the tree's ROOT, so the root is the
    ancestor holding both `SHA256SUMS` and `s6/SHA256SUMS`.
    """
    match = ARTIFACT_ROOT.search(text)
    if not match:
        return None
    start = pathlib.Path(match.group(1))
    if not start.is_dir():
        return None
    for candidate in (start, *start.parents):
        if (candidate / "SHA256SUMS").is_file() and \
                (candidate / "s6" / "SHA256SUMS").is_file():
            return candidate
    return None


def _stated_counts(text: str) -> list[tuple[str, int]]:
    """The handoff's stated entry counts, as (label, count) pairs."""
    out: list[tuple[str, int]] = []
    for label, count in COUNT_LINE.findall(text):
        out.append((label, int(count)))
    return out


def _resolve_manifest(artifact_root: pathlib.Path, label: str) -> pathlib.Path | None:
    """Find the manifest a label names, relative to the artifact root.

    The label is a path relative to the artifact root, so it is resolved
    EXACTLY. A bare `SHA256SUMS` is the root manifest and `s6/SHA256SUMS` is
    the s6 one; falling back to a basename match would let the two shadow
    each other and silently compare the wrong file.
    """
    clean = label.replace(" (artifact root)", "")
    candidate = artifact_root / clean
    return candidate if candidate.is_file() else None


def main() -> int:
    print("HANDOFF DIGEST PROOF - the handoff describes the bytes on disk")
    print("=" * 66)
    print()

    handoff = S6 / "ROOT-HANDOFF-5427ded.md"
    driver = S6 / "s6-t2-execute.sh"
    ok("the handoff exists", handoff.is_file(), str(handoff))
    ok("the driver exists", driver.is_file(), str(driver))
    if not (handoff.is_file() and driver.is_file()):
        print()
        print("---")
        print(f"pass={PASS} fail={FAIL}")
        return 1

    text = handoff.read_text()

    # ---- 1. every stated digest describes the file next to it --------------
    print("1. STATED DIGESTS - each describes the bytes on disk")
    stated_digests = {}
    for digest, path in DIGEST_PATH.findall(text):
        stated_digests[path] = digest
    ok("the handoff states digests", len(stated_digests) > 0,
       str(sorted(stated_digests)))
    for path, digest in sorted(stated_digests.items()):
        target = S6 / path
        if not target.is_file():
            # A path that is not in this tree is not a claim about this tree.
            # It is reported rather than skipped in silence, so a claim that
            # stopped being checkable cannot pass unnoticed.
            print(f"NOT IN THIS TREE  {path}")
            continue
        actual = sha256_of(target)
        check(f"{path} matches its stated digest", actual, digest)

    # ---- 2. the documented one-liner's verify step passes ------------------
    print()
    print("2. THE DOCUMENTED COMMAND - its verify step passes")
    # The one-liner embeds the digest in an `echo "<sha>  <file>" | sha256sum -c -`.
    # Run ONLY that verify step, in a temp dir, against the real driver file.
    # The driver itself is never executed.
    one_liners = re.findall(r'echo "([0-9a-f]{64})  ([A-Za-z0-9._/-]+)"', text)
    ok("the handoff states the one-line command", len(one_liners) > 0,
       "no `echo \"<sha>  <file>\"` found")
    for digest, path in one_liners:
        target = S6 / path
        if not target.is_file():
            print(f"SKIP  {path} is not in this tree")
            continue
        actual = sha256_of(target)
        check(f"the command's stated digest matches {path}", actual, digest)
        # And the check itself, exactly as the operator would run it.
        proc = subprocess.run(
            ["sha256sum", "-c", "-"],
            input=f"{digest}  {path}\n",
            capture_output=True, text=True, cwd=str(S6),
        )
        check(f"the command's verify step passes for {path}",
              proc.returncode, 0)
        ok(f"the verify step reports OK for {path}",
           "OK" in proc.stdout, proc.stdout.strip() + proc.stderr.strip())

    # ---- 3. the stated entry counts match the real files -------------------
    print()
    print("3. ENTRY COUNTS - the handoff's numbers are the real numbers")
    # The handoff's byte manifests live in the ARTIFACT tree, not in this
    # checkout. The artifact root is derived from the handoff's own one-line
    # command, so the proof follows the document rather than a hardcoded path.
    #
    # A missing artifact tree is a FAILURE, not a skip: the property being
    # asserted is that the handoff's numbers are the real numbers, and a
    # silent SKIP would let this proof report success without ever checking
    # it. The proof is part of the qualification suite, so it must not go
    # green by not running.
    artifact_root = _artifact_root(text)
    if artifact_root is None:
        ok("the handoff's artifact tree is present to check against", False,
           "the handoff names no artifact tree, or it is not on this host")
    else:
        counts = _stated_counts(text)
        ok("the handoff states byte-manifest entry counts", len(counts) > 0,
           str(counts))
        for label, stated in counts:
            target = _resolve_manifest(artifact_root, label)
            if target is None:
                ok(f"the handoff's stated count for {label} has a real manifest",
                   False, f"{label} is not in {artifact_root}")
                continue
            actual_count = len(
                [ln for ln in target.read_text().splitlines() if ln.strip()]
            )
            check(f"the handoff's stated count for {label}", stated, actual_count)

    # ---- 4. the handoff and the driver agree on the reporter digests ------
    print()
    print("4. ONE FACT, TWO PLACES - the handoff and the driver agree")
    driver_text = driver.read_text()
    pins = dict(DRIVER_PIN.findall(driver_text))
    ok("the driver declares its pins", len(pins) > 0, str(sorted(pins)))
    for var, path in PIN_TO_FILE.items():
        if var not in pins:
            print(f"SKIP  the driver does not declare {var}")
            continue
        target = S6 / path
        if not target.is_file():
            print(f"SKIP  {path} is not in this tree")
            continue
        check(f"the driver's {var} matches {path}", sha256_of(target), pins[var])
        # The handoff states the same file's digest too. If the handoff states
        # it under a different path spelling, compare on the file name.
        by_name = {Path(p).name: d for p, d in stated_digests.items()
                   if Path(p).name == Path(path).name}
        if by_name:
            check(f"the handoff and the driver agree on {path}",
                  list(by_name.values())[0], pins[var])
        else:
            print(f"SKIP  the handoff does not state {path}")

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
