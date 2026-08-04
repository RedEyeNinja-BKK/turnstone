# Methodology — Pre-Isolation Multi-Agent Evaluation

How to measure whether multi-agent delegation adds value **before** you commit to isolation or
routing changes, with evidence that cannot be faked.

## Why

"Delegation is better" is a claim. Without a controlled comparison, you cannot tell whether adding
agents to a task improves outcomes, adds overhead, or both. This harness runs a fixed matrix of
task × route-arm cells and scores the **blinded outputs** plus **deterministic receipts** — two
independent layers of evidence.

## The route arms

| Arm | Composition | Tool surface | Meaning |
|---|---|---|---|
| A | Coordinator alone | native tools only | solo baseline |
| B | Coordinator + one agent | + that agent's MCP gateway | delegation |
| C | Coordinator + both agents | + both gateways | full stack |

Each task is run once per arm (Latin-square ordering across blocks to counterbalance fatigue/timing).

## The two scoring layers

**Layer 1 — blinded content.** Each cell's output is:
1. Reduced to the **final VERDICT-bearing message** (internal reasoning + tool traces are kept
   separately for audit, not scored).
2. **Anonymized fail-closed** (arm, model, ids, cost, correlation stripped; any residual → exit 2).
3. Scored by a **pinned evaluator model at temperature 0** on correctness/completeness/usefulness
   (0–5 each). The evaluator never sees the arm or any other cell's output.

**Layer 2 — deterministic operations.** Route legality is derived from **concrete receipts**:
- Arm A: `turnstone_receipt` (real workstream id)
- Arm B: `hermes_receipt` (real gateway run id, polled to terminal) + coordinator verify receipt
- Arm C (review tasks): Hermes + independent OpenClaw review + coordinator reconcile
- Arm C (delivery task): Hermes + OpenClaw delivery to an **isolated loopback webhook** + hash and
  correlation read-back + coordinator finalize

Missing receipt → **INDETERMINATE (exit 3)**, never inferred PASS. Prohibited side effect (e.g. any
emit to a production destination) → **FAIL (exit 1)**.

## Evidence lineage (per cell, hash-linked, sealed)

```
run-evidence.json  (raw output + receipts + verdict)      ← sha256
anonymized-output.json  (references raw hash)
content-evaluation.json  (references anonymized hash)
operational-evaluation.json  (references raw hash)
run-summary.json  (references raw + anonymized + op exit)
```

Files sealed 0600; the manifest seals the whole bundle so any tamper breaks verification.

## Gates (non-negotiable)

1. **Real execution before scored runs.** A non-scored rehearsal (one cell per arm) must pass with
   complete receipts, plus a non-scored arm-C review-route preflight.
2. **Embed the real task bodies.** Task-ID-only prompts measure coordinator behavior under ambiguous
   input, not task performance. (We made this mistake; the invalid run was preserved as evidence and
   the harness corrected.)
3. **Preflight the environment** before/after each block: reranker semantic check, pinned-schedule
   drift check, webhook receiver + destination blocklist.
4. **No comparative results until all cells are collected** and sealed.
5. **Isolation is enforced, not asked.** Revoke other agents' MCP tools at the session level per arm
   (capability-level), don't rely on prompt instructions.

## Interpretation (be honest)

- One scored execution per cell is **directional**, not statistically significant.
- In our 18-cell pilot (six tasks): coordinator+agent materially beat solo on live multi-step tasks;
   the full three-agent arm did **not** add general content quality over coordinator+agent (it adds
   independent review / delivery capability). Both findings are deployment-specific.
- The largest quality lever was **coordinator finalize discipline** (clean final deliverable), not
   which agents were involved.

## Adapting

The six tasks are in `TASK_BODIES` (in `trial_harness.py`): incident diagnosis, structured report,
read-only dry-run, source synthesis, pinned multi-step orchestration, delivery. Replace with your
own domain tasks; keep them self-contained and factual-anchored so scoring is possible.
