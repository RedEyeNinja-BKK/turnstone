#!/usr/bin/env python3
"""PROOF: the published rate, the published ETA and the published gate count
are all the GATE's count.

The defect this closes (found 2026-10-20, while reading the live report):

The acceptance gate keys on ATTRIBUTABLE operator events - B + B2 from the
provenance reconciliation. The collector's rate and ETA were computed on the
RAW observation count instead, and the text render labelled that raw count as
the progress toward 100:

    unique obs    : 55 / 100
    rate          : 1.81 unique obs/h  ETA 24.8 h to 100

Both numbers are true, and neither is the gate's number. On the live epoch the
raw count was 55 and the gate was 48, so the report told the operator the
campaign was 7 events further along than it was, and projected an ETA several
hours shorter than the one the gate needs. The same raw count was also
published in ``liveness.py``'s text render under the label "acceptance unique
obs".

This is the same class of defect as the third reporting defect - the gate
publishing a raw count - in the two surfaces that fix did not reach: the
rate/ETA, and the human-readable labels.

Three properties are asserted here, on synthetic fixtures only:

  1. THE RATE AND THE ETA USE THE GATE'S COUNT. With a reconciliation
     available the rate is over B + B2; without one it is over the raw count
     AND the basis is named.
  2. THE TIME FLOOR IS STATED. The event count is only half the gate.
  3. NO SURFACE LABELS A RAW COUNT AS THE GATE COUNT.

No telemetry, database or service is touched: every epoch is a scratch
directory under /var/tmp.

Run: /opt/turnstone-venv/bin/python tools/s6/proofs/rate_basis_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent

PASS = 0
FAIL = 0

SERVICE = "turnstone-node-2"

#: The epoch window is pinned to a whole number of hours so the rate is
#: exactly computable: 10 hours of window, 10 attributable events, 1.0/h.
WINDOW_HOURS = 10
N_RAW = 12
N_B = 8
N_B2 = 2
N_ATTRIBUTABLE = N_B + N_B2  # 10


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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLLECT = load("s6collect_rate", S6 / "collect.py")
LIVENESS = load("s6liveness_rate", S6 / "liveness.py")


def admitted(event_id: str, ts: float) -> dict:
    return {"event": "operator_instruction", "event_id": event_id,
            "outcome": "sensor_call", "ts": ts}


def observation(event_id: str, ts: float) -> dict:
    return {
        "event_id": event_id,
        "timestamp": ts,
        "content_basis": "text",
        "signals": [{"name": n, "available": True} for n in COLLECT.GRADED_SIGNALS],
    }


def build(root: pathlib.Path, *, n_raw: int = N_RAW) -> tuple[pathlib.Path, str]:
    """A scratch epoch with `n_raw` admitted observations, and its T2 marker.

    The rows are stamped AFTER the boundary, so they land in the acceptance
    slice: the acceptance unit is an admission at or after the marker.
    """
    now = datetime.now(timezone.utc)
    t1 = now - timedelta(hours=WINDOW_HOURS)
    t1_text = t1.strftime("%Y-%m-%dT%H:%M:%SZ")
    base = t1.timestamp()
    epoch = root / "s6-t2"
    (epoch / SERVICE).mkdir(parents=True)
    (epoch / "T2").write_text(t1_text + "\n")
    cadence = [admitted(f"e{i}", base + 10.0 + i) for i in range(n_raw)]
    observations = [observation(f"e{i}", base + 11.0 + i) for i in range(n_raw)]
    for filename, rows in (("shadow-cadence.jsonl", cadence),
                           ("shadow-observations.jsonl", observations)):
        (epoch / SERVICE / filename).write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
    return epoch, t1_text


def provenance_for(n_b: int, n_b2: int, n_raw: int) -> dict:
    """A reconciliation in the shape provenance.py produces."""
    return {
        "A_raw_observed": n_raw,
        "B_verified_operator": n_b,
        "B2_verified_operator_audit": n_b2,
        "C_unverified": n_raw - n_b - n_b2,
        "C2_system_generated": 0,
        "C3_system_transitions": 0,
        "E_legitimate_duplicates_suppressed": 0,
        "F_distinct_turns_suppressed": 0,
        "match_window_s": 5.0,
    }


def main() -> int:
    print("RATE / ETA BASIS PROOF - the published rate is the gate's rate")
    print("=" * 66)
    print()

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp)
        epoch, _t1_text = build(root)
        prov = provenance_for(N_B, N_B2, N_RAW)

        # ---- 1. the rate and the ETA key on the ATTRIBUTABLE count ----------
        print("1. RATE AND ETA - computed on the gate's count")
        r = COLLECT.collect(epoch, provenance=prov)

        check("the rate's basis is named", r.get("rate_basis"), "attributable")
        check("the rate counts the attributable events", r.get("rate_events"),
              N_ATTRIBUTABLE)
        # 10 attributable events over a 10-hour window.
        check("the rate is over the attributable count", r["observed_per_hour"], 1.0)
        # (100 - 10) events at 1.0/h.
        check("the ETA is over the attributable count", r["eta_hours_to_100"], 90.0)

        raw_rate = round(N_RAW / WINDOW_HOURS, 2)
        raw_eta = round((100 - N_RAW) / (N_RAW / WINDOW_HOURS), 1)
        ok("the rate is NOT the raw count's rate",
           r["observed_per_hour"] != raw_rate,
           f"published {r['observed_per_hour']}, raw-basis would be {raw_rate}")
        ok("the ETA is NOT the raw count's ETA",
           r["eta_hours_to_100"] != raw_eta,
           f"published {r['eta_hours_to_100']}, raw-basis would be {raw_eta}")
        ok("the ETA is never optimistic relative to the raw basis",
           r["eta_hours_to_100"] >= raw_eta,
           f"published {r['eta_hours_to_100']}, raw basis {raw_eta}")

        # The gate itself is unchanged by this: it already keyed on B + B2.
        check("the gate still counts the attributable events",
              r["gate"]["observations"]["have"], N_ATTRIBUTABLE)

        # ---- 2. without a reconciliation the basis is named as raw --------
        print()
        print("2. NO RECONCILIATION - the raw basis is named, and the gate "
              "fails closed")
        # Without a reconciliation nothing is VERIFIED, so there is no
        # attributable rate, no ETA, and the gate's count is zero. The raw
        # count stays available as INFORMATION - it is published beside the
        # gate and labelled as raw - but it can never certify the campaign.
        r_raw = COLLECT.collect(epoch)
        check("the basis is named", r_raw.get("rate_basis"), "raw")
        check("there is no attributable rate without a reconciliation",
              r_raw["observed_per_hour"], None)
        check("...and no ETA", r_raw["eta_hours_to_100"], None)
        check("the gate's count is zero without a reconciliation",
              r_raw["gate"]["observations"]["have"], 0)
        check("...and the gate cannot be met",
              r_raw["gate"]["observations"]["met"], False)
        check("...and says provenance was unavailable",
              r_raw["gate"]["provenance_available"], False)
        check("...but the raw count is still published as information",
              r_raw["gate"]["raw_observations"], N_RAW)

        # An EMPTY reconciliation carries no bucket counts, so it cannot
        # support an attributable count. It must fall back to the raw basis
        # AND say so: a report that used the raw basis while claiming a
        # reconciliation was available would present raw rows as verified.
        r_empty = COLLECT.collect(epoch, provenance={})
        check("an empty reconciliation falls back to the raw basis",
              r_empty.get("rate_basis"), "raw")
        check("...and says provenance was unavailable",
              r_empty["gate"]["provenance_available"], False)
        check("...and the gate fails closed",
              r_empty["gate"]["observations"]["met"], False)
        ok("the basis label and the availability flag agree",
           (r_empty.get("rate_basis") == "attributable")
           == bool(r_empty["gate"]["provenance_available"]),
           f"basis={r_empty.get('rate_basis')!r}, "
           f"available={r_empty['gate']['provenance_available']!r}")

        # ---- 3. the time floor is stated ------------------------------------
        print()
        print("3. THE TIME FLOOR - the count is only half the gate")
        remaining = r["gate"]["elapsed"].get("hours_remaining")
        ok("the gate states how much of the 48h floor is left",
           remaining is not None and abs(remaining - (48 - WINDOW_HOURS)) < 0.01,
           f"got {remaining!r}, want ~{48 - WINDOW_HOURS}")

        # ---- 4. no surface labels a raw count as the gate count --------------
        print()
        print("4. LABELS - a raw count is never presented as the gate count")
        collect_text = COLLECT.render(r)
        ok("the collector states the attributable count against the gate",
           f"{N_ATTRIBUTABLE} / 100" in collect_text,
           collect_text[:400])
        ok("the collector does not present the raw count as the gate count",
           f"unique obs    : {N_RAW} / 100" not in collect_text,
           collect_text[:400])
        ok("the collector names the rate's basis",
           "attributable" in collect_text, collect_text[:400])
        # The COVERAGE section repeats the raw admitted count. It is a
        # dimension-A figure and must NOT claim to be the gate's count - the
        # report states the gate count at the top, and a second line calling a
        # different number "the gate" would reintroduce the defect this proof
        # exists to close.
        ok("the collector does not call the raw admitted count the gate count",
           "these gate" not in collect_text, collect_text[:400])
        ok("the collector marks the raw admitted count as NOT the gate count",
           "NOT the gate count" in collect_text, collect_text[:400])
        # The two numbers must not be interchangeable: the raw count is
        # published beside the gate count, and both appear.
        ok("the collector publishes the raw count distinctly from the gate count",
           f"raw obs rows  : {N_RAW}" in collect_text, collect_text[:400])

        # The liveness text render must not label a raw count "acceptance".
        liveness = LIVENESS.assess(
            acceptance_operator_observations=N_ATTRIBUTABLE,
            acceptance_operator_admitted=N_ATTRIBUTABLE,
            turns=[], elapsed_h=float(WINDOW_HOURS),
            attachments_observed=True,
            basis_counts={"text": N_ATTRIBUTABLE},
            raw_operator_observations=N_RAW,
        )
        liveness["capability_source"] = "fixture"
        liveness["epoch_selection"] = "explicit"
        liveness["raw_operator_observations"] = N_RAW
        liveness["provenance"] = prov
        liveness_text = LIVENESS.render(r, liveness, epoch)
        ok("the liveness report states the attributable gate count",
           f"{N_ATTRIBUTABLE} / 100" in liveness_text, liveness_text[:600])
        ok("the liveness report does not label a raw count as acceptance",
           "acceptance unique obs" not in liveness_text, liveness_text[:600])

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
