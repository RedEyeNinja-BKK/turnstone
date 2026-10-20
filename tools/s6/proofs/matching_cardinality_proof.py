#!/usr/bin/env python3
"""PROOF: the attribution is an EXACT maximum-cardinality assignment.

The defect this pins (found 2026-10-20, during independent review):

``provenance.match_events_to_turns`` assigned each event its best
still-unclaimed candidate row, greedily, in event order. A greedy
assignment does not find a complete matching when events compete for the
same rows, so it UNDER-reports attributable operator work.

The counterexample (a 5-second window)::

    store rows at  -4 s  and  +1 s
    events        at  +0.5 s and +5.5 s

    event @ +0.5 s  -> candidates: row @ -4 s (d = 4.5), row @ +1 s (d = 0.5)
    event @ +5.5 s  -> candidates: row @ +1 s (d = 4.5)

The greedy matcher gives row @ +1 s to the event @ +0.5 s (it is the
closest), leaving the event @ +5.5 s with NO candidate. ONE match.

But a valid TWO-match assignment exists: event @ +0.5 s -> row @ -4 s,
event @ +5.5 s -> row @ +1 s, both at d = 4.5. TWO matches.

So the greedy matcher UNDER-REPORTS attributable operator work. That is
the same class of defect as the double-claim it was written to fix:
under-reporting distinct work.

The correction is a MINIMUM-COST MAXIMUM-CARDINALITY assignment:

  * PRIMARY: maximise the number of matched (event, row) pairs. A campaign
    is measured in operator turns, so a matching that credits more turns is
    better even if some pairings are less close in time.
  * SECONDARY: among maximum-cardinality matchings, minimise the total
    attribution distance, so the pairing is as tight as the data allows.
  * TERTIARY: deterministic tie-breaking on (distance, operator-origin
    first, row id, event id), so the result never depends on the order
    the events arrive in.

Time proximity alone does NOT prove identity. The matcher says "this event
corresponds to that row"; it never claims the two are the same thing. The
FINGERPRINT is what independently corroborates an attribution, and the
attribution's correctness is checked against the store separately.

These checks drive the REAL ``provenance.py`` with synthetic turns - no
database, no telemetry.

Run: /opt/turnstone-venv/bin/python tools/s6/proofs/matching_cardinality_proof.py
"""
from __future__ import annotations

import datetime
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent

PASS = 0
FAIL = 0

UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


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


spec = importlib.util.spec_from_file_location("s6prov_match", S6 / "provenance.py")
prov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prov)


def turn(ident, ws, when, role="user", source=None):
    """A store turn in the shape ``provenance.fetch_store`` produces.

    The origin is DERIVED, the way the real store does it, so the
    operator-origin ranking the matcher applies is exercised for real.
    """
    return {
        "id": ident,
        "ws_id": ws,
        "ts": when,
        "role": role,
        "source": source,
        "commit_key": f"ck-{ident}",
        "has_attachment": False,
        "content": "",
        "meta": {},
        "origin": prov.classify_row(role, source),
    }


def event(ident, ws, when, event_type="operator_instruction"):
    return {"event_id": ident, "event_type": event_type, "ws_id": ws,
            "ts": when}


def at(seconds):
    return BASE + datetime.timedelta(seconds=seconds)


