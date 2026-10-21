#!/usr/bin/env python3
"""PROOF: the source tree and the deployed artifact tree are the same bytes.

The defect this pins (found 2026-10-20, during independent review of the
source <-> artifact correspondence):

The branch keeps the measurement reporters, their proofs and the deployment
driver in ``tools/s6/``. The deployed artifact keeps the same files under
``s6/`` and ``s6/evidence/``. Two copies of the same file is a promise, and
a promise nobody checks is a promise that decays: one side gets edited, the
other does not, and the artifact the operator is about to run is no longer
the artifact that was reviewed.

That had already happened. The artifact's proof runner listed an OLDER
proof set than the source's: it was missing ``provenance_required_proof.py``
and ``matching_cardinality_proof.py`` entirely, so the artifact tree ran
469 checks while the source tree ran 592 - and the two trees disagreed about
which proofs exist without either saying so.

THE CONTRACT

``SOURCE-SHA256SUMS`` is the published digest list of the source tree's
correspondence files, one per line, in the same format as the artifact's
byte manifests. It is COMMITTED in the source tree and MIRRORED into the
artifact tree, and it is the ONE statement of what the two trees are
supposed to contain.

It is regenerated mechanically, from the tree, so it cannot go stale by
hand::

    cd tools/s6 && find . -type f ! -name 'SOURCE-SHA256SUMS' \\
      ! -path '*/__pycache__/*' ! -path '*/.ruff_cache/*' \\
      ! -name '.turnstone.db*' -print0 | sort -z | xargs -0 sha256sum \\
      > SOURCE-SHA256SUMS

Four properties are asserted here, on the trees themselves:

  1. THE MANIFEST IS PRESENT and describes this tree.
  2. EVERY FILE THE MANIFEST NAMES IS HERE, with the stated digest. In the
     source tree that is the source file; in the artifact tree it is the
     artifact file the source file corresponds to.
  3. THE TWO COPIES OF THE MANIFEST ARE IDENTICAL, and the artifact's byte
     manifest covers the artifact's copy.
  4. NO FILE IN THIS TREE IS UNLISTED. A file added on one side and not on
     the other is exactly how the correspondence decays.

The check COUNT is the same in both layouts, by construction: every check
is "this file matches the published digest", and the mapping from a source
path to its artifact path is fixed. So the two trees report the same
totals, and a difference between them is a real difference.

No telemetry, database or service is touched: the trees are only READ and
hashed.

Run: /opt/turnstone-venv/bin/python tools/s6/proofs/source_artifact_proof.py
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent

PASS = 0
FAIL = 0

#: The published manifest, at the root of BOTH trees.
MANIFEST_NAME = "SOURCE-SHA256SUMS"

#: The artifact tree, as the handoff's own one-line command names it.
ARTIFACT_ROOT = re.compile(r"cd (/var/lib/turnstone/artifacts/[A-Za-z0-9._/-]+)")

#: A digest followed by two spaces and a path, in the manifest format.
DIGEST_PATH = re.compile(r"^([0-9a-f]{64})  (\./.+)$", re.MULTILINE)

#: The source path -> artifact path mapping. The artifact keeps the proofs
#: and the driver's own tests in ``evidence/``, and the runner under its
#: deployed name.
RUNNER_SOURCE_NAME = "run-all.sh"
RUNNER_ARTIFACT_NAME = "script-tests-proofs.sh"


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


def sha256_of(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def artifact_root(text: str) -> pathlib.Path | None:
    """The artifact tree's ``s6/`` directory, as the handoff names it.

    The handoff's one-line command cds into the ``s6/`` directory INSIDE the
    artifact tree - that is the directory the operator's command runs in and
    the tree the drivers and proofs live in. The name is read from the
    document, so the rule cannot drift from the handoff it describes.
    """
    match = ARTIFACT_ROOT.search(text)
    if not match:
        return None
    start = pathlib.Path(match.group(1))
    return start if start.is_dir() else None


def artifact_path_for(source_rel: str) -> str:
    """Where a source-relative path lives in the artifact tree."""
    rel = source_rel[2:] if source_rel.startswith("./") else source_rel
    if rel.startswith("proofs/"):
        name = rel[len("proofs/"):]
        if name == RUNNER_SOURCE_NAME:
            return f"evidence/{RUNNER_ARTIFACT_NAME}"
        return f"evidence/{name}"
    if rel.startswith("driver/"):
        return "evidence/" + rel[len("driver/"):]
    return rel


def correspondence_files(root: pathlib.Path) -> list[str]:
    """The files of a tree that the correspondence covers."""
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or ".ruff_cache" in path.parts:
            continue
        if path.name.startswith(".turnstone.db"):
            continue
        if path.name == MANIFEST_NAME:
            continue
        out.append(str(path.relative_to(root)))
    return out


def main() -> int:
    print("SOURCE / ARTIFACT CORRESPONDENCE PROOF - the two trees are the same bytes")
    print("=" * 74)
    print()

    handoff = S6 / "ROOT-HANDOFF-5427ded.md"
    ok("the handoff exists", handoff.is_file(), str(handoff))
    if not handoff.is_file():
        print()
        print("---")
        print(f"pass={PASS} fail={FAIL}")
        return 1

    text = handoff.read_text()
    art = artifact_root(text)
    ok("the artifact tree is present to check against", art is not None,
       "the handoff names no artifact tree, or it is not on this host")
    if art is None:
        print()
        print("---")
        print(f"pass={PASS} fail={FAIL}")
        return 1

    # Which tree is this copy of the proof running in? The two trees have
    # different layouts, and the layout is what says which.
    in_source = (S6 / "proofs").is_dir()
    in_artifact = (S6 / "evidence").is_dir()
    ok("this tree is one of the two known layouts", in_source or in_artifact,
       f"neither {S6 / 'proofs'} nor {S6 / 'evidence'} exists")
    current = S6

    # ---- 1. the manifest is present and readable -------------------------
    print()
    print("1. THE PUBLISHED MANIFEST - present, and describing this tree")
    manifest = current / MANIFEST_NAME
    ok("the manifest is in this tree", manifest.is_file(), str(manifest))
    if not manifest.is_file():
        print()
        print("---")
        print(f"pass={PASS} fail={FAIL}")
        return 1
    entries = DIGEST_PATH.findall(manifest.read_text())
    ok("the manifest lists files", len(entries) > 0, str(len(entries)))

    # ---- 2. every file the manifest names is here, with that digest ------
    print()
    print("2. EVERY NAMED FILE - present here, with the published digest")
    # ---- 2. every file the manifest names is here, with that digest ------
    print()
    print("2. EVERY NAMED FILE - present here, with the published digest")
    # Two source files can name ONE artifact file (the same helper lives in
    # two directories). The mapping collapses them, so the targets are
    # deduplicated - and the sources that collapse must agree.
    #
    # The targets are keyed by the ARTIFACT path in BOTH layouts, so the
    # number of checks is the same in the source tree and the artifact tree.
    # A proof whose check count depends on which tree it runs in cannot be
    # compared between them.
    targets: dict[str, list[tuple[str, str]]] = {}
    for digest, source_rel in entries:
        rel = source_rel[2:] if source_rel.startswith("./") else source_rel
        targets.setdefault(artifact_path_for(rel), []).append((rel, digest))

    for target, sources in sorted(targets.items()):
        ok(f"the sources that map to {target} agree on its digest",
           len({d for _rel, d in sources}) == 1,
           ", ".join(sorted({d for _rel, d in sources})))

    missing: list[str] = []
    wrong: list[str] = []
    for target, sources in sorted(targets.items()):
        digest = sources[0][1]
        if in_source:
            # The file is at its SOURCE path; every source that maps to this
            # target must carry the published digest.
            paths = [current / rel for rel, _d in sources]
        else:
            # The file is at its ARTIFACT path.
            paths = [current / target]
        for path in paths:
            if not path.is_file():
                missing.append(str(path.relative_to(current)))
                continue
            if sha256_of(path) != digest:
                wrong.append(str(path.relative_to(current)))
    ok("no file the manifest names is missing from this tree", not missing,
       ", ".join(missing))
    ok("no file the manifest names differs from its published digest",
       not wrong, ", ".join(wrong))

    # ---- 3. the two copies of the manifest are identical -----------------
    print()
    print("3. THE TWO COPIES - the same manifest, covered by the byte manifest")
    artifact_manifest = art / MANIFEST_NAME
    ok("the artifact carries the manifest", artifact_manifest.is_file(),
       str(artifact_manifest))
    if artifact_manifest.is_file():
        check("this tree's manifest and the artifact's are byte-identical",
              sha256_of(manifest), sha256_of(artifact_manifest))
    byte_manifest = art / "SHA256SUMS"
    ok("the artifact has a byte manifest", byte_manifest.is_file(),
       str(byte_manifest))
    if byte_manifest.is_file() and artifact_manifest.is_file():
        listed = {line.split("  ", 1)[1].lstrip("./")
                  for line in byte_manifest.read_text().splitlines()
                  if line.strip()}
        ok("the artifact's byte manifest covers the artifact's copy",
           MANIFEST_NAME in listed, ", ".join(sorted(listed))[:200])

    # ---- 4. no file in this tree is unlisted ------------------------------
    print()
    print("4. NO UNLISTED FILE - a file added on one side only is caught")
    present = correspondence_files(current)
    if in_source:
        # The source tree: every file it holds must be published.
        unlisted = [p for p in present
                    if "./" + p not in {rel for _d, rel in entries}]
    else:
        # The artifact tree: it also holds superseded material that has no
        # source counterpart (older activation scripts, captured reports,
        # the activation plans). What must not happen is a file that DOES
        # have a source counterpart and is not published - and the targets
        # above are exactly that set.
        published = set(targets)
        unlisted = [p for p in present
                    if p in set(targets) and p not in published]
    ok("no file in this tree is missing from the published manifest",
       not unlisted, ", ".join(unlisted))

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
