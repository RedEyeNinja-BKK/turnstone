# multi-agent-trial — Pre-Isolation Multi-Agent Evaluation Harness

A self-contained harness for measuring whether **multi-agent delegation adds value** before
committing to agent isolation or routing changes. Developed and used on a three-agent deployment
(Turnstone + Hermes + OpenClaw) to run a blinded, evidence-backed 18-cell trial.

**Status:** reference implementation — config-driven, no deployment-specific identifiers.

---

## What it does

Runs a fixed trial matrix of task × route-arm cells and produces sealed, hash-linked evidence per
cell, so that:

1. **Blinded content scoring** — each output is anonymized (fail-closed) and scored by a pinned
   evaluator model without knowing which arm produced it.
2. **Deterministic route validation** — legality is derived from concrete receipts (run/task IDs
   returned by the agents' gateways), never from a trusted boolean. Missing receipt → INDETERMINATE,
   never inferred PASS.
3. **Capability-level arm isolation** — the coordinator session has other agents' MCP tools revoked
   per arm (A: none; B: no third agent; C: full), enforced at the session level, not by prompt.
4. **Clean final-output extraction** — the deliverable is the final VERDICT-bearing message; internal
   reasoning and tool traces are kept separately for audit.
5. **Evidence lineage** — five hash-linked files per cell (raw → anonymized → content → operational →
   summary), sealed 0600.

## Files

| File | Purpose |
|---|---|
| `trial_harness.py` | Orchestrates blocks/cells, dispatches through gateway adapters, applies restrict, seals evidence |
| `anonymize.py` | Fail-closed anonymizer (strips arm/model/cost/ids; exits 2 on residual) |
| `evaluate.py` | Blinded scorer — pinned model, schema-validated, INDETERMINATE on infra failure |
| `validate_evidence.py` | Receipt-derived route legality (exit 0 PASS / 1 FAIL / 3 INDETERMINATE) |
| `evidence.py` | Hash-linked 5-file lineage (raw/anonymized/content/operational/summary) |
| `webhook_receiver.py` | Loopback-only delivery receiver for the delivery task (per-cell correlation + hash read-back) |
| `test_anonymize.py` | Adversarial anonymizer regression suite (10 cases) |
| `samples/` | Synthetic (non-deployment) example evidence records |

## Config (env vars — no hardcoded deployment identifiers)

| Var | Meaning | Default |
|---|---|---|
| `TURNSTONE_TOKEN_FILE` | Bearer token file for the platform API | `/run/secrets/turnstone-token` |
| `HERMES_MCP_ADAPTER` / `OPENCLAW_MCP_ADAPTER` | stdio MCP adapter paths for the two agents | `hermes_gateway_mcp.py` / `openclaw_gateway_mcp.py` |
| `RERANK_HEALTH_URL` / `RERANK_MODELS_URL` / `RERANK_URL` | Semantic reranker preflight endpoints | `https://rerank.example/...` |
| `T5_TASK_ID` / `T5_DEF_HASH` | Pinned-schedule drift check (task 5) | `pinned-task-id` / `pinned-def-hash` |
| `PROD_CHANNELS` | Blocklist of destinations that must never be emitted to | `[]` (set in your deployment) |
| `EVALUATOR_MODEL` / `EVALUATOR_URL` | Pinned evaluator (evaluate.py rejects overrides) | sealed in evaluate.py |

## Usage

```bash
python trial_harness.py --bundle-dir . --rehearsal        # 3 non-scored pipeline-proof cells
python trial_harness.py --bundle-dir . --c-review-preflight # non-scored arm-C review-route proof
python trial_harness.py --bundle-dir . --block 1|2|3      # scored cells (preflights auto-run)
python trial_harness.py --bundle-dir . --run-dir my-run    # isolated evidence dir
python evaluate.py <cell>/<cell>-anonymized-output.json    # blind-score one cell
```

See `docs/METHODOLOGY.md` for the trial design, route contracts, scoring layers, and how to adapt
the six task definitions (`TASK_BODIES` in `trial_harness.py`) to your own domain.

## Honest limits (learned the hard way)

- **Embed the real task bodies** in the prompt. A first run dispatched task-ID-only prompts; the
  scores measured coordinator behavior under ambiguous input, not task performance. The run was
  preserved as evidence and the harness corrected — keep the bodies in `TASK_BODIES`.
- **One scored execution per cell is directional**, not statistical. Report it as such.
- **Coordinator finalize discipline is the binding quality gate.** The harness now treats a
  VERDICT-bearing final assistant message as terminal and extracts it as the deliverable.
- **Sandbox flags can break PTYs.** In our deployment, `PrivateDevices=true` on the dashboard broke
  the Chat TUI's PTY spawn; keep device sandboxing off units that allocate PTYs.

## License

See the containing repository's license. No deployment-specific data is included; samples are
synthetic.
