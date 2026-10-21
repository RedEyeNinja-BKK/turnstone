#!/usr/bin/env python3
"""Proofs for EVENT PROVENANCE accounting - read only.

Three findings are pinned here, each of which a raw row count could not see:

1. ``role`` is not an origin signal. A queued operator message is stored as
   ``role='system'``/``_source='user_interjection'``; a machine nudge is stored
   as ``role='user'``/``_source='system_nudge'``.
2. An observation whose workstream was DELETED has no conversation row, but is
   still attributable when audit + usage records show an authenticated user
   created the workstream at that instant and drove model calls in it.
3. Dedupe suppressed a DISTINCT operator turn, because the privacy-preserving
   descriptor it keys on is identical for two different documents.

Nothing is written. Usage: /opt/turnstone-venv/bin/python evidence/provenance_proof.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

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


spec = importlib.util.spec_from_file_location("s6prov", S6 / "provenance.py")
prov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prov)

T0 = datetime(2026, 10, 18, 13, 21, 51, tzinfo=timezone.utc)


def turn(row_id, when, *, role="user", source=None, ws="ws1", commit="c1"):
    return {"id": row_id, "ws_id": ws, "ts": when, "role": role, "source": source,
            "commit_key": commit, "has_attachment": False, "content": "",
            "meta": None, "origin": prov.classify_row(role, source)}


def event(eid, when, *, ws="ws1", fp="fp1", outcome="sensor_call", observed=True):
    return {"event_id": eid, "outcome": outcome, "ws_id": ws, "fingerprint": fp,
            "ts": when, "observed": observed, "service": "svc"}


def empty_audit():
    return {"created": {}, "deleted": set(), "usage": {}, "users": {}}


print("== 1. origin is decided by role AND _source")
ck("user_interjection is operator", prov.classify_row("system", "user_interjection"), "operator")
ck("plain user turn is operator", prov.classify_row("user", None), "operator")
ck("system_nudge on role=user is NOT operator", prov.classify_row("user", "system_nudge"), "system")
for src in ("output_guard", "watch_triggered", "memory_pointer", "tool_error",
            "background_shell_exit", "skill_hint"):
    ck(f"{src} is NOT operator", prov.classify_row("system", src), "system")

print()
print("== 2. an admitted event with a matching store turn is verified")
store = {"turns": [turn(1, T0 + timedelta(minutes=1))]}
r = prov.reconcile(events=[event("e1", T0 + timedelta(minutes=1))], store=store,
                   audit=empty_audit())
ck("A counts the raw observation", r["A_raw_observed"], 1)
ck("B counts the verified operator event", r["B_verified_operator"], 1)
ck("C is empty", r["C_unverified"], 0)

print()
print("== 3. an interjection event is verified operator work")
store = {"turns": [turn(2, T0 + timedelta(minutes=2), role="system",
                        source="user_interjection")]}
r = prov.reconcile(events=[event("e2", T0 + timedelta(minutes=2))], store=store,
                   audit=empty_audit())
ck("an interjection corroborates an event", r["B_verified_operator"], 1)
ck("and is not classified as system-generated", r["C2_system_generated"], 0)

print()
print("== 4. a machine turn does NOT corroborate operator work")
store = {"turns": [turn(3, T0 + timedelta(minutes=3), role="user", source="system_nudge")]}
r = prov.reconcile(events=[event("e3", T0 + timedelta(minutes=3))], store=store,
                   audit=empty_audit())
ck("a nudge cannot verify an operator event", r["B_verified_operator"], 0)
ck("it is reported as system-generated", r["C2_system_generated"], 1)
ck("and not as unverified", r["C_unverified"], 0)

print()
print("== 5. no store row, no audit trail -> UNVERIFIED (never silently counted)")
store = {"turns": []}
r = prov.reconcile(events=[event("e4", T0 + timedelta(minutes=4))], store=store,
                   audit=empty_audit())
ck("unattributable is UNVERIFIED", r["C_unverified"], 1)
ck("and is NOT verified", r["B_verified_operator"], 0)
ck("and is NOT counted as raw-observed work", r["B_verified_operator"], 0)

print()
print("== 6. deleted history + authoritative audit records -> attributable")
when = T0 + timedelta(minutes=5)
audit = {"created": {"ws9": {"ts": when, "user_id": "u1"}},
         "deleted": {"ws9"}, "usage": {"ws9": 7}, "users": {"ws9": {"u1"}}}
r = prov.reconcile(events=[event("e5", when, ws="ws9")], store={"turns": []}, audit=audit)
ck("audit-attributed, not unverified", r["C_unverified"], 0)
ck("counted separately so it can be audited", r["B2_verified_operator_audit"], 1)
ck("B counts only store-matched events (no double count)",
   r["B_verified_operator"], 0)
entry = r["ledger"][0]
ck("the ledger names the erased history", entry["history_erased"], True)
ck("and the model calls that prove real use", entry["usage_events"], 7)

print()
print("== 7. audit evidence must be contemporaneous AND used")
stale = {"created": {"ws9": {"ts": when - timedelta(minutes=30), "user_id": "u1"}},
         "deleted": {"ws9"}, "usage": {"ws9": 7}, "users": {"ws9": {"u1"}}}
r = prov.reconcile(events=[event("e5", when, ws="ws9")], store={"turns": []}, audit=stale)
ck("a stale creation record does not attribute", r["C_unverified"], 1)
unused = {"created": {"ws9": {"ts": when, "user_id": "u1"}}, "deleted": set(),
          "usage": {}, "users": {}}
r = prov.reconcile(events=[event("e5", when, ws="ws9")], store={"turns": []}, audit=unused)
ck("a workstream with zero model calls does not attribute", r["C_unverified"], 1)

print()
print("== 8. dedupe: same turn twice vs two distinct turns")
same = [turn(10, T0 + timedelta(minutes=10), commit="aaa")]
store = {"turns": same}
r = prov.reconcile(
    events=[event("a1", T0 + timedelta(minutes=10)),
            event("s1", T0 + timedelta(minutes=10), outcome="dedupe_suppressed")],
    store=store, audit=empty_audit())
ck("one logical turn fired twice counts as a legitimate duplicate",
   r["E_legitimate_duplicates_suppressed"], 1)
ck("and not as over-suppression", r["F_distinct_turns_suppressed"], 0)

distinct = [turn(10, T0 + timedelta(minutes=10), commit="aaa"),
            turn(11, T0 + timedelta(minutes=200), commit="bbb")]
r = prov.reconcile(
    events=[event("a1", T0 + timedelta(minutes=10)),
            event("s1", T0 + timedelta(minutes=200), outcome="dedupe_suppressed")],
    store={"turns": distinct}, audit=empty_audit())
ck("a DIFFERENT stored turn is over-suppression",
   r["F_distinct_turns_suppressed"], 1)
ck("and is not excused as a duplicate", r["E_legitimate_duplicates_suppressed"], 0)
led = [e for e in r["ledger"] if e["kind"] == "suppressed"][0]
ck("the ledger names the turn it was collapsed into", led["previous_event"], "a1")
ck("...and the distinct turn it discarded", led["store_id"], 11)

print()
print("== 9. the matcher cannot be fooled by a tool row on the same second")
store = {"turns": [
    {"id": 20, "ws_id": "ws1", "ts": T0, "role": "tool", "source": None,
     "commit_key": "t", "has_attachment": False, "content": "", "meta": None,
     "origin": "system"},
    turn(21, T0, role="system", source="user_interjection"),
]}
r = prov.reconcile(events=[event("e6", T0)], store=store, audit=empty_audit())
ck("the interjection wins over the tool row", r["B_verified_operator"], 1)
ck("and the tool row does not make it system-generated", r["C2_system_generated"], 0)

print("---")
print(f"pass={pass_n} fail={fail_n}")
sys.exit(0 if fail_n == 0 else 1)
