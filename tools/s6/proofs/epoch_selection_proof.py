#!/usr/bin/env python3
"""Proofs for S6 epoch SELECTION and epoch-scoped CAPABILITY — read only.

Two defects this pins down:

1. ``DEFAULT_EPOCH`` was a literal ``s6-t1``. A standing monitor that passes no
   ``--epoch`` therefore reports the *superseded* campaign forever, and the
   scheduled liveness watch was doing exactly that while the cluster ran
   ``5427ded`` against ``s6-t2``.

2. Capability was anchored to "the code running right now" for ANY epoch. Once
   the cluster moved to attachment-aware code, a report of the historical
   ``s6-t1`` epoch silently re-labelled 10 attachment-delivered turns as
   eligible work it had never been able to observe — 3/13 = 23% — and raised a
   suppression caveat against a sensor that was working correctly.

The correction is: an epoch is selected by its own authoritative boundary, and
its capability comes from its own evidence. Running code answers only for the
epoch that running code is writing to.

Every case below uses a synthetic epoch root or the real one read-only. Nothing
is written outside /var/tmp, no service is touched.

Usage: /opt/turnstone-venv/bin/python evidence/epoch_selection_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

S6 = pathlib.Path(__file__).resolve().parent.parent
VENV_PY = "/opt/turnstone-venv/bin/python"
REAL_ROOT = pathlib.Path("/var/lib/turnstone/sensor")

pass_n = 0
fail_n = 0


def ck(label, got, want):
    global pass_n, fail_n
    if got == want:
        print(f"PASS  {label}")
        pass_n += 1
    else:
        print(f"FAIL  {label}: got [{got}] want [{want}]")
        fail_n += 1


def ok(label, condition, detail=""):
    global pass_n, fail_n
    if condition:
        print(f"PASS  {label}")
        pass_n += 1
    else:
        print(f"FAIL  {label}{': ' + detail if detail else ''}")
        fail_n += 1


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect = load("s6collect", S6 / "collect.py")
liveness = load("s6liveness", S6 / "liveness.py")


def make_epoch(root: pathlib.Path, name: str, marker: str | None,
               instant: str | None, *, capability: str | None = None,
               conflict: bool = False) -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if marker and instant:
        (d / marker).write_text(instant)
    if conflict:
        (d / "T1").write_text("2026-10-18T01:00:00Z")
        (d / "T2").write_text("2026-10-18T02:00:00Z")
    if capability is not None:
        (d / "CAPABILITY").write_text(capability)
    return d


def turn(text="", *, attachment=False, attachment_work=False, substantive=False):
    return {"text": text, "has_attachment": attachment,
            "attachment_work": attachment_work, "substantive": substantive}


print("== 1. epoch selection is anchored to the authoritative boundary")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="s6epoch.", dir="/var/tmp"))
try:
    # Name order and mtime BOTH point at s6-t9; only its boundary is older.
    # A resolver that sorts by name or by directory mtime gets this wrong.
    make_epoch(tmp, "s6-t9", "T9", "2026-10-17T00:00:00Z")
    make_epoch(tmp, "s6-t2", "T2", "2026-10-18T13:21:51Z")
    make_epoch(tmp, "s6-t10", None, None)          # no boundary: not an epoch yet
    make_epoch(tmp, "s6-t11", None, None, conflict=True)  # corrupt: refuse
    # make the lexicographically-largest dir also the newest on disk
    import os
    stamp = datetime.now(timezone.utc).timestamp()
    os.utime(tmp / "s6-t9", (stamp, stamp))
    os.utime(tmp / "s6-t10", (stamp, stamp))
    os.utime(tmp / "s6-t11", (stamp, stamp))

    chosen, how, skipped = collect.resolve_epoch(root=tmp)
    ck("resolver picks s6-t2, the newest boundary", chosen.name, "s6-t2")
    ok("reason names the boundary it used", "2026-10-18T13:21:51Z" in how, how)
    ok("name order would have picked s6-t9", max(p.name for p in tmp.iterdir()) != "s6-t2")
    ok("the marker-less epoch is skipped, not guessed", "s6-t10" in " ".join(skipped), str(skipped))
    ok("the corrupt epoch is skipped, not silently preferred",
       any("s6-t11" in s for s in skipped), str(skipped))

    explicit = tmp / "s6-t9"
    chosen, how, _ = collect.resolve_epoch(str(explicit), root=tmp)
    ck("an explicit --epoch always wins", chosen, explicit)
    ck("an explicit choice is reported as such", how, "explicit")

    empty = tmp / "empty"
    empty.mkdir()
    try:
        collect.resolve_epoch(root=empty)
        ck("an empty root raises instead of guessing", "no exception", "LookupError")
    except LookupError as error:
        ok("an empty root raises instead of guessing", True)
        ok("its message names the boundary requirement",
           "boundary" in str(error).lower(), str(error))

    # The real root: this is the selection the scheduled watch inherits.
    real, how, _ = collect.resolve_epoch(root=REAL_ROOT)
    ck("the live root resolves to s6-t2", real.name, "s6-t2")
    ok("the live reason cites T2",
       "2026-10-18T13:21:51Z" in how, how)

    # the window helper itself: a superseded epoch must be bounded
    _s, cur_end = collect.epoch_window(tmp / "s6-t2")
    ck("the current epoch's window has no upper bound", cur_end, None)
    _s1, s1_end = collect.epoch_window(pathlib.Path(REAL_ROOT / "s6-t1"))
    ck("a superseded epoch's window ends at the next boundary",
       s1_end.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-10-18T13:21:51Z")

    print()
    print("== 2. capability is epoch-scoped, not 'whatever runs today'")
    cap_aware = make_epoch(tmp, "s6-t2b", "T2", "2026-10-18T13:21:51Z",
                           capability="capability.attachments_observed=1\n")
    cap_blind = make_epoch(tmp, "s6-t1b", "T1", "2026-10-18T03:43:02Z")

    # s6-t1: historical. It has no CAPABILITY record and no content_basis rows.
    # The attachment-aware code running NOW must not redefine its eligibility.
    got, why = liveness.detect_attachment_capability(
        cap_blind, explicit_basis=0, override="auto", units=[],
        current_epoch=cap_aware,
    )
    ck("a historical epoch stays text-only", got, False)
    ok("and says why without blaming the running code",
       "historical" in why.lower(), why)
    ok("it does not claim to have consulted live code",
       "running code" not in why, why)

    got, why = liveness.detect_attachment_capability(
        cap_aware, explicit_basis=0, override="auto", units=[],
        current_epoch=cap_aware,
    )
    ck("the current epoch trusts its own CAPABILITY record", got, True)
    ok("provenance names the record", "CAPABILITY" in why, why)

    got, why = liveness.detect_attachment_capability(
        cap_blind, explicit_basis=3, override="auto", units=[],
        current_epoch=cap_aware,
    )
    ck("epoch rows carrying content_basis prove capability on their own", got, True)

    got, why = liveness.detect_attachment_capability(
        cap_blind, explicit_basis=0, override="yes", units=[],
        current_epoch=cap_aware,
    )
    ck("an explicit override still wins", got, True)
    ck("and is labelled an override", why, "operator override")

    # Current epoch, no record, no rows, no locatable code -> fail closed.
    empty_cur = make_epoch(tmp, "s6-t3", "T3", "2026-10-18T14:00:00Z")
    got, why = liveness.detect_attachment_capability(
        empty_cur, explicit_basis=0, override="auto", units=[],
        current_epoch=empty_cur,
    )
    ck("a current epoch with no evidence fails closed", got, False)
    ok("and says it has no evidence", "no evidence" in why.lower(), why)

    # A CAPABILITY record that contradicts the epoch's own rows is a fault.
    liar = make_epoch(tmp, "s6-t4", "T4", "2026-10-18T14:10:00Z",
                      capability="capability.attachments_observed=0\n")
    got, why = liveness.detect_attachment_capability(
        liar, explicit_basis=5, override="auto", units=[],
        current_epoch=liar,
    )
    ck("a contradicting record fails closed", got, False)
    ok("and is labelled a conflict", "CONFLICT" in why, why)

    # A mixed cluster is a fault, whichever epoch asks.
    real_slots = liveness.live_code_slots
    aware = tmp / "slot-aware"
    plain = tmp / "slot-plain"
    for slot, seam in ((aware, True), (plain, False)):
        (slot / "turnstone" / "core").mkdir(parents=True, exist_ok=True)
        body = "def attachment_projection(attachments):\n    return {}\n" if seam else "x = 1\n"
        (slot / "turnstone" / "core" / "sensor_lifecycle.py").write_text(body)
    liveness.live_code_slots = lambda units: [str(aware), str(plain)]
    try:
        got, why = liveness.detect_attachment_capability(
            empty_cur, explicit_basis=0, override="auto", units=[],
            current_epoch=empty_cur,
        )
    finally:
        liveness.live_code_slots = real_slots
    ck("a mixed cluster fails closed", got, False)
    ok("and is named MIXED", "MIXED" in why, why)

    print()
    print("== 3. coverage is judged against an epoch-scoped denominator")
    # Historical, text-only: 3 plain + 10 attachment, 3 observed.
    turns_hist = ([turn("do the thing", substantive=True)] * 3
                  + [turn(".", attachment=True, attachment_work=True)] * 10)
    r = liveness.assess(
        acceptance_operator_observations=3, acceptance_operator_admitted=3,
        turns=turns_hist, elapsed_h=9.9, attachments_observed=False,
        basis_counts={"text_inferred": 3},
    )
    ck("historical eligibility excludes attachment turns", r["eligible_turns"], 3)
    ck("so 3 observations are full coverage", r["dimensions"]["coverage"]["ratio"], 1.0)
    ok("and the epoch is not failed either", r["verdict"] != "FAIL", r["verdict"])
    ck("the caveat is the blind spot, not a suppression alarm", len(r["warnings"]), 1)
    ok("the blind spot is still stated",
       any("attachment" in w and "excluded" in w for w in r["warnings"]), str(r["warnings"]))

    # Current, attachment-aware: 1 attachment turn, 1 observed, structural-only.
    turns_now = [turn(".", attachment=True, attachment_work=True)]
    r = liveness.assess(
        acceptance_operator_observations=1, acceptance_operator_admitted=1,
        turns=turns_now, elapsed_h=0.3, attachments_observed=True,
        basis_counts={"attachments_only": 1},
    )
    ck("attachment work is eligible on attachment-aware code",
       r["eligible_turns"], 1)
    ck("and it counts toward coverage", r["dimensions"]["coverage"]["ratio"], 1.0)
    ck("but not toward semantic grading", r["dimensions"]["grading"]["gradeable"], 0)
    ck("the structural observation is reported, not graded",
       r["dimensions"]["grading"]["structural_only"], 1)
    ok("the two dimensions are not merged",
       r["dimensions"]["grading"]["qualified_rate"] is None,
       str(r["dimensions"]["grading"]["qualified_rate"]))

    print()
    print("== 4. a real coverage shortfall is a FAILURE, not a caveat")
    turns_short = [turn("work", substantive=True)] * 3
    r = liveness.assess(
        acceptance_operator_observations=1, acceptance_operator_admitted=1,
        turns=turns_short, elapsed_h=2.0, attachments_observed=True,
        basis_counts={"text": 1},
    )
    ck("1 observed of 3 eligible is a failure verdict", r["verdict"], "FAIL")
    ok("the failure is stated as a coverage failure",
       any("independently verified" in f for f in r["failures"]), str(r["failures"]))
    ok("it is not merely a warning about suppression",
       not any("possible suppression" in w for w in r["warnings"]), str(r["warnings"]))

    r = liveness.assess(
        acceptance_operator_observations=3, acceptance_operator_admitted=3,
        turns=turns_short, elapsed_h=2.0, attachments_observed=True,
        basis_counts={"text": 3},
    )
    ck("full coverage is still healthy", r["verdict"], "HEALTHY")

    r = liveness.assess(
        acceptance_operator_observations=0, acceptance_operator_admitted=0,
        turns=turns_short, elapsed_h=2.0, attachments_observed=True,
        basis_counts={},
    )
    ck("zero observations of real work is still a failure", r["verdict"], "FAIL")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("== 5. the reports, run as the scheduled watch runs them")
env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/var/lib/turnstone",
       "PYTHONPATH": "/opt/turnstone-overlays/sensor-v187-5427ded"}


def run(script, *args):
    p = subprocess.run([VENV_PY, str(S6 / script), *args],
                       capture_output=True, text=True, env=env, timeout=120)
    return p.returncode, p.stdout, p.stderr


rc, out, err = run("liveness.py", "--json")          # watch argv: no --epoch
data = json.loads(out) if out.strip().startswith("{") else {}
ck("liveness with no --epoch targets s6-t2", data.get("epoch"), str(REAL_ROOT / "s6-t2"))
ck("and reads its T2 boundary", data.get("boundary_marker"), "T2")
ck("liveness exits 0 when the epoch is not failing", rc, 0)
ok("the selection is explained in the output",
   "boundary" in str(data.get("epoch_selection", "")).lower(),
   str(data.get("epoch_selection")))

rc, out, err = run("collect.py", "--json")           # watch argv: no --epoch
data = json.loads(out) if out.strip().startswith("{") else {}
ck("collector with no --epoch targets s6-t2", data.get("epoch"), str(REAL_ROOT / "s6-t2"))

# The historical epoch, explicitly requested, must keep its own semantics.
rc, out, err = run("liveness.py", "--epoch", str(REAL_ROOT / "s6-t1"), "--json")
hist = json.loads(out) if out.strip().startswith("{") else {}
ck("s6-t1 is still readable", hist.get("boundary_marker"), "T1")
ck("s6-t1 keeps its text-only capability", hist.get("attachments_observed"), False)
ok("s6-t1 does not inherit today's attachment seam",
   "historical" in str(hist.get("capability_source", "")).lower(),
   str(hist.get("capability_source")))
# The window closes at the NEXT boundary. Without that, an historical epoch
# absorbs every later turn and reads as a sensor that stopped working.
ck("the historical window closes at the superseding boundary",
   hist.get("window_end"), "2026-10-18T13:21:51Z")
ck("s6-t1's denominator is its 3 plain turns",
   hist.get("eligible_turns"), 3)
ck("s6-t1 reports full coverage of what it could see",
   hist.get("dimensions", {}).get("coverage", {}).get("ratio"), 1.0)
ok("s6-t1 is not accused of suppression",
   not any("possible suppression" in w for w in hist.get("warnings", [])),
   str(hist.get("warnings")))

print()
print("== 6. exit status carries the verdict")
tmp2 = pathlib.Path(tempfile.mkdtemp(prefix="s6fail.", dir="/var/tmp"))
try:
    d = tmp2 / "s6-t2"
    d.mkdir()
    (d / "T2").write_text("2026-10-18T03:43:02Z")   # real work exists after this
    for service in collect.SERVICE_DIRS:
        (d / service).mkdir()
    rc, out, err = run("liveness.py", "--epoch", str(d), "--json")
    data = json.loads(out) if out.strip().startswith("{") else {}
    ck("a silent sensor on real work is a FAIL", data.get("verdict"), "FAIL")
    ck("and exits 1 so the watch alerts", rc, 1)
    ok("the failure names the missing observations",
       any("not observing" in f or "independently verified" in f
           for f in data.get("failures", [])), str(data.get("failures")))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print("---")
print(f"pass={pass_n} fail={fail_n}")
sys.exit(0 if fail_n == 0 else 1)
