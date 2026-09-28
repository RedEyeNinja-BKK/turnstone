#!/usr/bin/env python3
"""Mutation battery for the Output Guard typed-decision bridge.

A compile error is NOT a kill, and neither is a stale .pyc. Bytecode is purged
before and after every mutant, so a kill can never be faked by a cached import
and a mutant can never leak into the next case. Each mutant must be applied to
the real source, still import, and be killed by a NAMED test failing for the
intended reason.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

# Resolve the tree and interpreter from this file's own location so the battery
# runs in any checkout against any accepted runtime.  Pinning an absolute
# checkout path and one slot's interpreter made the battery break the moment
# either moved: it silently targeted a tree and a v1.8.4 runtime that were not
# the ones under review.  A review instrument must be portable.
ROOT = pathlib.Path(__file__).resolve().parent
TD = ROOT / "turnstone/core/typed_decision.py"
OG = ROOT / "turnstone/core/output_guard_judge.py"
PY = sys.executable

MUTANTS = [
    (
        "M1", TD,
        '        if not isinstance(caps, dict):\n            return None\n'
        '        if not caps.get("supports_typed_decision"):\n            return None',
        '        if not isinstance(caps, dict):\n            return None\n'
        '        if not caps.get("supports_typed_decision_disabled"):\n            return None',
        "test_module_spec_is_built",
        "typed capability ignored -> ordinary alias treated as typed",
    ),
    (
        "M2", TD,
        '            graded = _clamp_confidence(answer.get("noul", answer.get("value")))\n'
        '            if graded is None:\n'
        '                raise TypedDecisionError("noul answer carried no usable score")',
        '            graded = 0.0',
        "test_graded_noul_without_a_score_is_refused",
        "a score-less noul silently becomes 0.0 (clean) instead of being refused",
    ),
    (
        "M3", TD,
        r"    return max\(0\.0, min\(1\.0, number\)\)",
        "    return number",
        "test_confidence_clamped_to_unit_interval",
        "confidence accepted outside [0,1]",
    ),
    (
        "M4", TD,
        "    value = choice.strip().lower()\n    if value not in VALID_RISK_LEVELS:",
        "    value = choice.strip().lower()\n    if False:",
        "test_invalid_choice_rejected",
        "invalid choice string accepted",
    ),
    (
        "M5", TD,
        "    if contract != spec.contract:",
        "    if False:",
        "test_contract_mismatch_refused",
        "contract mismatch accepted",
    ),
    (
        "M6", TD,
        '        raise TypedDecisionError(f"decision transport failure: {exc}") from exc',
        "        return TypedDecisionResult("
        "risk_level='none', confidence=None, contract=spec.contract)",
        "test_transport_and_http_failures_become_typed_errors",
        "transport failure degrades to a clean none instead of an error",
    ),
    (
        "M7", TD,
        "    if not isinstance(answers, dict) or not answers:",
        "    if False:",
        "test_missing_answers_refused",
        "missing answers not refused",
    ),
    (
        "M8", OG,
        '        if getattr(self, "_typed_spec", None) is not None:',
        '        if getattr(self, "_typed_spec", None) is None and False:',
        "test_guard_typed_branch_runs_without_model_turn",
        "typed branch disabled -> typed alias would flow to model_turn/session model",
    ),
    (
        "M9", TD,
        "            risk = candidate if risk is None else max(\n"
        "                risk, candidate, key=VALID_RISK_LEVELS.index\n"
        "            )",
        "            risk = candidate if risk is None else min(\n"
        "                risk, candidate, key=VALID_RISK_LEVELS.index\n"
        "            )",
        "test_strongest_choice_wins_across_questions",
        "multi-question decision understates risk by taking the weakest answer",
    ),
    (
        "M10", TD,
        '    if graded >= threshold:',
        '    if graded >= 1.0:',
        "test_graded_noul_maps_onto_the_risk_vocabulary",
        "graded noul floor removed -> a risky decision reads as clean",
    ),
]


def purge_bytecode() -> None:
    for cache in ROOT.rglob("__pycache__"):
        for f in cache.glob("*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass


def run_tests() -> tuple[int, str]:
    purge_bytecode()
    p = subprocess.run(
        [PY, str(ROOT / "tests/test_output_guard_typed_decision.py")],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    print("BASELINE")
    rc, out = run_tests()
    if rc != 0:
        print("  baseline is RED; aborting")
        print(out[-1200:])
        return 2
    print("  baseline green\n")

    killed, survived, untested, errored = [], [], [], []
    for mid, path, pattern, repl, test, why in MUTANTS:
        original = path.read_text()
        new, n = re.subn(pattern, repl, original, count=1)
        if n == 0:
            new, n = re.subn(re.escape(pattern), repl.replace("\\", "\\\\"), original, count=1)
        if n == 0:
            untested.append(mid)
            print(f"  {mid}: UNTESTED (anchor matched 0) — {why}")
            continue
        path.write_text(new)
        try:
            rc, out = run_tests()
        finally:
            path.write_text(original)
            purge_bytecode()
        if "Traceback" in out and "passed," not in out:
            errored.append(mid)
            last = out.strip().splitlines()[-1] if out.strip() else ""
            print(f"  {mid}: ERROR (harness could not run) — {why}\n     {last[:120]}")
            continue
        if re.search(rf"FAIL {re.escape(test)}:", out):
            killed.append(mid)
            print(f"  {mid}: KILLED by {test} — {why}")
        elif rc == 0:
            survived.append(mid)
            print(f"  {mid}: SURVIVED — {why}")
        else:
            untested.append(mid)
            fails = re.findall(r"FAIL (\w+):", out)
            print(f"  {mid}: KILLED by the WRONG test {fails[:2]} (expected {test}) — {why}")

    print(f"\n  KILLED   {len(killed)}/{len(MUTANTS)}: {killed}")
    print(f"  SURVIVED {len(survived)}: {survived}")
    print(f"  UNTESTED {len(untested)}: {untested}")
    print(f"  ERROR    {len(errored)}: {errored}")
    ok = not survived and not untested and not errored
    print("  MUTATION BATTERY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
