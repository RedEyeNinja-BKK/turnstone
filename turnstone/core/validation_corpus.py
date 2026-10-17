"""Validation corpus for the bounded-state serializer.

Representative Turnstone workstream states, spanning the shapes the operator
named. Each is a plausible real state rather than filler, so the measured
lengths and the truncation-stability comparison mean something.

The corpus is data, not fixtures-for-passing: it exists to be measured.
"""

from __future__ import annotations

from typing import Any

#: (name, rationale, state)
CORPUS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "short-simple",
        "a one-line question with no execution state",
        {"objective": "Answer whether the config key is spelled correctly."},
    ),
    (
        "ordinary-engineering",
        "a normal single-agent engineering task",
        {
            "objective": "Fix the fallback classification so a remote 500 degrades.",
            "current_state": "Classifier updated; focused tests added.",
            "blockers": "none",
            "progress": "waiting on independent review",
        },
    ),
    (
        "tool-heavy",
        "many tool invocations and command output in flight",
        {
            "objective": "Qualify the token budget against the live Laya service.",
            "current_state": "running calibration probes",
            "tools": "curl, python3, ssh",
            "progress": "7 of 9 probes complete",
            "evidence": "usage.input_tokens is authoritative",
            "notes": "avoid hand-typed constants",
        },
    ),
    (
        "multi-agent",
        "delegated sub-agents with mixed outcomes",
        {
            "objective": "Review the failover patch and qualify a candidate build.",
            "delegation": "review lane running; build lane queued",
            "current_state": "one review finding open",
            "blockers": "delta review pending",
            "progress": "rebuild after review",
            "evidence": "mutation battery 17/17",
        },
    ),
    (
        "failure-retry",
        "an active failure with retries in progress",
        {
            "objective": "Restore the credential to the sanctioned env file.",
            "current_state": "operator pasted the token",
            "failures": "read was still pending; first write did not land",
            "blockers": "trust boundary crossing is human-gated",
            "progress": "second attempt succeeded",
        },
    ),
    (
        "large-evidence",
        "a lot of genuinely material evidence",
        {
            "objective": "Reconcile the deployment record and prove each live claim.",
            "current_state": "all proofs green",
            "evidence": "healthy primary served six signals with no fallback",
            "evidence_2": "ineligible 401 produced zero fallback counters",
            "evidence_3": "double failure stayed terminal at two attempts",
            "evidence_4": "restoration returned to the primary immediately",
            "progress": "recording the run",
            "blockers": "none",
            "notes": "preserve the corrected mistake in the record",
        },
    ),
    (
        "near-worst-case",
        "history-heavy state where repetition can bury the objective",
        {
            "objective": "CRITICAL: a production routing decision is inverted.",
            "current_state": "cutover in progress",
            "history": " ".join(f"event {i}: routine status poll, no change" for i in range(40)),
            "filler": " ".join(
                "routine chatter with no material change " for _ in range(60)
            ),
            "notes": " ".join("checked the same thing again " for _ in range(40)),
            "progress": "awaiting confirmation",
        },
    ),
]
