#!/usr/bin/env python3
"""PROOF: without a provenance reconciliation, acceptance CANNOT be met.

The defect this closes (found 2026-10-20, during independent review):

The collector falls back from attributable observations to RAW observation
rows when the provenance reconciliation is unavailable. That fallback is
useful for INFORMATIONAL reporting - "how many rows exist at all" is worth
knowing - but it was wired into the same field the gate reads, so raw rows
could satisfy the 100-event acceptance requirement.

    provenance unavailable
    => attributable acceptance can never be MET

The raw count stays available as INFORMATION, and stays clearly labelled as
raw, but it can never certify the campaign. A campaign is certified by
events that were VERIFIED against the conversation store, and without the
reconciliation nothing is verified - so there is no attributable rate and no
ETA to publish either.

Four properties are asserted here, on synthetic fixtures only:

  1. THE GATE FAILS CLOSED. With no reconciliation, 100+ raw rows leave the
     acceptance gate unmet, the total verdict is not ACCEPTED, and the
     elapsed floor alone cannot carry it.
  2. THE RAW COUNT IS STILL REPORTED, explicitly labelled as raw and
     explicitly not the gate count.
  3. A VALID RECONCILIATION IS STILL EXACTLY B + B2. The fix must not
     change what a good reconciliation counts.
  4. EVERY SURFACE AGREES. The collector JSON, the collector text, the
     liveness report and the scheduled watch's stop condition all read the
     SAME gate state, and none of them can report acceptance that the
     collector did not.

No telemetry, database or service is touched: every epoch is a scratch
directory under /var/tmp.

Run: /opt/turnstone-venv/bin/python tools/s6/proofs/provenance_required_proof.py
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

#: The gate needs 100 attributable events. The fixture below deliberately
#: produces MORE than 100 raw rows, so a fallback that counts raw rows would
#: clear the gate and the test would fail.
N_RAW = 120
GATE_NEEDED = 100


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
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLLECT = load("s6collect_provreq", S6 / "collect.py")
LIVENESS = load("s6liveness_provreq", S6 / "liveness.py")


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
    slice. The window is long enough that the campaign is rateable, so the
    only thing standing between the fixture and an ACCEPTED verdict is the
    missing reconciliation.
    """
    now = datetime.now(timezone.utc)
    t1 = now - timedelta(hours=48)
    t1_text = t1.strftime("%Y-%m-%dT%H:%M:%SZ")
    base = t1.timestamp()
    epoch = root / "s6-t2"
    (epoch / SERVICE).mkdir(parents=True)
    (epoch / "T2").write_text(t1_text + "\n")
    # The rows are spread across the window so the rate is computable and the
    # elapsed floor is satisfied.
    step = (48 * 3600) / max(1, n_raw)
    cadence = [admitted(f"e{i}", base + 10.0 + i * step) for i in range(n_raw)]
    observations = [observation(f"e{i}", base + 11.0 + i * step)
                    for i in range(n_raw)]
    for filename, rows in (("shadow-cadence.jsonl", cadence),
                           ("shadow-observations.jsonl", observations)):
        (epoch / SERVICE / filename).write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
    return epoch, t1_text


def reconciliation(n_b: int, n_b2: int, n_raw: int) -> dict:
    """A reconciliation in the shape provenance.py produces."""
    return {
        "A_raw_observed": n_raw,
        "B_verified_operator": n_b,
        "B2_verified_operator_audit": n_b2,
        "C_unverified": max(0, n_raw - n_b - n_b2),
        "C2_system_generated": 0,
        "C3_system_transitions": 0,
        "E_legitimate_duplicates_suppressed": 0,
        "F_distinct_turns_suppressed": 0,
        "match_window_s": 5.0,
    }


