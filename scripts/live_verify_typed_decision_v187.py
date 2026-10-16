#!/usr/bin/env python3
"""Live candidate-only typed Output Guard probe. No DB/config/service writes.

Run from the operator's home with PYTHONPATH pointing at the isolated overlay:
  PYTHONPATH="$HOME/typed-decision-v187-candidate" \
    /opt/turnstone-venv/bin/python scripts/live_verify_typed_decision_v187.py

Makes two real, synthetic classification calls to the live Switchyard decision
endpoint, through OutputGuardJudge._evaluate_active, while forbidding model_turn.
Does not touch the installed Turnstone runtime or modify any model settings.
"""
from __future__ import annotations

import pathlib
import sys
import time
from types import SimpleNamespace

candidate = pathlib.Path.home() / "typed-decision-v187-candidate"
if not (candidate / "turnstone/core/typed_decision.py").is_file():
    raise SystemExit(f"Missing staged candidate: {candidate}")
if str(candidate) not in sys.path:
    sys.path.insert(0, str(candidate))

import turnstone.core.output_guard_judge as og
from turnstone.core.typed_decision import TypedDecisionSpec

if not pathlib.Path(og.__file__).resolve().is_relative_to(candidate.resolve()):
    raise SystemExit(f"Refusing live-package import: {og.__file__}")

origin = "https://switchyard-vm.tailc8ef7b.ts.net"
alias = "tier1-decision"
spec = TypedDecisionSpec.from_capabilities({
    "supports_typed_decision": True,
    "decision_contract": "switchyard-decision:v1",
    "decision_types": ["noul"],
    "decision_endpoint": "/v1/decisions",
})
assert spec is not None

judge = og.OutputGuardJudge.__new__(og.OutputGuardJudge)
judge._config = SimpleNamespace(output_guard_llm_timeout=60.0)
judge._typed_spec = spec
judge._typed_base_url = origin
judge._typed_alias = alias
judge._model = "do-not-call-a-session-model"

# The native typed adapter must never touch generative chat or Responses
# model_turn. Existing dedicated unit tests also prove this; retain it here
# while calling a REAL Switchyard decision backend.
old_turn = og.model_turn


def forbidden_model_turn(*args: object, **kwargs: object) -> None:
    raise RuntimeError("FAIL: generative model_turn invoked for typed alias")

og.model_turn = forbidden_model_turn
try:
    cases = (
        ("clean", "Verification succeeded. No warnings or failures."),
        ("diagnostic", "ERROR: simulated tool result: verification FAILED and credentials could not be validated."),
    )
    for label, sample in cases:
        started = time.monotonic()
        verdict = judge._evaluate_active(
            sample,
            func_name="read_file",
            call_id=f"typed-live-{label}",
            tool_description="Read synthetic tool result",
            tool_args="",
            heuristic_risk="none",
            heuristic_flags=(),
            heuristic_annotations=(),
            cancel_event=None,
            backend_auth_resolver=None,
            start=started,
            verdict_id=f"typed-live-{label}",
        )
        print(
            f"{label}: succeeded={verdict.succeeded}, "
            f"risk={verdict.risk_level}, "
            f"confidence_field={verdict.confidence}, "
            f"error={verdict.error!r}"
        )
        print(f"{label}: provenance={verdict.provenance}")
        assert verdict.succeeded, f"{label}: semantic decision failed: {verdict.error}"
        assert verdict.judge_model == alias, verdict.judge_model
        assert verdict.reasoning == "", "Typed decisions must not fabricate reasoning"
        assert "backend=" in verdict.provenance, "Current Switchyard backend provenance absent"
        assert "child_lane=" in verdict.provenance, "Current Switchyard child lane absent"
        assert "requested=tier1-decision" in verdict.provenance
finally:
    og.model_turn = old_turn

print("LIVE CANDIDATE OUTPUT GUARD: PASS; services/config/DB unchanged")
