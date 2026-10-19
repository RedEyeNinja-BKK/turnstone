#!/usr/bin/env python3
"""Watch-budget check — READ ONLY.

A watch's ``max_polls`` is a LIFETIME cap: the runner increments ``poll_count``
once per poll, never resets it, and ends the watch at ``poll_count >= max_polls``
whether or not its stop condition ever fired. A campaign watch that quietly
runs out of budget looks exactly like a campaign that is still running, because
no poll ever fires to say otherwise.

This script answers "how much monitoring is left?" for one watch, as JSON, so a
watch condition can fire on it:

    exit_code != 0 or not data or data["warn"] == true or not data["target_active"]

``warn`` is true once the remaining budget falls below ``--warn-fraction`` of
the lifetime (default 10%), which at a 30-minute cadence is weeks of notice.

Read-only in every direction: SELECT-only SQL, no writes, no service calls.

Usage:
    watch_budget.py --json
    watch_budget.py --watch s6-t1-liveness --warn-fraction 0.1 --json

Exit codes: 0 report produced, 2 setup error (unreadable store / unknown watch).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_WATCH = "s6-t1-liveness"
DEFAULT_DSN = "dbname=turnstone"


def load(conn, name: str):
    row = conn.execute(
        "select watch_id, name, interval_secs, max_polls, poll_count, active,"
        " last_poll, next_poll, last_exit_code"
        " from watches where name = %s",
        (name,),
    ).fetchone()
    if row is None:
        return None
    keys = ("watch_id", "name", "interval_secs", "max_polls", "poll_count",
            "active", "last_poll", "next_poll", "last_exit_code")
    return dict(zip(keys, row))


def report(watch: dict, warn_fraction: float) -> dict:
    interval = float(watch["interval_secs"])
    interval_h = interval / 3600.0
    max_polls = int(watch["max_polls"])
    poll_count = int(watch["poll_count"])
    remaining = max(0, max_polls - poll_count)
    remaining_h = remaining * interval_h
    fraction_left = (remaining / max_polls) if max_polls else 0.0

    # The runner ends the watch on the poll that takes poll_count to max_polls.
    # If next_poll is set, that poll is due one interval after it, plus one
    # interval for each poll in between.
    expiry = None
    if watch["next_poll"]:
        try:
            nxt = datetime.strptime(watch["next_poll"], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc)
            expiry = nxt + timedelta(seconds=interval * max(0, remaining - 1))
        except ValueError:
            expiry = None

    return {
        "watch": watch["name"],
        "watch_id": (watch["watch_id"] or "")[:8],
        "target_active": bool(watch["active"]),
        "interval_secs": interval,
        "poll_count": poll_count,
        "max_polls": max_polls,
        "polls_remaining": remaining,
        "hours_remaining": round(remaining_h, 1),
        "days_remaining": round(remaining_h / 24.0, 1),
        "fraction_left": round(fraction_left, 3),
        "expires_utc": expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if expiry else None,
        "last_poll": watch["last_poll"],
        "last_exit_code": watch["last_exit_code"],
        "warn": (remaining > 0 and fraction_left < warn_fraction) or remaining == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", default=DEFAULT_WATCH)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--warn-fraction", type=float, default=0.10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        import psycopg

        with psycopg.connect(args.dsn) as conn:
            conn.execute("set transaction read only")
            watch = load(conn, args.watch)
    except Exception as error:  # noqa: BLE001 - a check must never crash a watch
        print(f"cannot read the watch store: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 2

    if watch is None:
        print(f"no watch named {args.watch!r}", file=sys.stderr)
        return 2

    data = report(watch, args.warn_fraction)
    if args.json:
        print(json.dumps(data))
    else:
        print(f"watch {data['watch']} ({data['watch_id']}) "
              f"active={data['target_active']}")
        print(f"  polls  : {data['poll_count']}/{data['max_polls']} "
              f"({data['polls_remaining']} left)")
        print(f"  time   : {data['days_remaining']} d left, "
              f"expires {data['expires_utc']}")
        print(f"  warn   : {data['warn']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
