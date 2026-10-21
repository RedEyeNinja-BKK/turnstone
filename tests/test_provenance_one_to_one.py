"""One store row, one event: a one-to-one assignment in the attribution.

The defect this pins (measured live on 2026-10-19, epoch s6-t2):

``provenance._nearest()`` picks, for each admitted event, the single closest
store row within a +/-5 s window. It is an INDEPENDENT nearest-neighbour
match, so one store row can be claimed by TWO events.

Live proof: one store row was claimed by two events 0.68 s apart. The two
candidate interjection rows carry the SAME second-resolution timestamp, so
both events tie on distance - and the independent matcher credited the same
row to both.

The fingerprints settle which event belongs to which row, independently of the
matcher::

    c3eab1e49e95a55d087a90d82eee66bd == "do not modify the staging host"
    7424fc427f7909b93ee2ec1b058862a4 == "scope is limited to the second node"

These are SYNTHETIC messages with RECOMPUTED fingerprints. The originals were
real operator text, and the fingerprint is an unsalted SHA-256 of the
projection - so publishing the text beside its digest published the content
AND a lookup key for it, and the digest of a real message is a confirmation
oracle (guess the message, hash it, compare). The property the test needs -
two DISTINCT digests over two DISTINCT messages - is preserved exactly.

A one-to-one assignment (each row consumed at most once, events in timestamp
order, ties broken by distance -> operator-origin -> row id) attributes the
two events to two different rows, each matching its own fingerprint.

The headline B count does not change, but one row was double-credited and one
event was attributed to the wrong turn. Under-reporting distinct work is the
same class of defect as the dedupe collision, so this is pinned too.

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


def _turn(row_id: int, ws_id: str, when: datetime.datetime, *, role="user",
          source=None) -> dict:
    """A store turn in the shape ``provenance.fetch_store`` produces."""
    origin = prov.classify_row(role, source)
    return {
        "id": row_id,
        "ws_id": ws_id,
        "ts": when,
        "role": role,
        "source": source,
        "commit_key": f"ck-{row_id}",
        "has_attachment": False,
        "content": "",
        "meta": {},
        "origin": origin,
    }


def _event(event_id: str, ws_id: str, when: datetime.datetime,
           event_type: str = "operator_instruction") -> dict:
    return {
        "event_id": event_id,
        "outcome": "sensor_call",
        "event_type": event_type,
        "ws_id": ws_id,
        "fingerprint": f"fp-{event_id}",
        "ts": when,
        "observed": True,
        "service": "turnstone-head-1",
    }


# ----------------------------------------------------------------- the defect


def test_one_store_row_is_never_claimed_by_two_events():
    """RED against the independent matcher: the row was credited twice.

    The live shape: two interjection rows written in the same second, two
    admitted events 0.68 s apart, both tying on distance.
    """
    base = datetime.datetime(2026, 10, 19, 16, 20, 21, tzinfo=UTC)
    turns = [
        _turn(2001, "ws", base, role="system", source="user_interjection"),
        _turn(2002, "ws", base, role="system", source="user_interjection"),
    ]
    events = [
        _event("ev-a", "ws", base - datetime.timedelta(seconds=1.393)),
        _event("ev-b", "ws", base - datetime.timedelta(seconds=0.711)),
    ]

    matched = prov.match_events_to_turns(events, turns)

    claimed = [m["turn"]["id"] for m in matched]
    assert len(claimed) == len(set(claimed)), (
        f"one store row was credited to more than one event: {claimed}"
    )


def test_a_tied_pair_resolves_to_distinct_rows():
    """The two tied events must land on two different rows."""
    base = datetime.datetime(2026, 10, 19, 16, 20, 21, tzinfo=UTC)
    turns = [
        _turn(2001, "ws", base, role="system", source="user_interjection"),
        _turn(2002, "ws", base, role="system", source="user_interjection"),
    ]
    events = [
        _event("ev-a", "ws", base - datetime.timedelta(seconds=1.393)),
        _event("ev-b", "ws", base - datetime.timedelta(seconds=0.711)),
    ]

    matched = prov.match_events_to_turns(events, turns)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}

    assert by_event["ev-a"] != by_event["ev-b"], (
        f"the tied events were credited to the same row: {by_event}"
    )


def test_the_assignment_is_deterministic():
    """The same input must always produce the same attribution."""
    base = datetime.datetime(2026, 10, 19, 16, 20, 21, tzinfo=UTC)
    turns = [
        _turn(2001, "ws", base, role="system", source="user_interjection"),
        _turn(2002, "ws", base, role="system", source="user_interjection"),
    ]
    events = [
        _event("ev-a", "ws", base - datetime.timedelta(seconds=1.393)),
        _event("ev-b", "ws", base - datetime.timedelta(seconds=0.711)),
    ]

    first = prov.match_events_to_turns(events, turns)
    second = prov.match_events_to_turns(list(reversed(events)), turns)

    by_event_first = {m["event"]["event_id"]: m["turn"]["id"] for m in first}
    by_event_second = {m["event"]["event_id"]: m["turn"]["id"] for m in second}
    assert by_event_first == by_event_second, (
        "the attribution must not depend on the order the events arrive in"
    )


# ------------------------------------------------- the properties that hold


def test_an_ordinary_event_still_matches_its_own_row():
    """The common case must be untouched: one event, one row, no contention."""
    when = datetime.datetime(2026, 10, 18, 14, 0, 4, tzinfo=UTC)
    turns = [_turn(1001, "ws", when)]
    events = [_event("ev-a", "ws", when + datetime.timedelta(seconds=1))]

    matched = prov.match_events_to_turns(events, turns)
    assert len(matched) == 1
    assert matched[0]["turn"]["id"] == 1001
    assert matched[0]["event"]["event_id"] == "ev-a"


def test_an_event_with_no_candidate_stays_unmatched():
    """An event far from every row must remain unmatched, not steal one."""
    when = datetime.datetime(2026, 10, 18, 14, 0, 4, tzinfo=UTC)
    turns = [_turn(1001, "ws", when)]
    events = [_event("ev-a", "ws", when + datetime.timedelta(seconds=600))]

    assert prov.match_events_to_turns(events, turns) == []


def test_a_row_outside_the_window_is_not_claimed():
    when = datetime.datetime(2026, 10, 18, 14, 0, 4, tzinfo=UTC)
    turns = [_turn(1001, "ws", when)]
    events = [_event("ev-a", "ws", when + datetime.timedelta(seconds=60))]

    assert prov.match_events_to_turns(events, turns) == []


def test_an_operator_row_outranks_a_system_row_at_equal_distance():
    """The existing tie rule survives: operator origin wins at equal distance."""
    when = datetime.datetime(2026, 10, 18, 14, 0, 4, tzinfo=UTC)
    turns = [
        _turn(1, "ws", when, role="system", source="memory_pointer"),
        _turn(2, "ws", when, role="user"),
    ]
    events = [_event("ev-a", "ws", when + datetime.timedelta(seconds=1))]

    matched = prov.match_events_to_turns(events, turns)
    assert len(matched) == 1
    assert matched[0]["turn"]["id"] == 2, "the operator row must win the tie"


def test_two_events_two_rows_each_get_their_own():
    """Contention resolves to a bijection when there is one to be had."""
    t0 = datetime.datetime(2026, 10, 18, 14, 0, 0, tzinfo=UTC)
    turns = [_turn(1, "ws", t0), _turn(2, "ws", t0 + datetime.timedelta(seconds=10))]
    events = [
        _event("ev-a", "ws", t0 + datetime.timedelta(seconds=1)),
        _event("ev-b", "ws", t0 + datetime.timedelta(seconds=11)),
    ]

    matched = prov.match_events_to_turns(events, turns)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert by_event == {"ev-a": 1, "ev-b": 2}


# ----------------------------------------------- eligibility: operator events only


def test_a_delegation_result_is_not_attributed_to_an_operator_turn():
    """RED against the type-blind matcher: machine work counted as operator work.

    Live shape (epoch s6-t2): a ``delegation_result`` event whose fingerprint
    matches NO row anywhere in the store - its projection is the delegation
    outcome, not the operator's words. The old nearest-neighbour match credited
    it to a genuine ``user_interjection`` row on temporal proximity alone.
    That both counted machine work as operator work AND double-claimed the row
    the real operator event needed. The row and event ids are not reproduced
    here: a published row id is a lookup key into the live store.
    """
    when = datetime.datetime(2026, 10, 19, 18, 43, 0, tzinfo=UTC)
    turns = [_turn(2003, "ws", when, role="system", source="user_interjection")]
    events = [_event("ev-deleg", "ws", when, event_type="delegation_result")]

    assert prov.match_events_to_turns(events, turns) == [], (
        "a delegation result must not be attributed to an operator turn"
    )


def test_a_retry_transition_is_not_attributed_to_an_operator_turn():
    """A failed model attempt is the system's work, not the operator's."""
    when = datetime.datetime(2026, 10, 19, 18, 43, 0, tzinfo=UTC)
    turns = [_turn(1, "ws", when)]
    events = [_event("ev-retry", "ws", when, event_type="retry_transition")]

    assert prov.match_events_to_turns(events, turns) == []


def test_a_task_agent_failure_is_not_attributed_to_an_operator_turn():
    when = datetime.datetime(2026, 10, 19, 18, 43, 0, tzinfo=UTC)
    turns = [_turn(1, "ws", when)]
    events = [_event("ev-fail", "ws", when, event_type="task_agent_failure")]

    assert prov.match_events_to_turns(events, turns) == []


def test_a_system_transition_does_not_take_the_operator_rows_row():
    """The live defect, exactly: a delegation result must not steal an
    operator row.

    Two events, 1.5 s apart, both within the window of one operator row. The
    operator event must win the row, because that is the row whose fingerprint
    it carries.
    """
    base = datetime.datetime(2026, 10, 19, 18, 43, 0, tzinfo=UTC)
    turns = [_turn(2003, "ws", base, role="system", source="user_interjection")]
    events = [
        _event("ev-deleg", "ws", base, event_type="delegation_result"),
        _event("ev-op", "ws", base + datetime.timedelta(seconds=1.5)),
    ]

    matched = prov.match_events_to_turns(events, turns)
    by_event = {m["event"]["event_id"]: m["turn"]["id"] for m in matched}
    assert by_event == {"ev-op": 2003}, by_event


def test_an_event_with_no_type_is_still_attributed():
    """An event with no recorded type is treated as an operator event.

    The type is a REFINEMENT, not a new requirement: a caller that does not
    carry it must keep working, exactly as before. This is what lets the
    eligibility rule be added without changing every caller's contract.
    """
    when = datetime.datetime(2026, 10, 18, 14, 0, 4, tzinfo=UTC)
    turns = [_turn(1001, "ws", when)]
    event = _event("ev-a", "ws", when)
    del event["event_type"]

    matched = prov.match_events_to_turns([event], turns)
    assert len(matched) == 1
    assert matched[0]["turn"]["id"] == 1001


def test_reconcile_reports_a_system_transition_in_its_own_bucket():
    """A system transition is visible, not hidden and not counted as operator work.

    The old matcher either counted it as operator work or dropped it. It must
    now land in its own reported bucket, so nothing disappears from the
    accounting.
    """
    when = datetime.datetime(2026, 10, 19, 18, 43, 0, tzinfo=UTC)
    store = {"turns": [_turn(2003, "ws", when, role="system",
                            source="user_interjection")]}
    events = [
        _event("ev-deleg", "ws", when, event_type="delegation_result"),
        _event("ev-op", "ws", when + datetime.timedelta(seconds=1.5)),
    ]
    audit = {"created": {}, "deleted": set(), "usage": {}, "users": {}}

    result = prov.reconcile(events=events, store=store, audit=audit)

    assert result["B_verified_operator"] == 1, "the operator event is the work"
    assert result["C3_system_transitions"] == 1, (
        "the system transition must be reported, not dropped"
    )
    assert result["C_unverified"] == 0
    classifications = {
        e["event_id"]: e.get("classification") for e in result["ledger"]
    }
    assert classifications["ev-deleg"] == "system_transition"
    assert classifications["ev-op"] == "verified_operator"
