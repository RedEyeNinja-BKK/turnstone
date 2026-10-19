#!/usr/bin/env python3
"""NEGATIVE CONTROL for the two new proofs.

`rate_basis_proof.py` and `handoff_digest_proof.py` each assert a property of
the shipped tree. A property check that cannot fail is theatre, so each case
below MUTATES a copy of the tree in a disposable sandbox and requires the
proof to report a specific failure.

The shipped tree is never modified: every mutation happens on a copy under
/var/tmp, and the restore is from a python-held copy of the original bytes
(the same discipline the driver's own control uses).

The cases:
  rate-basis   the rate computed on the raw count instead of the gate's count
  rate-label   a text render that labels a raw count as the gate count
  coverage     the COVERAGE section calling the raw admitted count the gate
  empty-prov   an empty reconciliation treated as a real one
  handoff-sha  a handoff whose stated digest does not match the file
  handoff-cmd  a handoff whose documented one-liner no longer verifies
  handoff-count a handoff whose stated manifest entry count is wrong

Run: /opt/turnstone-venv/bin/python tools/s6/proofs/staleness_control.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent
PY = "/opt/turnstone-venv/bin/python"

PASS = 0
FAIL = 0


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


def proofs_dir(root: pathlib.Path) -> pathlib.Path:
    """Where the proofs live in this tree.

    The branch keeps them in `proofs/`; the deployed artifact keeps the same
    files in `evidence/`. Both layouts are supported, so the control runs
    against whichever tree it is placed in.
    """
    for candidate in (root / "proofs", root / "evidence", root):
        if (candidate / "rate_basis_proof.py").is_file():
            return candidate
    return root / "proofs"


def run_proof(name: str, root: pathlib.Path) -> tuple[int, str]:
    """Run a proof against a MUTATED tree, by pointing it at that tree.

    The proofs resolve the tree from their own location, so a mutated copy
    of the whole tools/s6 directory is the unit of mutation. The proof's
    stdout is captured so the control can assert WHICH check fired.
    """
    import io
    from contextlib import redirect_stdout

    spec = importlib.util.spec_from_file_location(
        f"mut_{name.replace('.', '_')}", proofs_dir(root) / name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the proof {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = module.main()
    return int(rc), buffer.getvalue()


def mutated_tree(tmp: pathlib.Path) -> pathlib.Path:
    """A disposable copy of tools/s6 that a case may edit."""
    dest = tmp / "s6"
    shutil.copytree(S6, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def main() -> int:
    print("NEGATIVE CONTROL - the new proofs actually bite")
    print("=" * 66)
    print()

    # ---- 0. the unmutated tree passes both proofs -------------------------
    print("0. POSITIVE - the unmutated tree passes")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        rc, out = run_proof("rate_basis_proof.py", tree)
        check("the rate basis proof passes the shipped tree", rc, 0)
        ok("...and reports no failures",
           "fail=0" in out, out[-300:])
        rc, out = run_proof("handoff_digest_proof.py", tree)
        check("the handoff digest proof passes the shipped tree", rc, 0)
        ok("...and reports no failures",
           "fail=0" in out, out[-300:])

    # ---- 1. the rate computed on the RAW count must be caught --------------
    print()
    print("1. RATE BASIS - a raw-count rate must be caught")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        collect = tree / "collect.py"
        original = collect.read_text()
        # Put the rate back on the raw count: the pre-fix behaviour.
        mutated = original.replace(
            "    obs_per_hour = ((attributable_observations / elapsed_h) if rateable else None)",
            "    obs_per_hour = ((unique_observations / elapsed_h) if rateable else None)",
        ).replace(
            "    remaining = max(0, GATE_OBSERVATIONS - attributable_observations)",
            "    remaining = max(0, GATE_OBSERVATIONS - unique_observations)",
        )
        ok("the mutation applied", mutated != original)
        collect.write_text(mutated)
        rc, out = run_proof("rate_basis_proof.py", tree)
        ok("a raw-count rate is rejected", rc != 0, f"rc={rc}")
        ok("...and the failure names the rate",
           "the rate is over the attributable count" in out, out[-600:])
        collect.write_text(original)  # python-held restore

    # ---- 2. a raw count labelled as the gate count must be caught ---------
    print()
    print("2. RATE LABEL - a raw count presented as the gate must be caught")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        collect = tree / "collect.py"
        original = collect.read_text()
        mutated = original.replace(
            '    lines.append(f"attributable  : {gate[\'observations\'][\'have\']} / {GATE_OBSERVATIONS}"\n'
            '                 "  (events that count toward the gate)")',
            '    lines.append(f"unique obs    : {result[\'unique_observations\']} / {GATE_OBSERVATIONS}")',
        )
        ok("the mutation applied", mutated != original)
        collect.write_text(mutated)
        rc, out = run_proof("rate_basis_proof.py", tree)
        ok("a raw count labelled as the gate is rejected", rc != 0, f"rc={rc}")
        ok("...and the failure names the label",
           "does not present the raw count as the gate count" in out, out[-600:])
        collect.write_text(original)

    # ---- 3. the COVERAGE section claiming the raw count gates must be caught
    print()
    print("3. COVERAGE LABEL - the raw admitted count must not be called the gate")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        collect = tree / "collect.py"
        original = collect.read_text()
        # Put the misleading claim back: the COVERAGE line calls the raw
        # admitted count the gate count.
        mutated = original.replace(
            '"  (dimension A events; NOT the gate count)")',
            '"  (dimension A events; these gate)")',
        )
        ok("the mutation applied", mutated != original)
        collect.write_text(mutated)
        rc, out = run_proof("rate_basis_proof.py", tree)
        ok("the coverage section calling the raw count the gate is rejected",
           rc != 0, f"rc={rc}")
        ok("...and the failure names the coverage claim",
           "does not call the raw admitted count the gate count" in out,
           out[-600:])
        collect.write_text(original)

    # ---- 4. an empty reconciliation treated as a real one must be caught ---
    print()
    print("4. EMPTY RECONCILIATION - it must not be treated as a real one")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        collect = tree / "collect.py"
        original = collect.read_text()
        # Go back to testing the reconciliation's TRUTHINESS: an empty
        # dict would then be treated as a real reconciliation while the
        # report fell back to the raw basis.
        mutated = original.replace(
            "    has_provenance = bool(provenance)",
            "    has_provenance = provenance is not None",
        )
        ok("the mutation applied", mutated != original)
        collect.write_text(mutated)
        rc, out = run_proof("rate_basis_proof.py", tree)
        ok("an empty reconciliation treated as real is rejected", rc != 0,
           f"rc={rc}")
        ok("...and the failure names the disagreement",
           "the basis label and the availability flag agree" in out, out[-600:])
        collect.write_text(original)

    # ---- 5. a stale digest in the handoff must be caught -------------------
    print()
    print("5. HANDOFF DIGEST - a stale stated digest must be caught")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        handoff = tree / "ROOT-HANDOFF-5427ded.md"
        original = handoff.read_text()
        # Corrupt the stated digest of collect.py: one hex character.
        mutated = re.sub(r"([0-9a-f]{64})(  collect\.py)",
                         lambda m: ("0" if m.group(1)[0] != "0" else "1")
                         + m.group(1)[1:] + m.group(2), original, count=1)
        ok("the mutation applied", mutated != original)
        handoff.write_text(mutated)
        rc, out = run_proof("handoff_digest_proof.py", tree)
        ok("a stale digest is rejected", rc != 0, f"rc={rc}")
        ok("...and the failure names the file",
           "collect.py matches its stated digest" in out, out[-600:])
        handoff.write_text(original)

    # ---- 4. a broken documented one-liner must be caught ------------------
    print()
    print("6. HANDOFF COMMAND - a command that no longer verifies must be caught")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        handoff = tree / "ROOT-HANDOFF-5427ded.md"
        original = handoff.read_text()
        mutated = re.sub(r'(echo ")([0-9a-f]{64})(  s6-t2-execute\.sh")',
                         lambda m: m.group(1) + ("0" * 64) + m.group(3),
                         original, count=1)
        ok("the mutation applied", mutated != original)
        handoff.write_text(mutated)
        rc, out = run_proof("handoff_digest_proof.py", tree)
        ok("a command that does not verify is rejected", rc != 0, f"rc={rc}")
        ok("...and the failure names the verify step",
           "verify step passes" in out, out[-600:])
        handoff.write_text(original)

    # ---- 5. a wrong entry count must be caught ----------------------------
    print()
    print("7. HANDOFF COUNT - a wrong manifest entry count must be caught")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        tmp = pathlib.Path(raw)
        tree = mutated_tree(tmp)
        handoff = tree / "ROOT-HANDOFF-5427ded.md"
        original = handoff.read_text()
        mutated = re.sub(r"(s6/SHA256SUMS\s+)(\d+)(\s+entries)",
                         lambda m: m.group(1) + str(int(m.group(2)) + 1)
                         + m.group(3), original, count=1)
        ok("the mutation applied", mutated != original)
        handoff.write_text(mutated)
        rc, out = run_proof("handoff_digest_proof.py", tree)
        ok("a wrong entry count is rejected", rc != 0, f"rc={rc}")
        ok("...and the failure names the count",
           "stated count for s6/SHA256SUMS" in out, out[-600:])
        handoff.write_text(original)

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
