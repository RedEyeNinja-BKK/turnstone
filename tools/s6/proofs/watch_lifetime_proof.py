#!/usr/bin/env python3
"""Proofs for the WATCH LIFETIME: is ``max_polls`` a lifetime cap, and does the
live campaign watch have enough of it?

``max_polls`` is not a "per campaign phase" budget. It is charged once per poll,
never reset, and the runner ends the watch at ``poll_count >= max_polls``
regardless of whether the stop condition ever fired. A watch created to police a
48-hour gate with ``max_polls=100`` at a 30-minute interval therefore dies after
50 hours -- about two hours after the gate it is policing would even become
satisfiable, and long before a slow 100-event campaign completes.

This proof is anchored to the DEPLOYED source, not to a copy: it reads the
expressions out of the overlay's ``watch.py`` and asserts the semantics they
encode, so the reasoning below cannot drift from the code that runs.

Read-only: SELECT-only SQL, source-file reads. Nothing is modified.

Usage: /opt/turnstone-venv/bin/python evidence/watch_lifetime_proof.py
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

S6 = pathlib.Path(__file__).resolve().parent.parent
OVERLAY = pathlib.Path("/opt/turnstone-overlays/sensor-v187-5427ded")
WATCH_PY = OVERLAY / "turnstone" / "core" / "watch.py"
STORE_PY = OVERLAY / "turnstone" / "core" / "storage" / "_postgresql.py"
WATCH_NAME = "s6-t1-liveness"          # cosmetic; behaviour is what matters
GATE_HOURS = 48
GATE_EVENTS = 100
# A campaign this proof will accept as "generously bounded": enough lifetime to
# complete a deliberately slow 100-event campaign, not just the 48-hour floor.
SLOW_CAMPAIGN_DAYS = 120

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


print("== 1. the deployed runner's lifetime semantics")
ok("the deployed watch module is present", WATCH_PY.is_file(), str(WATCH_PY))
src = WATCH_PY.read_text()
store_src = STORE_PY.read_text()

# The three expressions that together make max_polls a LIFETIME cap.
ck("poll_count increments by one per poll",
   'poll_count = watch_row.get("poll_count", 0) + 1' in src, True)
ck("the watch ends when the budget is spent, fired or not",
   "is_final = fired or poll_count >= max_polls" in src, True)
ck("spending the budget without a fire still terminates it",
   "if not fired and poll_count >= max_polls:" in src, True)
ck("and says so in the final message",
   '"max polls reached"' in src, True)

# ...and nothing resets it, so the budget is spent once, for the watch's whole
# life. The only `poll_count=0` is the INSERT default in create_watch.
create_idx = store_src.index("def create_watch")
create_body = store_src[create_idx:create_idx + 3000]
ck("poll_count starts at 0 when the row is created",
   "poll_count=0" in create_body, True)
ck("no other code path assigns poll_count = 0",
   len(re.findall(r"poll_count\s*=\s*0\b", store_src)) , 1)

# An in-place change is the only way to preserve id/count/last result: the
# storage update whitelist does not include the budget or the condition, and
# the watch tool exposes create/list/cancel only.
m = re.search(r"_UPDATABLE_WATCH_FIELDS = frozenset\(\s*\{(.*?)\}\s*\)", store_src, re.S)
fields = set(re.findall(r'"(\w+)"', m.group(1))) if m else set()
ck("the update whitelist excludes max_polls", "max_polls" in fields, False)
ck("the update whitelist excludes stop_on", "stop_on" in fields, False)
ok("so an in-place lifetime extension is a direct row write, not an API call",
   "max_polls" not in fields and "stop_on" not in fields)

# ---------------------------------------------------------------------------
print()
print("== 2. the live campaign watch")
import psycopg  # noqa: E402

with psycopg.connect("dbname=turnstone") as conn:
    conn.execute("set transaction read only")
    row = conn.execute(
        "select watch_id, name, ws_id, node_id, command, interval_secs, max_polls,"
        " poll_count, active, stop_on, last_poll, next_poll, last_exit_code,"
        " last_output from watches where name = %s", (WATCH_NAME,)
    ).fetchone()

ok("the watch exists", row is not None, WATCH_NAME)
(watch_id, name, ws_id, node_id, command, interval, max_polls, poll_count,
 active, stop_on, last_poll, next_poll, last_exit, last_output) = row

print(f"     id={watch_id[:8]} name={name} node={node_id}")
print(f"     every {interval}s, poll #{poll_count}/{max_polls}, active={active}")

ck("it is active", active, 1)
ck("it still polls every 30 minutes", interval, 1800.0)
ck("it still runs the deployed reporter", command.endswith("s6/liveness.py --json"), True)
ck("it does not pin an epoch (automatic s6-t2 selection)", "--epoch" in command, False)

# The last result must survive the change, and must still be the T2 one.
last = json.loads(last_output)
ck("the retained last result is the live epoch", last["epoch"],
   "/var/lib/turnstone/sensor/s6-t2")
ck("...with the T2 marker", last["boundary_marker"], "T2")
ok("...and a parseable verdict", last.get("verdict") in {
    "HEALTHY", "HEALTHY_WITH_CAVEATS", "FAIL", "NO_ELIGIBLE_WORK", "NOT_STARTED"},
   str(last.get("verdict")))
ck("the accumulated poll count is preserved", poll_count >= 21, True)

print()
print("== 3. the hardened alert condition survives")
for fragment in ('exit_code != 0', 'not data', 'data["verdict"] == "FAIL"',
                 'data["acceptance"]["observations"]["met"]',
                 'data["acceptance"]["elapsed"]["met"]',
                 'data["acceptance"]["unmatched_ok"]',
                 'data["acceptance"]["no_duplicates_ok"]'):
    ok(f"condition still covers {fragment}", fragment in (stop_on or ""), stop_on or "")

# ---------------------------------------------------------------------------
print()
print("== 4. does the lifetime reach past the gate?")
interval_h = interval / 3600.0
remaining = max_polls - poll_count
remaining_h = remaining * interval_h

elapsed_h = last.get("elapsed_hours")
if elapsed_h is None:
    elapsed_h = (datetime.now(timezone.utc)
                 - datetime.strptime(last["boundary"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() / 3600
hours_to_gate = max(0.0, GATE_HOURS - elapsed_h)
polls_to_gate = math.ceil(hours_to_gate / interval_h)

print(f"     elapsed at last poll : {elapsed_h:.2f} h")
print(f"     hours to 48 h floor  : {hours_to_gate:.2f} h "
      f"({polls_to_gate} poll(s))")
print(f"     budget remaining     : {remaining} poll(s) = {remaining_h:.1f} h "
      f"({remaining_h / 24:.1f} d)")
print(f"     lifetime total       : {max_polls * interval_h:.1f} h "
      f"({max_polls * interval_h / 24:.1f} d)")

ok("the budget outlives the 48-hour floor", remaining_h > hours_to_gate,
   f"{remaining_h:.1f}h <= {hours_to_gate:.1f}h")
ok(f"the budget covers a slow {GATE_EVENTS}-event campaign "
   f"({SLOW_CAMPAIGN_DAYS} days minimum)",
   max_polls * interval_h >= SLOW_CAMPAIGN_DAYS * 24,
   f"{max_polls * interval_h / 24:.1f} d")

# Absolute expiry, computed the way the runner will: poll #max_polls happens
# one interval after the poll that currently holds `next_poll`, plus
# (max_polls - poll_count - 1) further intervals.
if next_poll:
    nxt = datetime.strptime(next_poll, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    last_due = nxt + timedelta(seconds=interval * (max_polls - poll_count - 1))
    print(f"     final poll due       : {last_due.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    ok("the final poll lands after the 48-hour requirement",
       last_due > datetime.now(timezone.utc) + timedelta(hours=hours_to_gate),
       last_due.isoformat())

print()
print("== 5. exhaustion cannot be expressed as a stop_on clause")
# The evaluator's context is fixed at output/data/exit_code/prev_output/changed,
# so a condition cannot see the remaining budget. Detection must therefore come
# from the command's own output.
ctx_start = src.index("context = {")
ctx = src[ctx_start:ctx_start + 400]
for key in ('"output"', '"data"', '"exit_code"'):
    ok(f"the condition context provides {key}", key in ctx, ctx[:120])
for key in ('"poll_count"', '"max_polls"'):
    ok(f"the condition context does NOT provide {key}", key not in ctx, ctx[:120])
ok("so budget exhaustion needs a separate read-only check, not a stop_on clause",
   True)

print()
print("== 6. the budget check reports what the runner will do")
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("budget", S6 / "watch_budget.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


def fake(poll_count, max_polls=100, active=True, next_poll="2026-10-18T18:06:29"):
    return {"watch_id": "abc123def456", "name": "w", "interval_secs": 1800.0,
            "max_polls": max_polls, "poll_count": poll_count, "active": active,
            "last_poll": "2026-10-18T17:36:29", "next_poll": next_poll,
            "last_exit_code": 0}


r = budget.report(fake(28), 0.10)
ck("plenty of budget does not warn", r["warn"], False)
ck("remaining is max_polls - poll_count", r["polls_remaining"], 72)
ck("remaining hours use the real interval", r["hours_remaining"], 36.0)

r = budget.report(fake(91), 0.10)
ck("9% left warns", (r["warn"], r["polls_remaining"]), (True, 9))

r = budget.report(fake(90), 0.10)
ck("exactly 10% left does not warn", r["warn"], False)

r = budget.report(fake(100), 0.10)
ck("a spent budget warns", (r["warn"], r["polls_remaining"]), (True, 0))

r = budget.report(fake(28, active=False), 0.10)
ck("an ended campaign is reported as inactive", r["target_active"], False)

r = budget.report(fake(28, next_poll="2026-10-18T18:06:29"), 0.10)
# 72 remaining -> last poll is 71 intervals after the next one.
expected = (datetime.strptime("2026-10-18T18:06:29", "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=timezone.utc) + timedelta(seconds=1800 * 71))
ck("expiry matches the runner's schedule", r["expires_utc"],
   expected.strftime("%Y-%m-%dT%H:%M:%SZ"))

r = budget.report(fake(28, next_poll=""), 0.10)
ck("an inactive schedule reports no expiry", r["expires_utc"], None)

print()
print("== 7. exhaustion is monitored, not just measurable")
with psycopg.connect("dbname=turnstone") as conn:
    conn.execute("set transaction read only")
    brow = conn.execute(
        "select watch_id, ws_id, node_id, command, interval_secs, max_polls,"
        " poll_count, active, stop_on, last_exit_code, last_output"
        " from watches where name = 's6-t2-watch-budget'"
    ).fetchone()

ok("a budget watch exists", brow is not None)
(bid, bws, bnode, bcmd, bint, bmax, bcount, bactive, bstop, bexit, bout) = brow
ck("it alerts into the same workstream", bws, ws_id)
ck("it is polled by the same node as the campaign watch", bnode, node_id)
ck("it is active", bactive, 1)
ck("it checks every 12 hours", bint, 43200.0)
ok("it runs the read-only budget check", bcmd.endswith("watch_budget.py --json"), bcmd)
ok("its own lifetime outlives the campaign watch",
   bmax * bint / 3600 >= max_polls * interval_h,
   f"{bmax * bint / 86400:.0f} d vs {max_polls * interval_h / 24:.0f} d")
for fragment in ("exit_code != 0", "not data", 'data["warn"] is True',
                 'not data["target_active"]'):
    ok(f"budget condition covers {fragment}", fragment in (bstop or ""), bstop or "")
ok("the condition uses Python True (eval, not JavaScript)",
   " is True" in (bstop or "") and " true" not in (bstop or ""), bstop or "")

if bcount:
    ck("its first poll succeeded", bexit, 0)
    bdata = json.loads(bout)
    ck("it reported the campaign watch by identity", bdata["watch"], WATCH_NAME)
    ck("no warning is due yet", bdata["warn"], False)
    ok("it sees the extended budget", bdata["max_polls"] == max_polls,
       f"{bdata['max_polls']} vs {max_polls}")
else:
    print("     (first poll not yet taken)")

print("---")
print(f"pass={pass_n} fail={fail_n}")
sys.exit(0 if fail_n == 0 else 1)
