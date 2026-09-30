"""The sensor's question set, self-contained.

Copied VERBATIM from the qualified `sensor_layer` table. The wording is not
free text: these questions were qualified against Span and the Laya fallback,
and paraphrasing them would silently invalidate that qualification. An earlier
draft here reworded them and the resulting body differed from the qualified
one; a parity test now pins the two together so that cannot recur.

Kept separate from `sensor_layer` so the shadow path does not depend on
`typed_decision`, which the live Turnstone package does not ship - importing it
would make the shadow path unimportable in production.

The Respan/Span decision surface accepts ONLY `noul` questions (a `choice`
question is rejected with HTTP 400), so every question here is graded and the
grade is read directly. No risk-style remapping: a sensor score means "how much
of X", and reusing a security control's categorical vocabulary would corrupt
that meaning.
"""

from __future__ import annotations

from typing import Any

SENSOR_CONTRACT = "switchyard-decision:v1"

SENSOR_QUESTIONS: dict[str, tuple[str, str, str]] = {
    "task_complexity": (
        "How complex is this task?",
        "the task needs many interdependent steps, deep domain knowledge, or "
        "hard decomposition to complete correctly",
        "the task is simple and direct, with few steps and little decomposition",
    ),
    "reasoning_demand": (
        "How much does this task demand multi-step reasoning?",
        "solving this requires holding many interacting constraints and "
        "deriving conclusions that are not stated anywhere in the input",
        "solving this is largely a lookup or a mechanical application of a "
        "known procedure",
    ),
    "tool_dependency": (
        "How much does completing this task depend on external tools?",
        "the task cannot be completed from its own description and needs "
        "tools to read, execute, search, or verify",
        "the task can be completed from the description alone with no tools",
    ),
    "agentic_complexity": (
        "How much coordination does this task need?",
        "this needs several ordered steps with dependencies, or delegation to "
        "child agents whose results must be integrated",
        "this is a single self-contained step needing no coordination",
    ),
    "local_suitability": (
        "How well suited is a small local model for this task?",
        "a small local model can plausibly do this task acceptably, with no "
        "cloud-only capability required",
        "this task needs large-scale capability, long reliable reasoning, or "
        "broad knowledge that a small local model should not be trusted with",
    ),
    "route_sufficiency": (
        "Is the current approach still making adequate progress?",
        "the current approach is failing, repeating, or stuck, and the task "
        "warrants a change of approach or capability",
        "the current approach is progressing normally toward the goal",
    ),
}

def build_request(
    *,
    state: str,
    decision_id: str,
    signal_names: tuple[str, ...],
) -> dict:
    """Build the typed-decision request body.

    Non-empty state, `noul` questions only, explicit criteria. Raises on a
    caller mistake rather than sending a request the backend will reject.
    """
    if not state.strip():
        raise ValueError("sensor state must be non-empty")
    unknown = [n for n in signal_names if n not in SENSOR_QUESTIONS]
    if unknown:
        raise ValueError(f"unknown sensor signals: {sorted(unknown)}")
    if not signal_names:
        raise ValueError("at least one sensor signal is required")

    questions: dict[str, dict[str, Any]] = {}
    for name in signal_names:
        instructions, criteria_true, criteria_false = SENSOR_QUESTIONS[name]
        questions[name] = {
            "type": "noul",
            "instructions": instructions,
            "criteria": {"true": criteria_true, "false": criteria_false},
        }
    return {"model": decision_id, "state": state, "questions": questions}