def main() -> int:
    print("MATCHING CARDINALITY PROOF - the attribution is exact")
    print("=" * 66)
    print()

    # ---- 1. the counterexample: a greedy match loses a pairing ------------
    print("1. THE COUNTEREXAMPLE - a valid two-match assignment is found")
    turns = [turn(1, "ws", at(-4)), turn(2, "ws", at(1))]
    events = [event("e1", "ws", at(0.5)), event("e2", "ws", at(5.5))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}

    check("both events are matched", len(matched), 2)
    ok("the event a greedy matcher starves is matched", "e2" in by_event,
       str(by_event))
    check("no row is claimed twice", len(set(by_event.values())), len(by_event))
    ok("every match is inside the window",
       all(m["distance_s"] <= 5.0 for m in matched), str(matched))

    # ---- 2. overlapping candidate sets ------------------------------------
    print()
    print("2. OVERLAPPING CANDIDATES - as many as possible are matched")
    turns = [turn(1, "ws", at(0)), turn(2, "ws", at(2))]
    events = [event("e1", "ws", at(0)), event("e2", "ws", at(1)),
              event("e3", "ws", at(2))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    check("two of the three events are matched", len(matched), 2)
    check("no row is claimed twice", len(set(by_event.values())), len(by_event))

    # ---- 3. identical timestamps -------------------------------------------
    print()
    print("3. IDENTICAL TIMESTAMPS - the assignment is still one-to-one")
    turns = [turn(1, "ws", at(0)), turn(2, "ws", at(0))]
    events = [event("e1", "ws", at(0)), event("e2", "ws", at(0))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    check("both events are matched", len(matched), 2)
    check("no row is claimed twice", len(set(by_event.values())), len(by_event))

    # ---- 4. several events competing for one row --------------------------
    print()
    print("4. ONE ROW, THREE EVENTS - the CLOSEST one takes it")
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(-1)), event("e2", "ws", at(0)),
              event("e3", "ws", at(1))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    check("exactly one event takes the row", len(matched), 1)
    if matched:
        check("the closest event takes the row",
              matched[0]["event"]["event_id"], "e2")
        check("at zero distance", matched[0]["distance_s"], 0.0)

    # ---- 5. an event with no valid candidate ------------------------------
    print()
    print("5. NO CANDIDATE - the event stays unmatched")
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(600))]
    check("the event is unmatched",
          prov.match_events_to_turns(events, turns, window=5.0), [])

    # ---- 6. system transitions are excluded --------------------------------
    print()
    print("6. SYSTEM TRANSITIONS - excluded from the assignment")
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(0), event_type="delegation_result"),
              event("e2", "ws", at(0), event_type="retry_transition"),
              event("e3", "ws", at(0), event_type="task_agent_failure")]
    check("no system transition is attributed",
          prov.match_events_to_turns(events, turns, window=5.0), [])

    # ---- 7. the order of the input must not change the result -------------
    print()
    print("7. ORDER INDEPENDENCE - the same input, the same output")
    turns = [turn(1, "ws", at(-4)), turn(2, "ws", at(1)),
             turn(3, "ws", at(1)), turn(4, "ws", at(6))]
    events = [event("e1", "ws", at(0.5)), event("e2", "ws", at(5.5)),
              event("e3", "ws", at(1.0))]
    forward = prov.match_events_to_turns(events, turns, window=5.0)
    reversed_ = prov.match_events_to_turns(list(reversed(events)), turns,
                                           window=5.0)
    f_map = {m["event"]["event_id"]: m["turn"]["id"] for m in forward}
    r_map = {m["event"]["event_id"]: m["turn"]["id"] for m in reversed_}
    check("reversing the input changes nothing", f_map, r_map)
    check("no row is claimed twice", len(set(f_map.values())), len(f_map))

    # ---- 8. the historical case the one-to-one rule was written for -------
    print()
    print("8. THE HISTORICAL TIE CASE - two distinct rows")
    turns = [turn(2001, "ws", BASE, role="system", source="user_interjection"),
             turn(2002, "ws", BASE, role="system", source="user_interjection")]
    events = [event("e1", "ws", BASE), event("e2", "ws", BASE)]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    check("both events are matched", len(matched), 2)
    check("no row is claimed twice", len(set(by_event.values())), len(by_event))

    # ---- 9. the operator row outranks a system row at equal distance ------
    print()
    print("9. THE TIE RULE - an operator row outranks a system row")
    turns = [turn(3001, "ws", BASE, role="system", source="memory_pointer"),
             turn(3002, "ws", BASE, role="user")]
    events = [event("e1", "ws", BASE)]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    check("one event is matched", len(matched), 1)
    if matched:
        check("the operator row wins the tie", matched[0]["turn"]["id"], 3002)

    # ---- 10. the common case is untouched ----------------------------------
    print()
    print("10. THE COMMON CASE - unchanged")
    turns = [turn(1001, "ws", at(0))]
    events = [event("e1", "ws", at(1))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    check("one event is matched", len(matched), 1)
    if matched:
        check("it matched its own row", matched[0]["turn"]["id"], 1001)

    # ---- 11. workstreams are independent -----------------------------------
    print()
    print("11. WORKSTREAMS - a row is never shared across them")
    turns = [turn(1, "a", at(0)), turn(2, "b", at(0))]
    events = [event("e1", "a", at(0)), event("e2", "b", at(0))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    check("each event took its own workstream's row", by_event,
          {"e1": 1, "e2": 2})

    # ---- 12. the assignment is EXACT, not a heuristic ----------------------
    print()
    print("12. EXACTNESS - a random instance is solved exactly")
    # A brute-force reference: enumerate every one-to-one assignment of
    # events to admissible rows and take the best by the documented
    # objective. The solver must agree with it.
    import itertools
    import random

    # A FIXED seed, so the fixtures are reproducible and the proof is a
    # specification rather than a coin flip. This is a pseudo-random
    # generator used to build test data, never for anything cryptographic.
    rng = random.Random(20261020)  # noqa: S311
    disagreements = 0
    for _trial in range(60):
        n_events = rng.randint(2, 5)
        n_rows = rng.randint(2, 6)
        t_turns = [turn(5000 + k, "ws", at(rng.randint(-3, 3)))
                   for k in range(n_rows)]
        t_events = [event(f"x{k}", "ws", at(rng.randint(-3, 3)))
                    for k in range(n_events)]
        admissible: dict[str, list[tuple[int, int]]] = {}
        for e in t_events:
            candidates = []
            for t in t_turns:
                d = abs((t["ts"] - e["ts"]).total_seconds())
                if d <= 5.0:
                    candidates.append((t["id"], int(round(d * 1_000_000))))
            admissible[e["event_id"]] = candidates

        # Brute force the best one-to-one assignment.
        best = None
        event_ids = list(admissible)
        for size in range(min(len(event_ids), len(t_turns)), -1, -1):
            for chosen_events in itertools.combinations(event_ids, size):
                for chosen_rows in itertools.permutations(
                        [t["id"] for t in t_turns], size):
                    pairs = []
                    ok_pair = True
                    for e_id, r_id in zip(chosen_events, chosen_rows, strict=True):
                        hit = [c for c in admissible[e_id] if c[0] == r_id]
                        if not hit:
                            ok_pair = False
                            break
                        pairs.append((e_id, r_id, hit[0][1]))
                    if not ok_pair:
                        continue
                    total = sum(p[2] for p in pairs)
                    if best is None or total < best[0]:
                        best = (total, set(pairs))
            if best is not None:
                break

        got = prov.match_events_to_turns(t_events, t_turns, window=5.0)
        got_pairs = {(m["event"]["event_id"], m["turn"]["id"],
                      int(round(m["distance_s"] * 1_000_000))) for m in got}
        if best is None:
            if got_pairs:
                disagreements += 1
        else:
            if len(got_pairs) != len(best[1]):
                disagreements += 1
            else:
                got_total = sum(p[2] for p in got_pairs)
                if got_total != best[0]:
                    disagreements += 1
    check("60 random instances match the brute-force optimum", disagreements, 0)

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
