#!/usr/bin/env python3
"""Proof that pre-T1 rows cannot enter the S6 acceptance count.

Builds synthetic epochs in /var/tmp and drives the real collect.py. No
production telemetry is read or written.

Run: /opt/turnstone-venv/bin/python s6/evidence/t1_exclusion_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timezone

COLLECT = pathlib.Path(__file__).resolve().parents[1] / "collect.py"
spec = importlib.util.spec_from_file_location("s6collect", COLLECT)
collect_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect_mod)

T1_DT = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
T1 = T1_DT.timestamp()

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        print(f"PASS  {label}")
        PASS += 1
    else:
        print(f"FAIL  {label}: got {got!r} want {want!r}")
        FAIL += 1


def admitted(event_id: str, ts: float, workstream: str = "ws-1") -> dict:
    return {
        "ts": ts,
        "event": "operator_instruction",
        "outcome": "sensor_call",
        "workstream": workstream,
        "event_id": event_id,
        "process_id": "turnstone-node-2:999",
    }


def observation(event_id: str, ts: float, workstream: str = "ws-1") -> dict:
    return {
        "event_id": event_id,
        "timestamp": ts,
        "workstream": workstream,
        "backend": "direct-openrouter-span-01-lite",
        "content_mode": "sanitized",
        "latency_ms": 600,
        "serialized_state_tokens": 70,
        "state_fingerprint": f"fp-{event_id}",
        "process_id": "turnstone-node-2:999",
        "signals": [
            {"name": "task_complexity", "available": True},
            {"name": "local_suitability", "available": False,
             "reason": "backend_not_qualified_for_signal"},
        ],
    }


def write_epoch(root: pathlib.Path, name: str, cadence: list[dict],
                observations: list[dict], marker: datetime | None) -> pathlib.Path:
    epoch = root / name
    for service in collect_mod.SERVICE_DIRS:
        (epoch / service).mkdir(parents=True, exist_ok=True)
    # Everything lands in the first service; the others stay empty.
    service = collect_mod.SERVICE_DIRS[0]
    if cadence:
        (epoch / service / "shadow-cadence.jsonl").write_text(
            "\n".join(json.dumps(r) for r in cadence) + "\n")
    if observations:
        (epoch / service / "shadow-observations.jsonl").write_text(
            "\n".join(json.dumps(r) for r in observations) + "\n")
    if marker is not None:
        (epoch / "T1").write_text(marker.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    return epoch


def main() -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="t1-proof-", dir="/var/tmp"))
    try:
        # ---------------------------------------------------------------- case 1
        # No T1: nothing is acceptance, even rows with post-T1 timestamps.
        ep = write_epoch(root, "no-t1",
                         [admitted("e1", T1 + 100)],
                         [observation("e1", T1 + 100)],
                         marker=None)
        res = collect_mod.collect(ep)
        check("no T1 -> zero acceptance observations", res["unique_observations"], 0)
        check("no T1 -> zero acceptance admitted", res["admitted_rows"], 0)
        check("no T1 -> rows reported as rollout-validation",
              res["rollout_validation"]["observations"], 1)
        check("no T1 -> gate not met", res["gate"]["observations"]["met"], False)
        check("no T1 -> status says rollout in progress",
              "ROLLOUT IN PROGRESS" in collect_mod.render(res), True)

        # ---------------------------------------------------------------- case 2
        # T1 present: pre-T1 rows are excluded, post-T1 rows counted.
        ep = write_epoch(
            root, "s6-t1",
            [admitted("pre-1", T1 - 60), admitted("post-1", T1 + 60)],
            [observation("pre-1", T1 - 60), observation("post-1", T1 + 60)],
            marker=T1_DT,
        )
        res = collect_mod.collect(ep)
        check("pre-T1 observation excluded", res["unique_observations"], 1)
        check("pre-T1 admitted excluded", res["admitted_rows"], 1)
        check("excluded row still reported", res["rollout_validation"]["observations"], 1)
        check("backends counted only from acceptance",
              res["backends"], {"direct-openrouter-span-01-lite": 1})

        # ---------------------------------------------------------------- case 3
        # A STRADDLING pair: admitted BEFORE T1, observed AFTER T1. This is the
        # case the acceptance rule must not inflate. It is excluded from the
        # count and reported separately, exactly once.
        ep = write_epoch(
            root, "straddle",
            [admitted("straddle-1", T1 - 1.0)],
            [observation("straddle-1", T1 + 1.0)],
            marker=T1_DT,
        )
        res = collect_mod.collect(ep)
        check("straddling pair is NOT acceptance", res["unique_observations"], 0)
        check("straddling admitted is NOT acceptance", res["admitted_rows"], 0)
        check("straddling pair reported separately",
              res["straddling_admissions"]["admitted"], 1)
        check("straddling observation reported separately",
              res["straddling_admissions"]["observations"], 1)
        check("straddling pair is not a rollout row",
              res["rollout_validation"]["admitted"], 0)
        check("straddling pair creates no orphan", res["orphan_observations"], 0)
        check("straddling pair creates no unmatched", res["unmatched_admitted"], 0)

        # --------------------------------------------------------------- case 3b
        # Admission at/after T1 with a later observation IS acceptance.
        ep = write_epoch(
            root, "accept-pair",
            [admitted("acc-1", T1 + 0.5)],
            [observation("acc-1", T1 + 2.0)],
            marker=T1_DT,
        )
        res = collect_mod.collect(ep)
        check("post-T1 admission counts", res["unique_observations"], 1)
        check("post-T1 admission is not straddling",
              res["straddling_admissions"]["admitted"], 0)

        # --------------------------------------------------------------- case 3c
        # An observation with no admission row is an integrity failure.
        ep = write_epoch(root, "orphan", [],
                         [observation("orphan-1", T1 + 5)], marker=T1_DT)
        res = collect_mod.collect(ep)
        check("orphan observation excluded from the count",
              res["unique_observations"], 0)
        check("orphan observation counted as an orphan",
              res["orphan_observations"], 1)
        check("orphan fails the integrity gate", res["gate"]["no_orphans_ok"], False)

        # ---------------------------------------------------------------- case 4
        # An undated row cannot be proven post-T1, so it is never acceptance.
        ep = write_epoch(root, "undated", [{"event_id": "u1", "outcome": "sensor_call"}],
                         [{"event_id": "u1", "backend": "x"}], marker=T1_DT)
        res = collect_mod.collect(ep)
        check("undated observation excluded", res["unique_observations"], 0)
        check("undated row reported as rollout", res["rollout_validation"]["observations"], 1)

        # ---------------------------------------------------------------- case 5
        # A historical epoch is never acceptance, even with a marker present.
        ep = write_epoch(root, "s6-t0",
                         [admitted("old-1", T1 + 60)],
                         [observation("old-1", T1 + 60)], marker=T1_DT)
        res = collect_mod.collect(ep)
        check("s6-t0 -> zero acceptance", res["unique_observations"], 0)
        check("s6-t0 -> flagged historical", res["historical"], True)
        check("s6-t0 -> render marks it INCOMPLETE",
              "INCOMPLETE / UNDERCOUNTED" in collect_mod.render(res), True)

        # ---------------------------------------------------------------- case 6
        # Epochs are never merged: s6-t1 must not see s6-t0's rows.
        write_epoch(root, "s6-t0",
                    [admitted("old-a", T1 + 60), admitted("old-b", T1 + 61)],
                    [observation("old-a", T1 + 60), observation("old-b", T1 + 61)],
                    marker=T1_DT)
        ep = write_epoch(root, "s6-t1",
                         [admitted("new-a", T1 + 60)],
                         [observation("new-a", T1 + 60)], marker=T1_DT)
        res = collect_mod.collect(ep)
        check("s6-t1 count excludes s6-t0 rows", res["unique_observations"], 1)

        print("---")
        print(f"pass={PASS} fail={FAIL}")
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