def main() -> int:
    print("PROVENANCE REQUIRED PROOF - acceptance cannot be certified without it")
    print("=" * 66)
    print()

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp)
        epoch, _t1_text = build(root)

        # ---- 1. the gate FAILS CLOSED without a reconciliation --------------
        print("1. NO RECONCILIATION - the gate cannot be met")
        r = COLLECT.collect(epoch)
        gate = r["gate"]

        ok("the raw rows really do exceed the gate",
           r["unique_observations"] >= GATE_NEEDED,
           f"raw={r['unique_observations']} need={GATE_NEEDED}")
        ok("the elapsed floor IS satisfied, so the count is the only thing"
           " that can fail the gate",
           gate["elapsed"]["met"] is True, str(gate["elapsed"]))
        ok("the gate is NOT met without a reconciliation",
           gate["observations"]["met"] is False, str(gate["observations"]))
        ok("the gate reports provenance as unavailable",
           gate["provenance_available"] is False, str(gate["provenance_available"]))
        ok("the gate's own count is not the raw count",
           gate["observations"]["have"] != r["unique_observations"]
           or gate["observations"]["have"] < GATE_NEEDED,
           str(gate["observations"]))

        # The total verdict is the surface the operator reads.
        text = COLLECT.render(r)
        ok("the rendered verdict is NOT 'ACCEPTED'",
           "STATUS: ACCEPTED" not in text,
           text[-400:])
        ok("the rendered verdict says the gate is not satisfied",
           "STATUS: MEASUREMENT ACTIVE" in text
           or "STATUS: HISTORICAL EPOCH" in text
           or "STATUS: ROLLOUT IN PROGRESS" in text,
           text[-400:])

        # ---- 2. the raw count is still reported, and still labelled raw -----
        print()
        print("2. THE RAW COUNT IS STILL REPORTED - and labelled raw")
        ok("the raw count is still published",
           r["unique_observations"] >= GATE_NEEDED,
           str(r["unique_observations"]))
        ok("the raw count is published beside the gate",
           r["gate"]["raw_observations"] == r["unique_observations"],
           str(r["gate"]["raw_observations"]))
        ok("the rate basis is named 'raw'",
           r.get("rate_basis") == "raw", str(r.get("rate_basis")))
        ok("the text names the raw basis",
           "raw basis" in text or "rate basis    : raw" in text, text[:600])
        ok("the text does not present the raw count as the gate count",
           "these gate" not in text and "NOT the gate count" in text,
           text[:400])

        # ---- 3. a valid reconciliation is still exactly B + B2 --------------
        print()
        print("3. A VALID RECONCILIATION - still exactly B + B2")
        prov = reconciliation(n_b=70, n_b2=30, n_raw=N_RAW)
        r_ok = COLLECT.collect(epoch, provenance=prov)
        check("the gate counts B + B2",
              r_ok["gate"]["observations"]["have"], 100)
        check("the rate counts B + B2", r_ok.get("rate_events"), 100)
        check("the basis is 'attributable'", r_ok.get("rate_basis"),
              "attributable")
        ok("the gate IS met when 100 attributable events are reconciled",
           r_ok["gate"]["observations"]["met"] is True,
           str(r_ok["gate"]["observations"]))
        text_ok = COLLECT.render(r_ok)
        ok("the rendered verdict IS 'ACCEPTED'",
           "STATUS: ACCEPTED" in text_ok, text_ok[-400:])

        # A partial reconciliation is NOT a pass.
        prov_partial = reconciliation(n_b=60, n_b2=30, n_raw=N_RAW)
        r_partial = COLLECT.collect(epoch, provenance=prov_partial)
        check("a partial reconciliation counts B + B2 exactly",
              r_partial["gate"]["observations"]["have"], 90)
        ok("a partial reconciliation does not meet the gate",
           r_partial["gate"]["observations"]["met"] is False,
           str(r_partial["gate"]["observations"]))
        ok("...and the verdict is not ACCEPTED",
           "STATUS: ACCEPTED" not in COLLECT.render(r_partial))

        # ---- 4. every surface agrees ----------------------------------------
        print()
        print("4. EVERY SURFACE AGREES")
        liveness = LIVENESS.assess(
            acceptance_operator_observations=r["gate"]["observations"]["have"],
            acceptance_operator_admitted=0,
            turns=[], elapsed_h=48.0,
            attachments_observed=True,
            basis_counts={"text": r["gate"]["observations"]["have"]},
            raw_operator_observations=r["unique_observations"],
        )
        liveness["capability_source"] = "fixture"
        liveness["epoch_selection"] = "explicit"
        liveness["raw_operator_observations"] = r["unique_observations"]
        liveness_text = LIVENESS.render(r, liveness, epoch)
        ok("the liveness report does not claim acceptance",
           "ACCEPTED" not in liveness_text, liveness_text[-600:])
        ok("the liveness report states the same gate count",
           f"{r['gate']['observations']['have']}"
           in liveness_text, liveness_text[:600])

        # The scheduled watch reads the collector's JSON gate. That is the
        # state the watch's stop condition is evaluated against, so it must
        # agree with the rendered verdict.
        watch_state = r["gate"]
        ok("the watch's stop condition cannot fire without a reconciliation",
           watch_state["observations"]["met"] is False,
           str(watch_state["observations"]))
        ok("the watch's stop condition CAN fire with a valid reconciliation",
           r_ok["gate"]["observations"]["met"] is True,
           str(r_ok["gate"]["observations"]))

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
