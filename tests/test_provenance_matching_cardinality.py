"""RED-FIRST PROOF: the attribution finds a MAXIMUM-CARDINALITY matching.

The defect this pins (found 2026-10-20, during independent review):

``provenance.match_events_to_turns`` assigns each event its best
still-unclaimed candidate row, greedily, in event order. That is a
GREEDY assignment, and a greedy assignment does not find a complete
matching when events compete for the same rows.

The counterexample (a 5-second window):

    store rows at  -4 s  and  +1 s
    events        at  +0.5 s and +5.5 s

    event @ +0.5 s  -> candidates: row @ -4 s (d = 4.5), row @ +1 s (d = 0.5)
    event @ +5.5 s  -> candidates: row @ +1 s (d = 4.5)   [row @ -4 s is d = 9.5, out]

The greedy matcher gives row @ +1 s to the event @ +0.5 s (it is the
closest), leaving the event @ +5.5 s with NO candidate. ONE match.

But a valid TWO-match assignment exists: event @ +0.5 s -> row @ -4 s,
event @ +5.5 s -> row @ +1 s, both at d = 4.5. TWO matches.

So the greedy matcher UNDER-REPORTS attributable operator work. That is
the same class of defect as the double-claim it was written to fix:
under-reporting distinct work.

The correction is a MINIMUM-COST MAXIMUM-CARDINALITY assignment:

  * PRIMARY: maximise the number of matched (event, row) pairs. A campaign
    is measured in operator turns, so a matching that credits more turns
    is better even if some pairings are less close in time.
  * SECONDARY: among maximum-cardinality matchings, minimise the total
    attribution distance, so the pairing is as tight as the data allows.
  * TERTIARY: deterministic tie-breaking on (distance, operator-origin
    first, row id, event id), so the result never depends on the order
    the events arrive in.

Time proximity alone does NOT prove identity. The matcher says "this event
corresponds to that row"; it never claims the two are the same thing. The
FINGERPRINT is what independently corroborates an attribution, and the
attribution's correctness is checked against the store separately.

The problem sizes here are small: on the live epoch the largest workstream
had 20 operator events and at most 8 candidate rows per event, so an exact
solver is affordable.

These tests drive the REAL ``provenance.py`` functions with synthetic turns -
no database, no telemetry.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sys

#: Where the provenance module under test lives.
#:
#: The default is the module version-controlled with this repository
#: (``tools/s6/provenance.py``), so the test runs for any reviewer without a
#: VM-local artifact tree.
#:
#: ``TURNSTONE_S6_PROVENANCE_DIR`` redirects it, which is what makes the
#: RED-FIRST proof possible: the pre-fix module has to be pointed at
#: explicitly rather than inherited from the repository.
_S6_DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "tools" / "s6"
S6 = pathlib.Path(
    os.environ.get("TURNSTONE_S6_PROVENANCE_DIR", str(_S6_DEFAULT))
)
if str(S6) not in sys.path:
    sys.path.insert(0, str(S6))

import provenance as prov  # noqa: E402

UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
        return 0
    print(f"FAIL  {label}: got {got!r} want {want!r}")
    return 1


def ok(label, condition, detail=""):
    if condition:
        print(f"PASS  {label}")
        return 0
    print(f"FAIL  {label}{': ' + detail if detail else ''}")
    return 1


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


# ---- 1. the counterexample: a greedy match loses a pairing ----------------


def test_a_valid_two_match_assignment_is_found():
    """A greedy match loses a pairing that a maximum matching keeps.

    Store rows at -4 s and +1 s; events at +0.5 s and +5.5 s. The greedy
    matcher gives row +1 s to the event at +0.5 s (closest) and leaves the
    event at +5.5 s unmatched, even though event +0.5 s -> row -4 s and
    event +5.5 s -> row +1 s is a valid TWO-match assignment.
    """
    turns = [turn(1, "ws", at(-4)), turn(2, "ws", at(1))]
    events = [event("e1", "ws", at(0.5)), event("e2", "ws", at(5.5))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)

    assert len(matched) == 2, (
        f"only {len(matched)} of 2 events matched: "
        f"{[m['event']['event_id'] for m in matched]}"
    )
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert len(set(by_event.values())) == len(by_event), by_event
    assert all(m["distance_s"] <= 5.0 for m in matched), matched


def test_the_event_a_greedy_matcher_would_starve_is_matched():
    turns = [turn(1, "ws", at(-4)), turn(2, "ws", at(1))]
    events = [event("e1", "ws", at(0.5)), event("e2", "ws", at(5.5))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert "e2" in by_event, by_event


# ---- 2. overlapping candidate sets -----------------------------------------


def test_overlapping_candidate_sets_match_as_many_as_possible():
    """Three events, two rows: two of the three must be matched."""
    turns = [turn(1, "ws", at(0)), turn(2, "ws", at(2))]
    events = [event("e1", "ws", at(0)), event("e2", "ws", at(1)),
              event("e3", "ws", at(2))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 2, matched
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert len(set(by_event.values())) == len(by_event), by_event


# ---- 3. identical timestamps -----------------------------------------------


def test_identical_timestamps_still_produce_a_bijection():
    """Every pair ties on distance; the assignment must still be one-to-one."""
    turns = [turn(1, "ws", at(0)), turn(2, "ws", at(0))]
    events = [event("e1", "ws", at(0)), event("e2", "ws", at(0))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 2, matched
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert len(set(by_event.values())) == len(by_event), by_event


# ---- 4. several events competing for one row ------------------------------


def test_only_one_event_may_take_a_row_and_it_is_the_closest():
    """One row, three events: only one may take it, and it must be the
    CLOSEST.

    The greedy matcher gave the row to the first event it processed, which
    was not necessarily the closest. The assignment minimises total
    distance, so the closest event must win.
    """
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(-1)), event("e2", "ws", at(0)),
              event("e3", "ws", at(1))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 1, matched
    assert matched[0]["event"]["event_id"] == "e2", (
        "the CLOSEST event must take the row"
    )
    assert matched[0]["distance_s"] == 0.0, matched


# ---- 5. an event with no valid candidate -----------------------------------


def test_an_event_with_no_candidate_stays_unmatched():
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(600))]
    assert prov.match_events_to_turns(events, turns, window=5.0) == []


# ---- 4b. one row, many events: a ROW is credited once -----------------------


def test_one_row_credits_at_most_one_event_however_many_events_arrive():
    """A store ROW is credited to at most one event.

    The attribution is a one-to-one assignment, so one store row cannot be
    credited to more than one event, no matter how many events arrive for
    it. Three events over one row produce ONE attributed event.

    This is a property of the ROWS, and it is deliberately NOT stated as a
    property of turns: the row-level bound does not close the
    consecutive-only question (see the next test, which is the counter-shape
    that shows why).
    """
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(0)), event("e2", "ws", at(0)),
              event("e3", "ws", at(0))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 1, matched
    claimed = [m["turn"]["id"] for m in matched]
    assert len(claimed) == len(set(claimed)), (
        f"one store row was credited to more than one event: {claimed}"
    )


def test_a_repeated_identity_can_be_credited_to_a_different_row():
    """The counter-shape: the row-level bound is NOT a turn-level bound.

    The dedupe state is a single slot, so the same turn identity CAN be
    admitted again after an intervening event (see
    ``docs/design/s6-dedupe-identity.md`` §5). The gate counts attributable
    EVENTS.

    One store row being credited once does **not** mean a repeated identity
    yields one attributable event: the identity's second admission is matched
    against the rows still unclaimed, so it lands on a DIFFERENT row - here a
    different admitted turn - and both events are counted.

    This is pinned so the row-level one-to-one property is never mistaken for
    a turn-level one. What actually bounds a repeated identity is upstream,
    in the sensor: every operator seam observes the row it has just admitted,
    exactly once, and every admission mints a fresh commit key, so one
    admitted row never reaches the seam twice.
    """
    turns = [turn(1, "ws", at(0)), turn(2, "ws", at(1))]
    events = [event("e1", "ws", at(0)), event("e2", "ws", at(0.5))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)

    assert len(matched) == 2, matched
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert by_event == {"e1": 1, "e2": 2}, by_event
    assert len(set(by_event.values())) == len(by_event), (
        "the assignment must still be one-to-one over the rows"
    )


# ---- 6. system transitions are excluded ------------------------------------


def test_system_transitions_are_excluded_from_the_assignment():
    turns = [turn(1, "ws", at(0))]
    events = [event("e1", "ws", at(0), event_type="delegation_result"),
              event("e2", "ws", at(0), event_type="retry_transition"),
              event("e3", "ws", at(0), event_type="task_agent_failure")]
    assert prov.match_events_to_turns(events, turns, window=5.0) == []


# ---- 7. the order of the input must not change the result ------------------


def test_reversed_input_order_gives_the_same_assignment():
    turns = [turn(1, "ws", at(-4)), turn(2, "ws", at(1)),
             turn(3, "ws", at(1)), turn(4, "ws", at(6))]
    events = [event("e1", "ws", at(0.5)), event("e2", "ws", at(5.5)),
              event("e3", "ws", at(1.0))]
    forward = prov.match_events_to_turns(events, turns, window=5.0)
    reversed_ = prov.match_events_to_turns(list(reversed(events)), turns,
                                           window=5.0)
    f_map = {m["event"]["event_id"]: m["turn"]["id"] for m in forward}
    r_map = {m["event"]["event_id"]: m["turn"]["id"] for m in reversed_}
    assert f_map == r_map, f"forward {f_map} != reversed {r_map}"
    assert len(set(f_map.values())) == len(f_map), f_map


# ---- 8. the historical case the matcher was written for --------------------


def test_the_historical_tie_case_matches_two_distinct_rows():
    """Two candidate rows with the SAME timestamp, two events that tie.

    Each event must take a DIFFERENT row. This is the case the one-to-one
    rule was written for: the old independent matcher gave both events the
    same row.
    """
    turns = [turn(2001, "ws", BASE, role="system", source="user_interjection"),
             turn(2002, "ws", BASE, role="system", source="user_interjection")]
    events = [event("e1", "ws", BASE), event("e2", "ws", BASE)]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 2, matched
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert len(set(by_event.values())) == len(by_event), by_event

def test_an_operator_row_outranks_a_system_row_at_equal_distance():
    # A `memory_pointer` system row and a `user` operator row, both at the
    # same instant. The operator row must win the tie.
    turns = [turn(3001, "ws", BASE, role="system", source="memory_pointer"),
             turn(3002, "ws", BASE, role="user")]
    events = [event("e1", "ws", BASE)]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 1, matched
    assert matched[0]["turn"]["id"] == 3002, matched


# ---- 9. the common case is untouched ---------------------------------------


def test_one_event_and_one_row_still_match():
    turns = [turn(1001, "ws", at(0))]
    events = [event("e1", "ws", at(1))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    assert len(matched) == 1, matched
    assert matched[0]["turn"]["id"] == 1001, matched


# ---- 10. workstreams are separate ------------------------------------------


def test_rows_are_never_shared_across_workstreams():
    turns = [turn(1, "a", at(0)), turn(2, "b", at(0))]
    events = [event("e1", "a", at(0)), event("e2", "b", at(0))]
    matched = prov.match_events_to_turns(events, turns, window=5.0)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert by_event == {"e1": 1, "e2": 2}, by_event
