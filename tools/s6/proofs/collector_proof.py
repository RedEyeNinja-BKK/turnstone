#!/usr/bin/env python3
"""Proof for the collector's two measurement dimensions.

`collect.py` decides what reaches the 100-observation gate. Two independent
questions, previously conflated:

  DIMENSION 1  was this an EVENT?  An admitted cadence row at/after T1 with a
               matching observation. Admitted before T1 -> straddle, excluded.
               No admitted row at all -> orphan, integrity failure.

  DIMENSION 2  was this a MEASUREMENT?  All five graded signals available AND
               the observation grounded in the instruction. A row whose
               content_basis is "attachments_only" graded a descriptor, not the
               document: it is an event, never a qualified measurement.

Fixtures are built in a throwaway directory; no live epoch, telemetry or
database is touched.

Run: /opt/turnstone-venv/bin/python s6/evidence/collector_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

COLLECT = pathlib.Path(__file__).resolve().parents[1] / "collect.py"
spec = importlib.util.spec_from_file_location("s6collect_proof", COLLECT)
col = importlib.util.module_from_spec(spec)
spec.loader.exec_module(col)

PASS = 0
FAIL = 0

T1_TEXT = "2026-10-18T03:43:02Z"
T1 = datetime.strptime(T1_TEXT, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
SERVICE = "turnstone-node-2"


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        print(f"PASS  {label}")
        PASS += 1
    else:
        print(f"FAIL  {label}: got {got!r} want {want!r}")
        FAIL += 1


def ts(offset_s: float) -> float:
    return (T1 + timedelta(seconds=offset_s)).timestamp()


def admitted(event_id: str, offset_s: float, **kw) -> dict:
    return {"event": "operator_instruction", "event_id": event_id,
            "outcome": "sensor_call", "ts": ts(offset_s), **kw}


def observation(event_id: str, offset_s: float, *, signals: int = 5,
                basis: str | None = None, **kw) -> dict:
    row = {
        "event": "operator_instruction",
        "event_id": event_id,
        "timestamp": ts(offset_s),
        "backend": "direct-openrouter-span-01-lite",
        "signals": [
            {"name": name, "available": i < signals}
            for i, name in enumerate(col.GRADED_SIGNALS)
        ],
    }
    if basis is not None:
        row["content_basis"] = basis
    row.update(kw)
    return row


def build(root: pathlib.Path, *, name: str = "s6-proof", t1: str | None = T1_TEXT,
          cadence: list[dict] | None = None,
          observations: list[dict] | None = None) -> pathlib.Path:
    epoch = root / name
    (epoch / SERVICE).mkdir(parents=True)
    if t1 is not None:
        (epoch / "T1").write_text(t1 + "\n")
    for filename, rows in (("shadow-cadence.jsonl", cadence or []),
                           ("shadow-observations.jsonl", observations or [])):
        (epoch / SERVICE / filename).write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
    return epoch


def run(**kw) -> dict:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        epoch = build(pathlib.Path(tmp), **kw)
        return col.collect(epoch)


def main() -> int:
    # ---- dimension 1 + 2: a real, grounded, fully graded event ---------------
    r = run(cadence=[admitted("e1", 10)],
            observations=[observation("e1", 12, basis="text")])
    check("grounded full grading counts once",
          (r["unique_observations"], r["coverage"]["graded_full"]), (1, 1))
    check("grounded full grading has no structural rows",
          r["coverage"]["graded_structural_only"], 0)
    check("qualified rate is 1.0", r["coverage"]["qualified_signal_rate"], 1.0)
    check("basis is reported", r["content_bases"], {"text": 1})

    # ---- a descriptor-only grading is an EVENT but NOT a measurement ---------
    r = run(cadence=[admitted("e2", 10)],
            observations=[observation("e2", 12, basis="attachments_only")])
    check("descriptor-only still counts as an event",
          r["unique_observations"], 1)
    check("descriptor-only is excluded from graded_full",
          r["coverage"]["graded_full"], 0)
    check("descriptor-only is counted as structural",
          r["coverage"]["graded_structural_only"], 1)
    check("descriptor-only qualified rate is 0.0",
          r["coverage"]["qualified_signal_rate"], 0.0)
    # Dimension B excludes it from the DENOMINATOR as well: the grader never
    # saw the document, so this is neither a success nor a grading failure.
    check("descriptor-only is not in the semantic denominator",
          r["coverage"]["semantic_gradeable_events"], 0)
    check("descriptor-only leaves the semantic rate undefined",
          r["coverage"]["qualified_semantic_rate"], None)

    # ---- text + attachments is grounded: the text WAS graded ----------------
    r = run(cadence=[admitted("e3", 10)],
            observations=[observation("e3", 12, basis="text_plus_attachments")])
    check("text_plus_attachments grades in full",
          r["coverage"]["graded_full"], 1)
    # ...but the attachment's contents were NOT in the graded representation,
    # so it is reported as a distinct, weaker coverage claim.
    check("attachment context is counted separately",
          r["coverage"]["graded_text_with_attachment_context"], 1)
    check("attachment context rate over graded",
          r["coverage"]["attachment_context_rate"], 1.0)
    check("text-only grading is not attachment context",
          run(cadence=[admitted("e3b", 10)],
              observations=[observation("e3b", 12, basis="text")])["coverage"]
          ["graded_text_with_attachment_context"], 0)
    check("structural-only grading is not attachment context",
          run(cadence=[admitted("e3c", 10)],
              observations=[observation("e3c", 12, basis="attachments_only")])
          ["coverage"]["graded_text_with_attachment_context"], 0)

    # ---- legacy row: no basis field, predecessor code graded text -----------
    r = run(cadence=[admitted("e4", 10)],
            observations=[observation("e4", 12)])
    check("legacy row grades in full (predecessor was text-only)",
          r["coverage"]["graded_full"], 1)
    check("legacy row leaves the basis counter empty", r["content_bases"], {})

    # ---- a whole attachment-only workload: dimension A grows, B stays empty --
    r = run(cadence=[admitted(f"a{i}", 10 + i) for i in range(20)],
            observations=[observation(f"a{i}", 11 + i, basis="attachments_only")
                          for i in range(20)])
    check("attachment-only workload: 20 events for dimension A",
          r["unique_observations"], 20)
    check("attachment-only workload: zero graded in full",
          r["coverage"]["semantic_graded_events"], 0)
    check("attachment-only workload: empty semantic denominator",
          r["coverage"]["semantic_gradeable_events"], 0)
    check("attachment-only workload: all 20 reported structural",
          r["coverage"]["graded_structural_only"], 20)

    # ---- an incomplete signal set is neither bucket, whatever the basis -----
    r = run(cadence=[admitted("e5", 10)],
            observations=[observation("e5", 12, signals=4, basis="text")])
    check("4 of 5 signals is an event", r["unique_observations"], 1)
    check("4 of 5 signals is not graded",
          (r["coverage"]["graded_full"], r["coverage"]["graded_structural_only"]),
          (0, 0))
    check("4 of 5 signals scores zero qualified rate",
          r["coverage"]["qualified_signal_rate"], 0.0)

    # ---- mixed population: the rate is over events, not over graded rows ----
    r = run(cadence=[admitted(f"m{i}", 10 + i) for i in range(4)],
            observations=[
                observation("m0", 11, basis="text"),
                observation("m1", 12, basis="text_plus_attachments"),
                observation("m2", 13, basis="attachments_only"),
                observation("m3", 14, signals=3, basis="text"),
            ])
    check("mixed: 4 events", r["unique_observations"], 4)
    check("mixed: 2 graded in full", r["coverage"]["graded_full"], 2)
    check("mixed: 1 structural", r["coverage"]["graded_structural_only"], 1)
    check("mixed: qualified rate is 0.5",
          r["coverage"]["qualified_signal_rate"], 0.5)
    check("mixed: one graded observation had attachment context",
          r["coverage"]["graded_text_with_attachment_context"], 1)
    check("mixed: attachment context rate over graded is 0.5",
          r["coverage"]["attachment_context_rate"], 0.5)
    # Dimension B: 3 gradeable (text, text+att, text-with-missing-signal),
    # 2 graded, 0.667. The structural row is outside B entirely.
    check("mixed: semantic denominator excludes attachments_only",
          r["coverage"]["semantic_gradeable_events"], 3)
    check("mixed: semantic graded count",
          r["coverage"]["semantic_graded_events"], 2)
    check("mixed: gradeable but ungraded",
          r["coverage"]["semantic_ungraded_events"], 1)
    check("mixed: qualified semantic rate over gradeable events",
          r["coverage"]["qualified_semantic_rate"], 0.667)
    check("mixed: bases are itemised",
          r["content_bases"],
          {"text": 2, "text_plus_attachments": 1, "attachments_only": 1})

    # ---- pre-T1 admission, post-T1 observation: reported, never counted -----
    r = run(cadence=[admitted("s1", -100)],
            observations=[observation("s1", +10, basis="text")])
    check("straddle: no acceptance observations", r["unique_observations"], 0)
    check("straddle: counted separately",
          (r["straddling_admissions"]["admitted"],
           r["straddling_admissions"]["observations"]), (1, 1))
    check("straddle: not graded into acceptance",
          r["coverage"]["graded_full"], 0)

    # ---- an observation with no admitted cadence row is an integrity failure -
    r = run(cadence=[], observations=[observation("o1", 10, basis="text")])
    check("orphan is counted", r["orphan_observations"], 1)
    check("orphan fails the integrity gate", r["gate"]["no_orphans_ok"], False)
    check("orphan is not an acceptance event", r["unique_observations"], 0)

    # ---- historical and markerless epochs fail closed -----------------------
    r = run(name="s6-t0", cadence=[admitted("h1", 10)],
            observations=[observation("h1", 12, basis="text")])
    check("historical epoch reports zero acceptance", r["unique_observations"], 0)
    check("historical epoch is flagged", r["historical"], True)

    r = run(t1=None, cadence=[admitted("n1", 10)],
            observations=[observation("n1", 12, basis="text")])
    check("no T1 marker: zero acceptance", r["unique_observations"], 0)
    check("no T1 marker: gate not met", r["gate"]["observations"]["met"], False)

    # ---- the gate speaks about EVENTS ONLY, never about grading ------------
    r = run(cadence=[admitted(f"g{i}", 10 + i) for i in range(100)],
            observations=[observation(f"g{i}", 11 + i, basis="attachments_only")
                          for i in range(100)])
    check("100 descriptor-only events satisfy the observation gate",
          r["gate"]["observations"]["met"], True)
    check("...and grade nothing at all",
          r["coverage"]["semantic_graded_events"], 0)
    check("...with the semantic rate undefined, not 1.0",
          r["coverage"]["qualified_semantic_rate"], None)
    check("the gate exposes no grading key to be misread",
          sorted(k for k in r["gate"] if "grad" in k or "semantic" in k), [])

    # ---- 99 vs 100 is the boundary the gate actually enforces ---------------
    r = run(cadence=[admitted(f"g{i}", 10 + i) for i in range(100)],
            observations=[observation(f"g{i}", 11 + i, basis="text")
                          for i in range(100)])
    check("100 events meet the observation gate",
          r["gate"]["observations"]["met"], True)
    r = run(cadence=[admitted(f"g{i}", 10 + i) for i in range(99)],
            observations=[observation(f"g{i}", 11 + i, basis="text")
                          for i in range(99)])
    check("99 events do not meet it", r["gate"]["observations"]["met"], False)

    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
