#!/usr/bin/env python3
"""PROOF: the epoch boundary has ONE authoritative spelling per epoch.

The near-miss this closes (2026-10-18): the activation script wrote
``<epoch>/T1`` while the handoff told the operator to read ``<epoch>/T2``. Both
were "right" about different things — the old name was hardcoded, the new epoch
is numbered 2 — and an operator following the handoff would have got
``cat: No such file or directory`` AFTER a successful six-service activation,
with nothing actually wrong.

The rule now:

  * the marker's FILENAME follows the epoch number (s6-t2 -> ``T2``);
  * its CONTENT is the authoritative UTC acceptance instant;
  * writers use the epoch's own name;
  * readers use ``collect.BOUNDARY_MARKERS`` and accept either name;
  * two markers that DISAGREE are a hard fault, and the slice fails closed.

This proof drives all four parties against synthetic epochs:

  1. collect.read_boundary()       the resolver
  2. collect.collect()             the collector's acceptance slice
  3. liveness.py                   the coverage monitor
  4. the activation script         its boundary_name()/boundary_file() helpers
  5. the root handoff text         the exact path it tells the operator to read

No telemetry, database or service is touched: every epoch is a scratch
directory and the liveness case stubs its store query.

Run: /opt/turnstone-venv/bin/python s6/evidence/boundary_marker_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent
SERVICE = "turnstone-node-2"

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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLLECT = load("s6collect_boundary", S6 / "collect.py")
LIVENESS = load("s6liveness_boundary", S6 / "liveness.py")

STAMP = "2026-10-18T06:00:00Z"
INSTANT = datetime.strptime(STAMP, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def make_epoch(root: pathlib.Path, name: str, marker: str | None, *,
               also: tuple[str, ...] = (), rows: bool = True) -> pathlib.Path:
    epoch = root / name
    (epoch / SERVICE).mkdir(parents=True)
    if marker:
        (epoch / marker).write_text(STAMP + "\n")
    for extra in also:
        (epoch / extra).write_text(STAMP + "\n")
    if rows:
        ts = (INSTANT + timedelta(seconds=10)).timestamp()
        cadence = [{"event": "operator_instruction", "event_id": "e1",
                    "outcome": "sensor_call", "ts": ts}]
        obs = [{"event": "operator_instruction", "event_id": "e1",
                "timestamp": ts + 1, "content_basis": "text",
                "signals": [{"name": n, "available": True}
                            for n in COLLECT.GRADED_SIGNALS]}]
        (epoch / SERVICE / "shadow-cadence.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in cadence))
        (epoch / SERVICE / "shadow-observations.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in obs))
    return epoch


def tree_digest(root: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            h.update(str(path.relative_to(root)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def boundary_name(epoch_name: str) -> str:
    """The shell helper's rule, re-derived from the script's source.

    Running the real function would need the whole script on the shell's path;
    instead the source is read and executed in isolation, so the proof fails if
    the implementation drifts from this rule.
    """
    source = (S6 / "activate-cluster-5427ded.sh").read_text()
    start = source.index("boundary_name() {")
    end = source.index("\n}", start) + 2
    script = (
        f"EPOCH=/var/lib/turnstone/sensor/{epoch_name}\n"
        + source[start:end]
        + "\nboundary_name\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    print("EPOCH BOUNDARY PROOF - one authoritative spelling per epoch")
    print("=" * 62)
    print(f"resolver precedence: {'/'.join(COLLECT.BOUNDARY_MARKERS)}")
    print()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s6-boundary-", dir="/var/tmp"))

    # ---- 1. the writer's rule --------------------------------------------
    print("1. WRITER - which filename does the activation script choose?")
    check("s6-t2 writes T2", boundary_name("s6-t2"), "T2")
    check("s6-t1 writes T1 (historical epochs unchanged)", boundary_name("s6-t1"), "T1")
    check("s6-t0 writes T0", boundary_name("s6-t0"), "T0")
    check("s6-t12 writes T12 (no single-digit assumption)", boundary_name("s6-t12"), "T12")

    # ---- 2. the resolver -------------------------------------------------
    print()
    print("2. RESOLVER - collect.read_boundary()")
    t2_epoch = make_epoch(tmp, "s6-t2", "T2")
    t1_epoch = make_epoch(tmp, "s6-t1", "T1")
    bare = make_epoch(tmp, "s6-nomarker", None)

    got, name, problem = COLLECT.read_boundary(t2_epoch)
    check("a T2 epoch resolves", (got, name, problem), (INSTANT, "T2", ""))
    got, name, problem = COLLECT.read_boundary(t1_epoch)
    check("a T1 epoch still resolves", (got, name, problem), (INSTANT, "T1", ""))
    check("no marker -> no boundary, no fault",
          COLLECT.read_boundary(bare), (None, "", ""))

    # Both present and EQUAL is not a fault: a migration may leave both.
    agree = make_epoch(tmp, "s6-agree", "T2", also=("T1",))
    check("two markers that agree resolve",
          COLLECT.read_boundary(agree)[:2], (INSTANT, "T2"))

    # Both present and DIFFERENT is a fault, and it fails closed.
    split = make_epoch(tmp, "s6-split", "T2")
    (split / "T1").write_text("2026-10-18T07:30:00Z\n")
    got, name, problem = COLLECT.read_boundary(split)
    check("two markers that disagree -> no boundary", got, None)
    check("...named as a conflict", problem.startswith("conflicting boundary markers"), True)
    check("...and the collector counts nothing as acceptance",
          COLLECT.collect(split)["unique_observations"], 0)
    check("...and fails the integrity gate",
          COLLECT.collect(split)["gate"]["boundary_ok"], False)
    check("...while a healthy epoch passes it",
          COLLECT.collect(t2_epoch)["gate"]["boundary_ok"], True)

    malformed = make_epoch(tmp, "s6-bad", "T2")
    (malformed / "T2").write_text("not-a-timestamp\n")
    check("an unparseable marker is a fault, not silence",
          COLLECT.read_boundary(malformed)[2],
          "T2 exists but is not a valid UTC timestamp")

    # ---- 3. the collector reports which file it used ---------------------
    print()
    print("3. COLLECTOR - acceptance slice and the marker it cites")
    r2 = COLLECT.collect(t2_epoch)
    r1 = COLLECT.collect(t1_epoch)
    check("T2 epoch: acceptance counted", r2["unique_observations"], 1)
    check("T2 epoch: marker named in the report", r2["boundary_marker"], "T2")
    check("T1 epoch: acceptance counted", r1["unique_observations"], 1)
    check("T1 epoch: marker named in the report", r1["boundary_marker"], "T1")
    check("the rendered report names the marker",
          "marker T2" in COLLECT.render(r2), True)
    check("a boundary fault is printed, not hidden",
          "BOUNDARY FAULT" in COLLECT.render(COLLECT.collect(split)), True)

    # ---- 4. liveness resolves the same way -------------------------------
    print()
    print("4. LIVENESS - same boundary, same acceptance slice")
    # Stub the conversation-store read: this proof is about the marker, not
    # about a database.
    # The reporter takes the epoch's half-open window (start, end); a
    # superseded epoch must not absorb later turns.
    LIVENESS.fetch_turns = lambda dsn, since, until=None: []
    # The stub takes the provenance the reporter passes, so the collector it
    # stands in for builds its gate the same way the real one does.
    LIVENESS.count_operator_observations = (
        lambda epoch, provenance=None: (
            1, 1, {"text": 1}, 1, COLLECT.collect(epoch, provenance=provenance))
    )
    for epoch_path, marker in ((t2_epoch, "T2"), (t1_epoch, "T1")):
        args = ["liveness.py", "--epoch", str(epoch_path), "--json"]
        code = LIVENESS.main.__globals__  # noqa: F841 - documentation only
        # main() parses sys.argv, so drive it the way an operator does.
        old_argv = sys.argv
        sys.argv = args
        try:
            import io
            from contextlib import redirect_stdout
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                rc = LIVENESS.main()
        finally:
            sys.argv = old_argv
        payload = json.loads(buffer.getvalue())
        check(f"liveness reads the {marker} epoch", payload["boundary_marker"], marker)
        check(f"liveness reports the {marker} instant",
              payload["boundary"], STAMP)
        check(f"liveness exits 0 on the {marker} epoch", rc, 0)

    # ---- 5. the handoff names a file that will exist ---------------------
    print()
    print("5. HANDOFF - the path the operator is told to read")
    handoff = (S6 / "ROOT-HANDOFF-5427ded.md").read_text()
    check("handoff reads the epoch's own marker",
          'cat "$EPOCH/T2"' in handoff, True)
    check("handoff no longer reads a stale T1 path",
          'cat "$EPOCH/T1"' in handoff, False)
    # And the resolver agrees that that is the file this epoch uses.
    check("handoff path matches the writer's rule for s6-t2",
          pathlib.Path("T2").name, boundary_name("s6-t2"))

    # ---- 6. end to end: simulate activation, then read with every tool ----
    print()
    print("6. END TO END - activation writes, every reader finds it")
    sim = tmp / "s6-t2-sim"
    (sim / SERVICE).mkdir(parents=True)
    # Exactly what the activation script's write path does:
    import subprocess as sp
    marker = boundary_name(sim.name)
    sp.run(["bash", "-c", f'date -u +%Y-%m-%dT%H:%M:%SZ > "{sim}/{marker}"'],
           check=True)
    written = (sim / marker).read_text().strip()
    check("activation created the file the handoff reads",
          (sim / "T2").is_file(), True)
    resolved, resolved_name, resolved_problem = COLLECT.read_boundary(sim)
    check("collector resolves the marker activation wrote",
          resolved_name, "T2")
    check("collector parses the instant activation wrote",
          resolved.strftime("%Y-%m-%dT%H:%M:%SZ"), written)
    check("no fault reported for a freshly written marker", resolved_problem, "")

    # The handoff's literal command must succeed against this tree.
    out = sp.run(["bash", "-c", f'EPOCH="{sim}"; cat "$EPOCH/T2"'],
                 capture_output=True, text=True)
    check("the handoff's cat command exits 0 after activation", out.returncode, 0)
    check("...and prints the same instant", out.stdout.strip(), written)

    # ---- 7. nothing was modified by any reader ---------------------------
    print()
    print("7. READ-ONLY - the readers changed nothing")
    before = tree_digest(t2_epoch)
    COLLECT.collect(t2_epoch)
    COLLECT.render(COLLECT.collect(t2_epoch))
    COLLECT.read_boundary(t2_epoch)
    LIVENESS.detect_attachment_capability(t2_epoch, explicit_basis=1)
    check("the epoch tree is byte-identical after reading",
          tree_digest(t2_epoch), before)

    shutil.rmtree(tmp, ignore_errors=True)
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
