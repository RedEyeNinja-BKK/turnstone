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
from datetime import datetime, timezone

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
#: Measured live on 2026-10-19, epoch s6-t2: one ``delegation_result``
#: event's fingerprint matches NO row anywhere in the store, and the old
#: nearest-neighbour match credited it to a genuine ``user_interjection`` row
#: on temporal proximity alone. That same row was then credited a second time
#: to the genuine ``operator_instruction`` event, so the double-claim HID a
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


def match_events_to_turns(
    events: list[dict],
    turns: list[dict],
    window: float = MATCH_WINDOW_S,
) -> list[dict]:
    """Attribute OPERATOR events to store rows with a ONE-TO-ONE assignment.

    Why this exists (measured live on 2026-10-19, epoch s6-t2): the
    independent nearest-neighbour match can hand the SAME row to two events.
    One store row was claimed by both of two admitted events 0.68 s apart,
    because the two candidate interjection rows carry the same
    second-resolution timestamp and both events tie on distance. One row was
    therefore credited twice and one event was attributed to the wrong turn.

    The fingerprints settle the tie independently of the matcher::

        c3eab1e49e95a55d087a90d82eee66bd == "do not modify the staging host"
        7424fc427f7909b93ee2ec1b058862a4 == "scope is limited to the second node"

    These are SYNTHETIC messages with RECOMPUTED fingerprints. The originals
    were real operator text, and the fingerprint is an unsalted SHA-256 of
    the projection - so publishing the text beside its digest published the
    content AND a lookup key for it, and the digest of a real message is a
    confirmation oracle (guess the message, hash it, compare). The property
    the test needs - two DISTINCT digests over two DISTINCT messages - is
    preserved exactly.

    TWO rules, both required:

    1. ELIGIBILITY. Only ``operator_instruction`` events are attributed to
       store rows. The other material events are the system's own transitions
       (a retry, a delegation result, a task-agent failure); their projections
       are built from a cause, a prompt and a result, not from the operator's
       instruction, so they can never be the operator turn that produced an
       operator observation. Attributing them to an operator row counts
       machine work as operator work. They are reported in their own bucket
       instead - see :func:`reconcile`.

    2. ONE ROW, ONE EVENT. The assignment consumes each row at most once.
       Events are processed in timestamp order, and each event takes its best
       still-unclaimed candidate, ranked by (distance, operator-origin first,
       row id). The row id is the final tie-break so the result never depends
       on the order the events arrive in.

    A row that is not claimed stays unclaimed. An event with no candidate
    stays unmatched - it is not allowed to steal a row from a better-matched
    event.
    """
    by_workstream: dict[str, list[dict]] = {}
    for turn in turns:
        if turn["ts"] is None or turn["role"] not in TURN_ROLES:
            continue
        by_workstream.setdefault(turn["ws_id"], []).append(turn)

    claimed: set[int] = set()
    matched: list[dict] = []
    for event in sorted(events, key=lambda e: (e["ts"], str(e.get("event_id", "")))):
        if event.get("event_type") not in (None, OPERATOR_EVENT):
            # Not operator instruction work: see OPERATOR_EVENT.
            continue
        when = event["ts"]
        if when is None:
            continue
        best, best_key = None, None
        for turn in by_workstream.get(event["ws_id"], ()):
            if turn["id"] in claimed:
                continue
            delta = abs((turn["ts"] - when).total_seconds())
            if delta > window:
                continue
            # (distance, 0 for operator, row id) - operator first at equal
            # distance, row id last so the outcome is order-independent.
            key = (round(delta, 3), 0 if turn["origin"] == "operator" else 1, turn["id"])
            if best_key is None or key < best_key:
                best, best_key = turn, key
        if best is None:
            continue
        claimed.add(best["id"])
        matched.append({
            "event": event,
            "turn": best,
            "distance_s": best_key[0],
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
    # events. The independent nearest-neighbour match could hand one row to
    # two events, which double-credits a row and mis-attributes an event.
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
