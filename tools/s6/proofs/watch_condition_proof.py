#!/usr/bin/env python3
"""Proofs for the UNATTENDED monitoring path: the scheduled watch that runs the
reporter with nobody watching.

A reporter that is correct is not enough. The watch decides whether a poll is
worth reporting, and its condition is evaluated by
``turnstone.core.watch.evaluate_condition``, which swallows ANY exception and
returns "did not fire". So a condition that indexes into parsed JSON turns every
reporter failure -- a crash, a lost database, a missing boundary -- into a
silently healthy poll.

This loads the condition STORED IN THE WATCH ROW and runs it against the real
evaluator, with the reporter's real exit codes and output shapes. It is
read-only (SELECT-only SQL; the watch row is not modified here).

Usage: /opt/turnstone-venv/bin/python evidence/watch_condition_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

OVERLAY = "/opt/turnstone-overlays/sensor-v187-5427ded"
WATCH_PY = pathlib.Path(OVERLAY) / "turnstone" / "core" / "watch.py"
S6 = pathlib.Path(__file__).resolve().parent.parent

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


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("== the evaluator the running node actually uses")
ok(f"watch module present in the live overlay ({OVERLAY})", WATCH_PY.is_file())
watch = load(WATCH_PY, "livewatch")


def fired(expr, output, exit_code, prev=None):
    """The EFFECTIVE decision, exactly as the runner makes it.

    evaluate_condition() returns (False, "condition error: ...") when the
    expression raises, and WatchRunner promotes that to a fire so a broken
    condition is never silently looped over. Testing only the expression would
    measure neither the promotion nor its absence, so this mirrors both.
    """
    fired_, reason = watch.evaluate_condition(expr, output, exit_code, prev)
    if not fired_ and reason.startswith("condition error:"):
        return True
    return fired_


def condition_only(expr, output, exit_code, prev=None):
    return watch.evaluate_condition(expr, output, exit_code, prev)[0]


# ---------------------------------------------------------------------------
print()
print("== the stored condition of the live liveness watch")
import psycopg  # noqa: E402

with psycopg.connect("dbname=turnstone") as conn:
    conn.execute("set transaction read only")
    row = conn.execute(
        "select watch_id, name, command, stop_on, interval_secs, max_polls,"
        " poll_count, active from watches where name like '%%liveness%%'"
    ).fetchone()

ok("a liveness watch exists", row is not None)
watch_id, name, command, expr, interval, max_polls, poll_count, active = row
print(f"     id={watch_id} name={name}")
print(f"     command: {command}")
print(f"     stop_on: {expr}")

ck("the watch is enabled", active, 1)
ck("it polls every 30 minutes", interval, 1800.0)
ok("it has polls left in its budget", poll_count < max_polls,
   f"{poll_count}/{max_polls}")

print()
print("== the command is the deployed reporter, with no pinned epoch")
ok("it runs the shipped liveness.py", command.endswith("s6/liveness.py --json"), command)
ok("it does not pin --epoch (so the resolver picks the live epoch)",
   "--epoch" not in command, command)
ok("it does not pin --capability (so the epoch's own record answers)",
   "--capability" not in command, command)

# ...and the command really works in the watch runner's environment.
print()
print("== the command, run the way the runner runs it")
import subprocess  # noqa: E402

envmod = load(pathlib.Path(OVERLAY) / "turnstone" / "core" / "env.py", "liveenv")
pid = subprocess.run(
    ["systemctl", "show", "turnstone-node@3", "-p", "MainPID", "--value"],
    capture_output=True, text=True,
).stdout.strip()
proc_env = {}
try:
    for entry in pathlib.Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            proc_env[key.decode()] = value.decode("utf-8", "replace")
except OSError:
    pass
scrubbed = envmod.scrubbed_env(extra={"TMPDIR": "/var/tmp"})
ck("the runner passes PYTHONPATH through its environment scrubber",
   scrubbed.get("PYTHONPATH"), OVERLAY)
ok("scrubbed PYTHONPATH names the live overlay, not the venv copy",
   scrubbed.get("PYTHONPATH", "").startswith("/opt/turnstone-overlays/"),
   scrubbed.get("PYTHONPATH", ""))

# The command is a fixed, operator-authored string with no interpolated input
# from a request, a user, or the network - it is the live watch runner being
# exercised on the live cluster. shell=True is what lets the same string be
# used for the shell pipeline the runner itself uses.
proc = subprocess.run(command, shell=True, capture_output=True, text=True,  # noqa: S602
                      env=scrubbed, cwd="/var/lib/turnstone", timeout=180)
ck("the command exits 0 on the live cluster", proc.returncode, 0)
# The runner appends stderr to stdout before parsing, so one diagnostic byte on
# stderr makes `data` None and fires the watch with a condition error. That
# happened on 2026-10-18: a skip note on stderr cancelled the live watch.
ck("the machine output is stderr-clean", proc.stderr.strip(), "")
live = json.loads(proc.stdout)
ck("it resolved the live epoch", live["epoch"], "/var/lib/turnstone/sensor/s6-t2")
ck("it used the epoch's own boundary marker", live["boundary_marker"], "T2")
ok("it read the conversation store (denominator is populated)",
   live["eligible_turns"] > 0, str(live["eligible_turns"]))
ok("it anchored capability on the epoch, not on the venv copy",
   "CAPABILITY record" in live["capability_source"], live["capability_source"][:80])

# ---------------------------------------------------------------------------
print()
print("== the condition must fire on every failure the reporter can report")
healthy = json.dumps({
    "verdict": "HEALTHY_WITH_CAVEATS",
    "acceptance": {"observations": {"met": False}, "elapsed": {"met": False},
                   "unmatched_ok": True, "no_duplicates_ok": True},
})
failing = json.dumps({
    "verdict": "FAIL",
    "failures": ["observed 1 of 4 independently verified eligible turns"],
    "acceptance": {"observations": {"met": False}, "elapsed": {"met": False},
                   "unmatched_ok": True, "no_duplicates_ok": True},
})
complete = json.dumps({
    "verdict": "HEALTHY",
    "acceptance": {"observations": {"met": True}, "elapsed": {"met": True},
                   "unmatched_ok": True, "no_duplicates_ok": True},
})
# The reporter's own error shapes: exit 2 with a message, a traceback, -1 on
# timeout. None of them is JSON, so `data` is None.
store_error = ("cannot read the conversation store: OperationalError: "
               "connection refused\n[stderr]\n")
traceback_text = "Traceback (most recent call last):\n  File \"liveness.py\"...\n"
timeout_text = "[command timed out after 30s]"

ck("a healthy poll with caveats does not fire", fired(expr, healthy, 0, healthy), False)
ck("valid JSON of an unexpected shape is not silently healthy",
   fired(expr, '{"unexpected": true}', 0, healthy), True)
ck("a FAIL verdict fires", fired(expr, failing, 1, healthy), True)
ck("a completed gate fires", fired(expr, complete, 0, healthy), True)
ck("a reporter setup error (exit 2, no JSON) fires",
   fired(expr, store_error, 2, healthy), True)
ck("a reporter crash (traceback, no JSON) fires",
   fired(expr, traceback_text, 1, healthy), True)
ck("a reporter timeout fires", fired(expr, timeout_text, -1, healthy), True)

# A non-zero exit is the reporter's own signal that it did not produce a
# trustworthy report. Whatever else the condition says, it must not ignore it.
ok("the condition consults exit_code", "exit_code" in (expr or ""), str(expr))
ok("it short-circuits on exit_code before indexing into parsed JSON",
   (expr or "").lstrip().startswith("exit_code"),
   (expr or "")[:40])
ck("non-zero exit WITH parseable healthy JSON still fires",
   condition_only(expr, healthy, 2, healthy), True)
ck("exit 0 with parseable healthy JSON does not fire",
   condition_only(expr, healthy, 0, healthy), False)

print()
print("== the alert text is interpretable without the reporter's stdout")
# The evaluator's reason is generic, so the dispatch body is the shell output.
# It must therefore contain the epoch, so a stale or wrong-epoch poll cannot be
# mistaken for the current monitoring result.
ok("the JSON identifies the epoch it reported", "epoch" in live, "missing")
ok("the JSON identifies the boundary marker", "boundary_marker" in live, "missing")

print("---")
print(f"pass={pass_n} fail={fail_n}")
sys.exit(0 if fail_n == 0 else 1)
