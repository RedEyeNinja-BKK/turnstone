#!/usr/bin/env python3
"""PROOF: the ACCEPTANCE GATE reads the attributable count, in every reporter.

The defect this closes (2026-10-19): `collect.py` was fixed to key its gate on
the provenance-attributable count, but `liveness.py` embeds the collector's
result dict verbatim as its `acceptance` block -- and the collector's gate dict
is only rebuilt with provenance when the CALLER passes it in.

`liveness.py` calls `collector.collect(epoch)` with no provenance argument, so
the collector's own gate fell back to the RAW observation count, and liveness
published THAT as the acceptance gate the live watch reads. On the live epoch
that meant the watch's gate said 32 while only 27 of those events were
attributable to operator work.

The same collision existed a second time: `collect.collect()` returns the
backend-lane counter under the key `provenance`, and `main()` then OVERWRITES
that key with the provenance reconciliation. So the rendered "lanes" line and
the rendered "PROVENANCE RECONCILIATION" block read the same dict, and a
collector run whose lanes were non-empty but whose provenance was unavailable
raised `KeyError: 'A_raw_observed'` while rendering.

Two properties are asserted here, on synthetic fixtures only:

  1. THE GATE NEVER READS AN UNATTRIBUTABLE ROW. With provenance available, the
     gate's `have` is B+B2; without it, the gate must say so and must not
     present the raw count as verified.
  2. THE LANES AND THE RECONCILIATION ARE DIFFERENT DICTS. The lanes counter
     keeps its own key, the reconciliation keeps its own, and rendering works
     whether or not the reconciliation is available.

No telemetry, database or service is touched: every epoch is a scratch
directory under /var/tmp.

Run: /opt/turnstone-venv/bin/python s6/evidence/gate_provenance_proof.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
S6 = HERE.parent

PASS = 0
FAIL = 0


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


COLLECT = load("s6collect_gate", S6 / "collect.py")
LIVENESS = load("s6liveness_gate", S6 / "liveness.py")

T1 = datetime(2026, 10, 18, 13, 21, 51, tzinfo=timezone.utc)
SERVICE = "turnstone-node-2"


def admitted(event_id: str, offset_s: float) -> dict:
    return {"event": "operator_instruction", "event_id": event_id,
            "outcome": "sensor_call", "ts": (T1.timestamp() + offset_s)}


def observation(event_id: str, offset_s: float, *, lane: str | None = None) -> dict:
    row = {
        "event_id": event_id,
        "timestamp": T1.timestamp() + offset_s,
        "content_basis": "text",
        "signals": [{"name": n, "available": True} for n in COLLECT.GRADED_SIGNALS],
    }
    if lane:
        row["extra"] = {"child_lane": lane}
    return row


def build(root: pathlib.Path, *, cadence, observations, name: str = "s6-t2") -> pathlib.Path:
    epoch = root / name
    (epoch / SERVICE).mkdir(parents=True)
    (epoch / "T2").write_text("2026-10-18T13:21:51Z\n")
    for filename, rows in (("shadow-cadence.jsonl", cadence),
                           ("shadow-observations.jsonl", observations)):
        (epoch / SERVICE / filename).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
    return epoch


def provenance_for(n_attributable: int, n_raw: int) -> dict:
    """A reconciliation with the given shape, as provenance.py produces it."""
    return {
        "A_raw_observed": n_raw,
        "B_verified_operator": n_attributable,
        "B2_verified_operator_audit": 0,
        "C_unverified": n_raw - n_attributable,
        "C2_system_generated": 0,
        "E_legitimate_duplicates_suppressed": 0,
        "F_distinct_turns_suppressed": 0,
        "match_window_s": 5.0,
    }


def main() -> int:
    print("GATE / PROVENANCE PROOF - the gate reads attributable events only")
    print("=" * 66)
    print()

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        root = pathlib.Path(tmp)
        epoch = build(root,
                      cadence=[admitted("e1", 10), admitted("e2", 20)],
                      observations=[observation("e1", 11), observation("e2", 21)])

        # ---- 1. the collector's gate keys on provenance when given it --------
        print("1. COLLECTOR GATE - keys on the attributable count")
        r = COLLECT.collect(epoch, provenance=provenance_for(1, 2))
        check("the gate reads the attributable count",
              r["gate"]["observations"]["have"], 1)
        check("...not the raw count", r["gate"]["raw_observations"], 2)
        check("...and says provenance was available",
              r["gate"]["provenance_available"], True)
        check("...and reports the unverified remainder",
              r["gate"]["unverified"], 1)
        check("an unattributable row can never satisfy the gate",
              COLLECT.collect(epoch, provenance=provenance_for(0, 2))
              ["gate"]["observations"]["met"], False)

        # Without provenance the gate FAILS CLOSED: nothing is verified, so
        # the gate's count is zero and the gate can never be met, whatever
        # the raw row count is. The raw count stays available as information,
        # and the flag says it is not verified.
        r_raw = COLLECT.collect(epoch)
        check("without provenance the gate's count is zero",
              r_raw["gate"]["observations"]["have"], 0)
        check("...and the gate cannot be met",
              r_raw["gate"]["observations"]["met"], False)
        check("...and it is NOT presented as verified",
              r_raw["gate"]["provenance_available"], False)
        check("...and the unverified count is unknown, not zero",
              r_raw["gate"]["unverified"], None)
        check("...but the raw count is still reported as information",
              r_raw["gate"]["raw_observations"], 2)

        # ---- 2. the lanes counter and the reconciliation are DIFFERENT -----
        print()
        print("2. LANES vs RECONCILIATION - two dicts, one key each")
        epoch_lane = build(root,
                           cadence=[admitted("l1", 10)],
                           observations=[observation("l1", 11, lane="tier1")],
                           name="s6-t2-lanes")
        r_lane = COLLECT.collect(epoch_lane)
        check("the lanes counter is intact", r_lane["lanes"], {"child_lane=tier1": 1})
        ok("the reconciliation is not smuggled into the lanes key",
           "A_raw_observed" not in r_lane["lanes"], str(r_lane["lanes"]))

        # Rendering must work whether or not the reconciliation is available.
        try:
            COLLECT.render(r_lane)
            rendered = True
            err = ""
        except Exception as error:  # noqa: BLE001
            rendered = False
            err = f"{type(error).__name__}: {error}"
        ok("a collector with lanes renders without a reconciliation", rendered, err)

        # And with a reconciliation present, both blocks are rendered.
        r_both = COLLECT.collect(epoch_lane, provenance=provenance_for(1, 1))
        text = COLLECT.render(r_both)
        ok("the lanes line is rendered", "lanes" in text, text[:200])
        ok("the reconciliation block is rendered",
           "PROVENANCE RECONCILIATION" in text, text[:200])
        ok("the lanes value is not the reconciliation",
           "A_raw_observed" not in json.dumps(r_both["lanes"]), str(r_both["lanes"]))

        # ---- 3. LIVENESS publishes the collector's gate, not its own -------
        print()
        print("3. LIVENESS - the published acceptance block is the real gate")
        # Drive liveness.main() the way the watch does, with a stubbed store
        # read and a stubbed provenance reconciliation.
        LIVENESS.fetch_turns = lambda dsn, since, until=None: []
        LIVENESS.count_operator_observations = (
            lambda ep, prov=None: (2, 2, {"text": 2}, 2,
                                   COLLECT.collect(ep, provenance=prov)))
        prov_module = load("s6prov_gate", S6 / "provenance.py")
        prov_module.collect_events = lambda ep: ([], T1, None)
        prov_module.fetch_store = lambda dsn: {"turns": []}
        prov_module.fetch_audit = lambda dsn: {"created": {}, "deleted": set(),
                                               "usage": {}, "users": {}}
        prov_module.reconcile = lambda **kw: provenance_for(1, 2)

        import importlib.machinery as _machinery
        import importlib.util as _iu

        class _StubLoader:
            """A loader that hands back the already-stubbed module."""

            def __init__(self, module):
                self.module = module

            def create_module(self, spec):
                return self.module

            def exec_module(self, module):
                return None

        real_spec = _iu.spec_from_file_location
        try:
            def fake_spec(name, location, *a, **k):
                if str(location).endswith("provenance.py"):
                    return _machinery.ModuleSpec(name, _StubLoader(prov_module))
                return real_spec(name, location, *a, **k)

            _iu.spec_from_file_location = fake_spec
            import io
            from contextlib import redirect_stdout
            old_argv = sys.argv
            sys.argv = ["liveness.py", "--epoch", str(epoch), "--json"]
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer):
                    rc = LIVENESS.main()
            finally:
                sys.argv = old_argv
        finally:
            _iu.spec_from_file_location = real_spec

        payload = json.loads(buffer.getvalue())
        check("liveness exits 0", rc, 0)
        check("the published acceptance block reads the attributable count",
              payload["acceptance"]["observations"]["have"], 1)
        check("...and the raw count is reported beside it",
              payload["acceptance"]["raw_observations"], 2)
        check("...and provenance availability is stated",
              payload["acceptance"]["provenance_available"], True)
        check("...and the unverified remainder is reported",
              payload["acceptance"]["unverified"], 1)
        ok("the published gate is never the raw count while provenance is available",
           payload["acceptance"]["observations"]["have"] != payload["acceptance"]["raw_observations"],
           f"have={payload['acceptance']['observations']['have']} "
           f"raw={payload['acceptance']['raw_observations']}")

    print()
    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
