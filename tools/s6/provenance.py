#!/usr/bin/env python3
"""Event provenance for S6 acceptance accounting — READ ONLY.

`collect.py` answers "how many observation rows exist?" and `liveness.py`
answers "did the sensor see the work?". Neither answers the question the
acceptance gate actually needs: **how many of those rows are independently
attributable to genuine operator work?**

That gap produced three distinct findings on 2026-10-18, all invisible to a
raw row count:

  * an observation whose workstream was DELETED — the conversation rows were
    cascaded away, so the row cannot be matched to a stored message even
    though authoritative audit records prove an operator created the
    workstream and drove seven model calls in it;
  * a genuine operator message persisted as ``role='system'``
    (``_source='user_interjection'``) — real work the store-side denominator
    did not count;
  * a distinct operator turn DISCARDED by dedupe because its privacy-preserving
    structural descriptor matched an earlier turn's.

This module classifies, and never guesses. Every admitted event lands in
exactly one bucket, and the buckets are reported side by side rather than
summed into one flattering number.

Read-only in every direction: SELECT-only SQL, no writes, no service calls, no
network beyond the local database socket.

Usage:
    /opt/turnstone-venv/bin/python provenance.py --epoch /var/lib/turnstone/sensor/s6-t2
    /opt/turnstone-venv/bin/python provenance.py --json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_DSN = "dbname=turnstone"
#: A sensor event and the operator turn that caused it are written within the
#: same request. Five seconds absorbs commit latency without ever reaching a
#: neighbouring turn (the closest pair in the recorded history is 34 s apart).
MATCH_WINDOW_S = 5.0
#: The audit write and the sensor admission happen in the same turn; two
#: seconds is enough and still cannot reach another submission.
AUDIT_WINDOW_S = 2.0

#: ``_source`` values that carry a REAL operator message. Everything else in
#: ``SYSTEM_TURN_SOURCES`` is machine-authored and must never count as work.
#: `user_interjection` is a queued operator message rendered into a system
#: turn: the sender identity and client_send_ids prove the origin.
OPERATOR_SOURCES = frozenset({"user_interjection"})
#: A system-authored turn that is nonetheless persisted with ``role='user'``.
#: Proof that role alone is not an origin signal.
NON_OPERATOR_USER_SOURCES = frozenset({"system_nudge"})


def load_collector():
    spec = importlib.util.spec_from_file_location("s6collect", HERE / "collect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_row(role: str, source: str | None) -> str:
    """Origin of one conversation row: ``operator`` or ``system``.

    Two independent fields are needed, and neither is sufficient:
    ``role='user'`` is not enough (``system_nudge`` rides it), and
    ``role='system'`` is not disqualifying (``user_interjection`` is an
    operator message).
    """
    if source in OPERATOR_SOURCES:
        return "operator"
    if source in NON_OPERATOR_USER_SOURCES:
        return "system"
    if role == "user":
        return "operator"
    return "system"


def _ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_store(dsn: str) -> dict:
    """Operator-origin and system turns since the earliest epoch boundary."""
    import psycopg

    query = """
        select c.id, c.ws_id, c.timestamp, c.role, c._source, c.commit_key,
               c.attachments, coalesce(c.content, ''), c.meta
          from conversations c
         order by c.timestamp
    """
    turns = []
    with psycopg.connect(dsn) as conn:
        conn.execute("set transaction read only")
        for row in conn.execute(query):
            (row_id, ws_id, timestamp, role, source, commit_key,
             attachments, content, meta) = row
            origin = classify_row(role or "", source)
            turns.append({
                "id": row_id,
                "ws_id": ws_id,
                "ts": _ts(timestamp),
                "role": role,
                "source": source,
                "commit_key": commit_key,
                "has_attachment": bool(attachments and attachments not in ("", "[]")),
                "content": content,
                "meta": meta,
                "origin": origin,
            })
    return {"turns": turns}


def fetch_audit(dsn: str) -> dict:
    """Workstream lifecycle + usage records: the authoritative fallback when
    conversation rows have been erased by a workstream deletion."""
    import psycopg

    created = {}
    deleted = set()
    usage: dict[str, int] = {}
    users: dict[str, set] = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("set transaction read only")
        for ts, user_id, action, resource_id in conn.execute(
            "select timestamp, user_id, action, resource_id from audit_events "
            "where action in ('workstream.created','workstream.deleted') "
            "order by timestamp"
        ):
            if action == "workstream.created":
                created[resource_id] = {"ts": _ts(ts), "user_id": user_id}
            else:
                deleted.add(resource_id)
        for ts, user_id, ws_id in conn.execute(
            "select timestamp, user_id, ws_id from usage_events "
            "where ws_id is not null and ws_id <> ''"
        ):
            usage[ws_id] = usage.get(ws_id, 0) + 1
            users.setdefault(ws_id, set()).add(user_id)
    return {"created": created, "deleted": deleted, "usage": usage, "users": users}


#: Roles that can be the operator turn that produced an event. ``tool`` and
#: ``assistant`` rows share the timestamp and must never be matched: doing so
#: classified a genuine ``user_interjection`` and a genuine operator message as
#: system-generated, because a tool row sorting first won the tie.
TURN_ROLES = ("user", "system")

#: The ONLY event that represents operator instruction work.
#:
#: The other material events are the SYSTEM's own transitions, not the
#: operator's words: ``retry_transition`` is a failed model attempt,
#: ``delegation_result`` is a task-agent outcome, ``task_agent_failure`` is a
#: task-agent error. Their projections are built from a cause, a prompt and a
#: result - never from the operator's instruction - so their fingerprints
#: cannot match an operator turn's projection, and attributing them to one
#: counts machine work as operator work.
#:
#: Measured live on 2026-10-19, epoch s6-t2: one ``delegation_result`` event's
#: fingerprint matches NO row anywhere in the store, and the old
#: nearest-neighbour match credited it to a genuine ``user_interjection`` row on
#: temporal proximity alone. That same row was then credited a second time to
#: the genuine ``operator_instruction`` event, so the double-claim HID a
#: second defect. The row and event ids are omitted here: they are real
#: production row ids, and a published row id is a lookup key into the live
#: store. The property being recorded is the SHAPE of the defect - a system
#: transition matched to an operator row, and that row double-credited - and
#: the shape is what the tests reproduce.
#:
#: With this eligibility rule the live epoch attributes 42/42 admitted
#: operator events to rows whose fingerprint agrees, with no row claimed twice.
OPERATOR_EVENT = "operator_instruction"


def _nearest(turns: list[dict], ws_id: str, when: datetime, window: float):
    """The operator turn closest to ``when`` in ``ws_id``.

    Ties are common - a turn and its advisory rows share one timestamp - so an
    operator-origin row always outranks a system one at equal distance, and
    non-turn roles are excluded outright.

    This is the INDEPENDENT nearest-neighbour match. It is retained for
    diagnostics, but it is NOT what attributes an event: it can hand the same
    row to two events. See :func:`match_events_to_turns` for the assignment
    that is actually used.
    """
    best, best_key = None, None
    for turn in turns:
        if turn["ws_id"] != ws_id or turn["ts"] is None:
            continue
        if turn["role"] not in TURN_ROLES:
            continue
        delta = abs((turn["ts"] - when).total_seconds())
        if delta > window:
            continue
        # (distance, 0 for operator) sorts operator first at equal distance.
        key = (round(delta, 3), 0 if turn["origin"] == "operator" else 1)
        if best_key is None or key < best_key:
            best, best_key = turn, key
    return best


#: Attribution distances are carried as integer MICROSECONDS.
#:
#: Floating-point seconds would make the total cost of a matching depend on
#: the order the summands happen to be added in, which would turn the "same
#: input, same output" guarantee into a property of floating point rather
#: than of the algorithm. Integer microseconds are exact, and the stored
#: timestamps carry microsecond resolution anyway.
_MICROSECOND = timedelta(microseconds=1)

#: Sentinel for "no path" in the shortest-path search. The distances are
#: integers (microseconds), so the search uses a float sentinel for
#: "unreachable" and integer arithmetic everywhere else.
_INF = float("inf")


def _delta_us(later: datetime, earlier: datetime) -> int:
    """The absolute distance between two instants, in whole microseconds.

    Computed from the integer microsecond fields of the two timestamps, so
    the result is EXACT - no floating-point rounding is involved anywhere in
    the cost of the assignment. ``datetime`` guarantees microsecond
    resolution, and the store's timestamps are microsecond-resolution
    ``timestamptz`` values.
    """
    delta = later - earlier
    return abs(delta.days * 86_400_000_000 + delta.seconds * 1_000_000
               + delta.microseconds)


def _candidate_key(turn: dict, distance_us: int, event_id: str) -> tuple:
    """The deterministic ranking key for one (event, row) pair.

    Ordered so that, among equally good pairings, the winner is stable:
    closest first, then an operator-origin row ahead of a system one, then
    the lower row id, then the lower event id. The event id is last so the
    outcome never depends on the order the events arrive in.
    """
    return (distance_us, 0 if turn["origin"] == "operator" else 1,
            turn["id"], str(event_id))


def _min_cost_matching(
    event_ids: list[str],
    row_ids: list[int],
    pairs: list[tuple[str, int, int]],
) -> tuple[int, int, dict[str, int]]:
    """Exact minimum-cost, maximum-cardinality bipartite matching.

    The objective is lexicographic:

      1. MAXIMISE the number of matched (event, row) pairs. A campaign is
         measured in operator turns, so a matching that credits more turns
         is better even if some pairings are less close in time. A greedy
         assignment does not achieve this: it can take a row that a later
         event needed, leaving that event unmatched while another row sat
         idle - which UNDER-reports attributable operator work.
      2. Among maximum-cardinality matchings, MINIMISE the total distance,
         so the pairing is as tight as the data allows.

    It is solved EXACTLY, by successive shortest paths over the residual
    graph. Bellman-Ford is used for each shortest-path search, so the
    negative residual edges that appear as soon as a path is re-routed are
    handled exactly rather than approximated.

    Only ADMISSIBLE pairs are passed in: a pair that is too far apart in
    time is absent from ``pairs``, and therefore can never be chosen.

    The problem sizes are small - on the live epoch the largest workstream
    had 20 operator events against at most 8 candidate rows per event - so
    the exact solver is affordable.

    Returns ``(cardinality, total_cost, {event_id: row_id})``.
    """
    epos = {e: i for i, e in enumerate(event_ids)}
    rpos = {r: i for i, r in enumerate(row_ids)}
    edges = [(epos[e], rpos[r], d) for e, r, d in pairs
             if e in epos and r in rpos]
    if not edges:
        return 0, 0, {}

    n_e, n_r = len(event_ids), len(row_ids)
    source = 0
    sink = 1 + n_e + n_r
    n_nodes = sink + 1
    graph: list[list[int]] = [[] for _ in range(n_nodes)]
    to: list[int] = []
    cap: list[int] = []
    cost: list[int] = []

    def add_edge(u: int, v: int, capacity: int, c: int) -> None:
        forward = len(to)
        graph[u].append(forward)
        to.append(v)
        cap.append(capacity)
        cost.append(c)
        graph[v].append(forward + 1)
        to.append(u)
        cap.append(0)
        cost.append(-c)

    for i in range(n_e):
        add_edge(source, 1 + i, 1, 0)
    for j in range(n_r):
        add_edge(1 + n_e + j, sink, 1, 0)
    for e_pos, r_pos, c in edges:
        add_edge(1 + e_pos, 1 + n_e + r_pos, 1, c)

    total: int = 0
    flow = 0
    while True:
        dist: list[float] = [_INF] * n_nodes
        prev = [-1] * n_nodes
        dist[source] = 0
        queue = deque([source])
        queued = [False] * n_nodes
        queued[source] = True
        while queue:
            u = queue.popleft()
            queued[u] = False
            du = dist[u]
            for edge_index in graph[u]:
                if cap[edge_index] <= 0:
                    continue
                v = to[edge_index]
                nd = du + cost[edge_index]
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = edge_index
                    if not queued[v]:
                        queued[v] = True
                        queue.append(v)
        if prev[sink] == -1:
            break
        node = sink
        while node != source:
            edge_index = prev[node]
            cap[edge_index] -= 1
            cap[edge_index ^ 1] += 1
            node = to[edge_index ^ 1]
        total += int(dist[sink])
        flow += 1

    assignment: dict[str, int] = {}
    for event_id, row_id, _d in pairs:
        if event_id in assignment:
            continue
        node = 1 + epos[event_id]
        for edge_index in graph[node]:
            if edge_index & 1:
                continue  # a reverse edge, not an event->row edge
            if cap[edge_index] != 0:
                continue  # the pair was not used
            target = to[edge_index]
            if 1 + n_e <= target < 1 + n_e + n_r:
                assignment[event_id] = row_ids[target - 1 - n_e]
                break
    return flow, total, assignment


def _lexicographic_assignment(
    pairs: list[tuple[str, int, int, tuple]],
    event_ids: list[str],
    row_ids: list[int],
) -> dict[str, int]:
    """Choose THE optimal assignment, breaking ties by the documented order.

    The minimum-cost maximum-cardinality solution is usually unique, but
    not always. When several solutions are optimal, the preferred one is
    the one that includes the EARLIEST-ranked admissible pair, where a
    pair is ranked by

        (distance, operator-origin first, row id, event id)

    That is decided exactly, not approximately: candidate pairs are
    examined in rank order, and a pair is FIXED only when an optimal
    assignment still exists that contains it. The result therefore never
    depends on the order the events arrive in.

    ``pairs`` are ``(event_id, row_id, distance_us, rank)`` tuples.
    """
    card, cost, _ = _min_cost_matching(
        event_ids, row_ids, [(e, r, d) for e, r, d, _rank in pairs])
    if card == 0:
        return {}

    chosen: dict[str, int] = {}
    chosen_rows: set[int] = set()
    remaining = list(pairs)
    live_events = list(event_ids)
    live_rows = list(row_ids)
    target_card, target_cost = card, cost

    for event_id, row_id, delta_us, _rank in sorted(pairs, key=lambda p: p[3]):
        if target_card == 0:
            break
        if event_id in chosen or row_id in chosen_rows:
            continue
        rest = [p for p in remaining if p[0] != event_id and p[1] != row_id]
        rest_events = [e for e in live_events if e != event_id]
        rest_rows = [r for r in live_rows if r != row_id]
        sub_card, sub_cost, _ = _min_cost_matching(
            rest_events, rest_rows, [(e, r, d) for e, r, d, _rk in rest])
        if 1 + sub_card != target_card:
            continue
        if delta_us + sub_cost != target_cost:
            continue
        chosen[event_id] = row_id
        chosen_rows.add(row_id)
        remaining = rest
        live_events, live_rows = rest_events, rest_rows
        target_card -= 1
        target_cost -= delta_us
    return chosen


def match_events_to_turns(
    events: list[dict],
    turns: list[dict],
    window: float = MATCH_WINDOW_S,
) -> list[dict]:
    """Attribute OPERATOR events to store rows with a ONE-TO-ONE assignment.

    Why this exists (measured live on 2026-10-19, epoch s6-t2): the
    independent nearest-neighbour match can hand the SAME row to two events.
    Two candidate interjection rows carry the same second-resolution
    timestamp, so both events tie on distance. One row was therefore
    credited twice and one event was attributed to the wrong turn.

    The fingerprints settle the tie independently of the matcher::

        c3eab1e49e95a55d087a90d82eee66bd == "do not modify the staging host"
        7424fc427f7909b93ee2ec1b058862a4 == "scope is limited to the second node"

    These are SYNTHETIC messages with recomputed fingerprints. The originals
    were real operator text, and the fingerprint is an UNSALTED SHA-256 of the
    projection - so publishing the text next to its digest published both the
    content and a lookup key for it. The digest of a real message is also a
    confirmation oracle: anyone who can guess the message can verify it
    against the published digest, and anyone holding the digest can find the
    matching row in the live telemetry. Synthetic text with a recomputed
    digest has neither property, and the property the test needs - two
    DISTINCT digests over two DISTINCT messages - is preserved exactly.

    TWO rules, both required:

    1. ELIGIBILITY. Only ``operator_instruction`` events are attributed to
       store rows. The other material events are the system's own transitions
       (a retry, a delegation result, a task-agent failure); their projections
       are built from a cause, a prompt and a result, not from the operator's
       instruction, so they can never be the operator turn that produced an
       operator observation. Attributing them to an operator row counts
       machine work as operator work. They are reported in their own bucket
       instead - see :func:`reconcile`.

    2. ONE ROW, ONE EVENT, MAXIMALLY MANY. The assignment is a MIMUM-COST
       MAXIMUM-CARDINALITY bipartite matching, solved exactly. It consumes
       each row at most once, and among all such assignments it:

         a. matches as MANY events as the data allows, and
         b. subject to that, minimises the total attribution distance.

       A greedy assignment (each event takes its best still-unclaimed
       candidate) does not achieve (a): it can take a row that a later
       event needed, leaving that event unmatched while another row sat
       idle. That UNDER-reports attributable operator work, which is the
       same class of defect as the double-claim it was written to fix.

       Ties among equally optimal assignments are broken by
       (distance, operator-origin first, row id, event id), decided
       exactly, so the result never depends on the order the events
       arrive in.

    A row that is not claimed stays unclaimed. An event with no candidate
    stays unmatched - it is not allowed to steal a row from a better-matched
    event.
    """
    by_workstream: dict[str, list[dict]] = {}
    for turn in turns:
        if turn["ts"] is None or turn["role"] not in TURN_ROLES:
            continue
        by_workstream.setdefault(turn["ws_id"], []).append(turn)

    # Workstreams are independent: a row in one workstream can never be
    # claimed by an event in another, so each workstream is solved on its
    # own and the results are merged.
    matched: list[dict] = []
    for ws_id, ws_turns in by_workstream.items():
        ws_events = [
            e for e in events
            if e.get("ws_id") == ws_id
            and e.get("event_type") in (None, OPERATOR_EVENT)
            and e.get("ts") is not None
        ]
        if not ws_events:
            continue
        matched.extend(_match_one_workstream(ws_events, ws_turns, window))
    matched.sort(key=lambda m: (m["event"]["ts"], str(m["event"].get("event_id", ""))))
    return matched


def _match_one_workstream(
    events: list[dict],
    turns: list[dict],
    window: float,
) -> list[dict]:
    """The exact one-to-one assignment for ONE workstream.

    Events and rows are matched across the whole workstream at once, not
    event by event, because that is what makes the matching maximum-
    cardinality: an event is allowed to give up its closest row to a
    later event if that is what lets both be attributed.
    """
    # The event id is the identity of an event in the assignment. Fall back
    # to a positional id when the caller did not supply one, so the
    # assignment is still well defined.
    ordered_events = sorted(
        events, key=lambda e: (e["ts"], str(e.get("event_id", ""))))
    event_keys: list[str] = []
    event_by_key: dict[str, dict] = {}
    for index, event in enumerate(ordered_events):
        key = str(event.get("event_id") or f"__idx_{index}")
        if key in event_by_key:
            # Two events with the same id: disambiguate so that neither is
            # silently dropped from the assignment.
            key = f"{key}#{index}"
        event_keys.append(key)
        event_by_key[key] = event

    ordered_turns = sorted(turns, key=lambda t: (t["ts"], t["id"]))
    turn_by_id = {t["id"]: t for t in ordered_turns}

    window_us = int(round(window * 1_000_000))
    pairs: list[tuple[str, int, int, tuple]] = []
    for event_key in event_keys:
        event = event_by_key[event_key]
        when = event["ts"]
        for turn in ordered_turns:
            delta_us = _delta_us(turn["ts"], when)
            if delta_us > window_us:
                continue
            pairs.append((event_key, turn["id"], delta_us,
                          _candidate_key(turn, delta_us, event_key)))

    # A row that is not admissible for ANY event can never be matched, so it
    # is left out of the assignment entirely. This keeps the exact solver's
    # graph proportional to the contested region rather than to the size of
    # the workstream's whole history.
    contested_rows = sorted({row_id for _e, row_id, _d, _rank in pairs})
    assignment = _lexicographic_assignment(pairs, event_keys, contested_rows)

    matched: list[dict] = []
    for event_key, row_id in assignment.items():
        event = event_by_key[event_key]
        turn = turn_by_id[row_id]
        delta_us = _delta_us(turn["ts"], event["ts"])
        matched.append({
            "event": event,
            "turn": turn,
            "distance_s": delta_us / 1_000_000,
        })
    return matched


def reconcile(
    *,
    events: list[dict],
    store: dict,
    audit: dict,
    boundary: datetime | None = None,
) -> dict:
    """Classify admitted events and suppressed cadence rows.

    ``events`` are dicts with ``event_id``, ``outcome``, ``ws_id``, ``ts``
    (datetime), ``fingerprint`` and (for admitted rows) whether an observation
    exists.

    Returns the A–F accounting plus a per-event ledger.
    """
    turns = store["turns"]
    ledger = []
    verified = unverified = system_generated = audit_attributed = 0
    suppressed_legit = suppressed_distinct = 0
    system_transitions = 0

    # Map each ADMITTED event to its conversation row, so a suppressed row can
    # be compared against the turn it was deduped against.
    admitted_by_fp: dict[tuple[str, str], dict] = {}

    # The attribution is a ONE-TO-ONE assignment over the ADMITTED OPERATOR
    # events. The independent nearest-neighbour match could hand one row to two
    # events, which double-credits a row and mis-attributes an event.
    # See match_events_to_turns for the full account.
    admitted_events = [e for e in events if e["outcome"] == "sensor_call"]
    assignment = {
        m["event"]["event_id"]: m["turn"]
        for m in match_events_to_turns(admitted_events, turns)
    }

    for event in sorted(events, key=lambda e: e["ts"]):
        row = assignment.get(event["event_id"])
        if event["outcome"] == "dedupe_suppressed":
            entry = {"kind": "suppressed", "event_id": event["event_id"],
                     "ws_id": event["ws_id"], "ts": event["ts"].isoformat(),
                     "fingerprint": event["fingerprint"]}
            if row is None:
                # Nothing to compare against: report as unclassified rather
                # than assuming either way. A suppressed row is NOT part of the
                # one-to-one assignment - it never consumed a row - so it is
                # matched independently here.
                row = _nearest(turns, event["ws_id"], event["ts"], MATCH_WINDOW_S)
            if row is None:
                entry.update(classification="suppressed_unmatched")
            else:
                previous = admitted_by_fp.get((event["ws_id"], event["fingerprint"]))
                same_turn = bool(
                    previous
                    and previous["store_id"] is not None
                    and previous["store_id"] == row["id"]
                )
                entry.update(
                    classification=("duplicate_firing" if same_turn
                                    else "distinct_turn_suppressed"),
                    store_id=row["id"],
                    store_commit_key=row["commit_key"],
                    store_origin=row["origin"],
                    previous_event=previous["event_id"] if previous else None,
                    previous_store_id=previous["store_id"] if previous else None,
                )
                if same_turn:
                    suppressed_legit += 1
                else:
                    suppressed_distinct += 1
            ledger.append(entry)
            continue

        entry = {"kind": "admitted", "event_id": event["event_id"],
                 "ws_id": event["ws_id"], "ts": event["ts"].isoformat(),
                 "fingerprint": event["fingerprint"], "observed": event["observed"]}
        if event.get("event_type") and event["event_type"] != OPERATOR_EVENT:
            # The SYSTEM's own transition - a retry, a delegation result, a
            # task-agent failure. It is real work and it is observed, but it
            # is not operator instruction, so it is reported in its own bucket
            # rather than being matched to an operator turn. Matching it to one
            # (as the old nearest-neighbour match did) counted machine work as
            # operator work AND double-claimed the operator row it took.
            entry.update(classification="system_transition",
                         event_type=event["event_type"])
            system_transitions += 1
            ledger.append(entry)
            continue
        if row is not None:
            entry.update(classification=("verified_operator" if row["origin"] == "operator"
                                         else "system_generated"),
                         store_id=row["id"], store_origin=row["origin"],
                         store_source=row["source"])
            if row["origin"] == "operator":
                verified += 1
            else:
                system_generated += 1
        else:
            recorded = audit["created"].get(event["ws_id"])
            deleted = event["ws_id"] in audit["deleted"]
            same_second = bool(
                recorded
                and abs((recorded["ts"] - event["ts"]).total_seconds()) <= AUDIT_WINDOW_S
            )
            if same_second and audit["usage"].get(event["ws_id"], 0) > 0:
                # The conversation rows were erased with the workstream, but an
                # authenticated user created it at this instant and drove model
                # calls in it. That is attributable operator work with a
                # persistence gap, not an unattributed event.
                entry.update(
                    classification="verified_operator_audit",
                    audit_user=recorded["user_id"],
                    audit_created=recorded["ts"].isoformat(),
                    usage_events=audit["usage"].get(event["ws_id"], 0),
                    history_erased=deleted,
                )
                audit_attributed += 1
            else:
                entry.update(classification="unverified")
                unverified += 1
        ledger.append(entry)
        admitted_by_fp[(event["ws_id"], event["fingerprint"])] = entry

    return {
        "A_raw_observed": sum(1 for e in ledger
                              if e["kind"] == "admitted" and e.get("observed")),
        "B_verified_operator": verified,
        "B2_verified_operator_audit": audit_attributed,
        "C_unverified": unverified,
        "C2_system_generated": system_generated,
        "C3_system_transitions": system_transitions,
        "E_legitimate_duplicates_suppressed": suppressed_legit,
        "F_distinct_turns_suppressed": suppressed_distinct,
        "match_window_s": MATCH_WINDOW_S,
        "ledger": ledger,
    }


def collect_events(epoch: pathlib.Path) -> tuple[list[dict], datetime | None]:
    """Admitted and suppressed cadence rows for one epoch, with observations."""
    collector = load_collector()
    boundary, _marker, _problem = collector.read_boundary(epoch)
    _start, window_end = collector.epoch_window(epoch)
    observations = set()
    events = []
    for service in collector.SERVICE_DIRS:
        sdir = epoch / service
        for row in collector.read_jsonl(sdir / "shadow-observations.jsonl"):
            if row.get("event_id"):
                observations.add(row["event_id"])
        for row in collector.read_jsonl(sdir / "shadow-cadence.jsonl"):
            outcome = row.get("outcome")
            if outcome not in {"sensor_call", "dedupe_suppressed"}:
                continue
            when = row.get("ts")
            if not isinstance(when, (int, float)):
                continue
            ts = datetime.fromtimestamp(when, timezone.utc)
            if boundary is not None and ts < boundary:
                continue          # pre-boundary: rollout, never acceptance
            if window_end is not None and ts >= window_end:
                continue          # a later epoch owns this event
            events.append({
                "event_id": row.get("event_id"),
                "outcome": outcome,
                # The event TYPE decides eligibility: only an
                # ``operator_instruction`` can be the operator turn that
                # produced an observation. See OPERATOR_EVENT.
                "event_type": row.get("event") or "",
                "ws_id": row.get("workstream") or "",
                "fingerprint": row.get("fingerprint") or "",
                "ts": ts,
                "observed": row.get("event_id") in observations,
                "service": service,
            })
    return events, boundary, window_end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epoch", default=None)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    collector = load_collector()
    try:
        epoch, selection, _skipped = collector.resolve_epoch(args.epoch)
    except LookupError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not epoch.is_dir():
        print(f"epoch directory not found: {epoch}", file=sys.stderr)
        return 2

    events, boundary, window_end = collect_events(epoch)
    try:
        store = fetch_store(args.dsn)
        audit = fetch_audit(args.dsn)
    except Exception as error:  # noqa: BLE001 - a report must never crash
        print(f"cannot read the conversation store: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 2

    report = reconcile(events=events, store=store, audit=audit, boundary=boundary)
    report["epoch"] = str(epoch)
    report["epoch_selection"] = selection
    report["boundary"] = boundary.isoformat().replace("+00:00", "Z") if boundary else None
    report["window_end"] = window_end.isoformat().replace("+00:00", "Z") if window_end else None

    if args.json:
        print(json.dumps(report, indent=1))
        return 0

    print("S6 EVENT PROVENANCE (read-only)")
    print("=" * 62)
    print(f"epoch        : {report['epoch']}")
    print(f"boundary     : {report['boundary']}"
          + (f"  (window closes {report['window_end']})" if report.get("window_end") else ""))
    print()
    print("ACCEPTANCE ACCOUNTING (separate, never summed into one number)")
    print(f"  A raw admitted + observed          : {report['A_raw_observed']}")
    print(f"  B verified operator (in store)     : {report['B_verified_operator']}")
    if report["B2_verified_operator_audit"]:
        print(f"  B2 attributed outside store        : "
              f"{report['B2_verified_operator_audit']}"
              "  (history erased; audit+usage evidence)")
    print(f"  B+B2 total attributable            : "
          f"{report['B_verified_operator'] + report['B2_verified_operator_audit']}")
    print(f"  C unverified provenance            : {report['C_unverified']}")
    if report["C2_system_generated"]:
        print(f"      system-generated (not work)    : {report['C2_system_generated']}")
    print(f"  E legitimate duplicates suppressed : "
          f"{report['E_legitimate_duplicates_suppressed']}")
    print(f"  F distinct turns suppressed        : "
          f"{report['F_distinct_turns_suppressed']}")
    print()
    print("LEDGER")
    for entry in report["ledger"]:
        print(f"  {entry['ts'][:19]}  {entry['ws_id'][:8]}  "
              f"{entry['classification']:<28s} eid={entry['event_id'][:8]}")
        if entry.get("history_erased"):
            print(f"      workstream since deleted: conversation rows erased "
                  f"(audit user {entry.get('audit_user')}, "
                  f"{entry.get('usage_events')} model calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
