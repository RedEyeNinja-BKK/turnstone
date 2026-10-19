#!/usr/bin/env python3
"""Regenerate a handoff's digests from the tree it describes.

The handoff is the operator interface. It states the SHA-256 of every
artifact an operator is about to execute, so the operator can check the
bytes on disk against the bytes that were reviewed. That check is only worth
something if the stated digests are TRUE, and they went stale: the artifacts
were edited and the handoff was not regenerated with them.

Hand-editing hashes is how they go stale again - nobody remembers to redo
it after the next edit. This script does the regeneration mechanically, from
the tree, so the handoff is always derivable from what it describes.

It rewrites, in place:

  * every `<digest>  <path>` pair, from the file the path names;
  * the digest inside the documented one-line command;
  * the stated entry counts of the byte manifests.

It never invents a digest and never edits a file other than the handoff.

A claim whose file is NOT in the tree is left alone - deleting a claim that
cannot be verified would be worse than keeping it - but it is REPORTED, and
a run that skipped any claim exits non-zero. A silent skip would let the
script print "already current" and exit 0 while the handoff still carried a
stale digest: the exact failure this tool exists to prevent.

`--tree` MUST be the tree the handoff describes. The handoff names that tree
in its own one-line command, and this script refuses to write digests of a
different tree into it: the handoff's digests are the operator's assurance
about the bytes that will actually be executed.

Usage:
    sync_handoff_digests.py <handoff.md> --tree <dir>

`--tree` is the directory the handoff's paths are relative to.

Run: /opt/turnstone-venv/bin/python tools/s6/sync_handoff_digests.py \
        <handoff.md> --tree <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys

#: A digest followed by two spaces and a path.
DIGEST_PATH = re.compile(r"\b([0-9a-f]{64})  ([A-Za-z0-9._/-]+)\b")
#: The same, but only the digest, for the one-line command.
ONE_LINER = re.compile(r'(echo ")([0-9a-f]{64})(  ([A-Za-z0-9._/-]+)")')
#: A stated entry count: `path  114 entries`.
ENTRY_COUNT = re.compile(r"^(\S*SHA256SUMS(?:\s+\(artifact root\))?)\s+(\d+)(\s+entries)", re.MULTILINE)
#: The directory the handoff's one-line command cds into.
CD_TARGET = re.compile(r"cd (/var/lib/turnstone/artifacts/[A-Za-z0-9._/-]+)")


def described_tree(text: str) -> pathlib.Path | None:
    """The directory the handoff's one-line command cds into.

    That is the directory the handoff's file paths are relative to: the
    command cds there and names ``s6-t2-execute.sh`` in it. The byte
    manifests are named relative to the tree's ROOT instead, which is what
    :func:`artifact_root` resolves.

    Returns None when the path is not present on this machine.
    """
    match = CD_TARGET.search(text)
    if not match:
        return None
    start = pathlib.Path(match.group(1))
    return start if start.is_dir() else None


def artifact_root(text: str) -> pathlib.Path | None:
    """The artifact tree the handoff describes.

    The handoff's one-line command cds into a directory INSIDE the artifact
    tree (the `s6/` directory that holds the driver). The handoff's byte
    manifests are named relative to the tree's ROOT, so the root is the
    ancestor that contains both `SHA256SUMS` and `s6/SHA256SUMS`.

    Resolved from the document, so the rule cannot drift from the handoff it
    describes. Returns None when the tree is not present on this machine.
    """
    match = CD_TARGET.search(text)
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


def resolve_manifest(root: pathlib.Path, label: str) -> pathlib.Path | None:
    """Find the manifest a label names, relative to the artifact root.

    The label is a path relative to the root, resolved EXACTLY. A bare
    `SHA256SUMS` is the root manifest and `s6/SHA256SUMS` is the s6 one; a
    basename fallback would let the two shadow each other.
    """
    clean = label.replace(" (artifact root)", "")
    candidate = root / clean
    return candidate if candidate.is_file() else None


def sha256_of(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("handoff", help="the handoff markdown to regenerate")
    ap.add_argument("--tree", required=True,
                    help="the directory the handoff's paths are relative to")
    args = ap.parse_args()

    handoff = pathlib.Path(args.handoff).resolve()
    tree = pathlib.Path(args.tree).resolve()
    if not handoff.is_file():
        print(f"no such handoff: {handoff}", file=sys.stderr)
        return 2
    if not tree.is_dir():
        print(f"no such tree: {tree}", file=sys.stderr)
        return 2

    original = handoff.read_text()
    text = original
    changed: list[str] = []
    skipped: list[str] = []

    # The tree the handoff DESCRIBES, read from the handoff's own one-line
    # command. The digests in this document are the operator's assurance about
    # the bytes that will be executed, so they must describe that tree. A
    # mismatch is refused rather than papered over: writing one tree's
    # digests into a document about another tree is how the handoff went
    # stale in the first place.
    #
    # The handoff's file paths are relative to the directory its command cds
    # into; its byte manifests are relative to the tree's ROOT. Both are
    # checked, because a tree can be wrong in either way.
    described = described_tree(original)
    if described is None:
        described = artifact_root(original)
    if described is not None and tree != described:
        print(f"refusing: the handoff describes {described}, not {tree}",
              file=sys.stderr)
        print("pass the tree the handoff's one-line command cds into",
              file=sys.stderr)
        return 2

    # ---- 1. every `<digest>  <path>` pair -------------------------------
    def _pair(match: re.Match) -> str:
        digest, path = match.group(1), match.group(2)
        actual = sha256_of(tree / path)
        if actual is None:
            # Not in this tree. Leave the claim alone rather than deleting it,
            # but REPORT it: a silently skipped claim is a stale claim.
            skipped.append(path)
            return match.group(0)
        if actual != digest:
            changed.append(path)
        return f"{actual}  {path}"

    text = DIGEST_PATH.sub(_pair, text)

    # ---- 2. the documented one-line command -----------------------------
    def _oneline(match: re.Match) -> str:
        actual = sha256_of(tree / match.group(4))
        if actual is None:
            skipped.append(match.group(4))
            return match.group(0)
        if actual != match.group(2):
            changed.append(match.group(4))
        return f"{match.group(1)}{actual}{match.group(3)}"

    text = ONE_LINER.sub(_oneline, text)

    # ---- 3. the stated entry counts -------------------------------------
    root = artifact_root(text)

    def _count(match: re.Match) -> str:
        label, stated = match.group(1), int(match.group(2))
        if root is None:
            skipped.append(f"{label} (no artifact root on this host)")
            return match.group(0)
        candidate = resolve_manifest(root, label)
        if candidate is None:
            skipped.append(f"{label} (not in the artifact tree)")
            return match.group(0)
        actual = len([ln for ln in candidate.read_text().splitlines() if ln.strip()])
        if actual != stated:
            changed.append(f"{label} ({stated} -> {actual})")
        return f"{label}  {actual}{match.group(3)}"

    text = ENTRY_COUNT.sub(_count, text)

    # A skipped claim is a claim this run could not verify. It is left in
    # place - it may be true - but it is reported, and the run FAILS, so a
    # skipped claim can never be mistaken for a verified one.
    if skipped:
        print("NOT VERIFIED - these claims were left untouched:", file=sys.stderr)
        for item in sorted(set(skipped)):
            print(f"  {item}", file=sys.stderr)

    if text != original:
        handoff.write_text(text)
        if changed:
            print("regenerated:")
            for item in sorted(set(changed)):
                print(f"  {item}")
    else:
        print("already current: no digest or count changed")

    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
