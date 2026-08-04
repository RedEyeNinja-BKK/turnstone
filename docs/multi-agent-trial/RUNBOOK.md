# Multi-Agent Trial Runbook — Running the Pre-Isolation Evaluation on a Turnstone Deployment

A practical operational runbook for standing up and running the `tools/multi-agent-trial` harness on
a Turnstone deployment with one or two additional agents (e.g. Hermes and/or OpenClaw) via their MCP
gateways. Companion to `docs/multi-agent-trial/METHODOLOGY.md` (design) and
`tools/multi-agent-trial/README.md` (harness reference).

## 1. Prerequisites

- A running Turnstone console with an API bearer token (file path → `TURNSTONE_TOKEN_FILE`).
- The agent gateways reachable as **stdio MCP adapters** (paths → `HERMES_MCP_ADAPTER` /
  `OPENCLAW_MCP_ADAPTER`). The harness spawns them itself; it does not call agent HTTP ports.
- (Recommended) a semantic reranker endpoint for preflights (`RERANK_URL` etc.); if absent, set the
  URLs and skip or stub the preflight.
- Node/npm for the TUI is **not** required by the harness (it drives gateways directly).

## 2. Configure

```bash
export TURNSTONE_TOKEN_FILE=/run/secrets/turnstone-token
export HERMES_MCP_ADAPTER=/path/to/hermes_gateway_mcp.py
export OPENCLAW_MCP_ADAPTER=/path/to/openclaw_gateway_mcp.py
export RERANK_HEALTH_URL=... RERANK_MODELS_URL=... RERANK_URL=...
export PROD_CHANNELS='["<never-emit-to-ids>"]'   # destinations that must never receive an emit
```

Edit `TASK_BODIES` in `trial_harness.py` for your domain tasks (keep them self-contained and
factually anchored). Keep the frozen bundle manifest approach: any task change = new bundle + new
sealed manifest.

## 3. Prove the pipeline (non-scored) before any scored run

```bash
python tools/multi-agent-trial/src/trial_harness.py --bundle-dir . --rehearsal
python tools/multi-agent-trial/src/trial_harness.py --bundle-dir . --c-review-preflight
```

Both must pass with **complete receipts** (turnstone/hermes/openclaw/webhook) and validator exit 0.
If the arm-C review preflight fails, do NOT proceed — fix the route first.

## 4. Run the scored trial

```bash
python tools/multi-agent-trial/src/trial_harness.py --bundle-dir . --block 1
python tools/multi-agent-trial/src/trial_harness.py --bundle-dir . --block 2
python tools/multi-agent-trial/src/trial_harness.py --bundle-dir . --block 3
# or all at once:
python tools/multi-agent-trial/src/trial_harness.py --bundle-dir . --all
```

Each block auto-runs: reranker preflight → arm-C review preflight → T5-style pin check → 6 cells →
post-block reranker preflight. Evidence lands in `--run-dir` (default `trial-runs/`).

**Between blocks, report integrity only** (runs completed, evidence valid, budget compliance,
reranker health, no prohibited side effects). **Do not** publish scores or arm comparisons until all
cells are collected and sealed.

## 5. Evaluate (blinded) after all cells

```bash
export OPENAI_CATALOG_TOKEN=...   # your evaluator gateway token
python tools/multi-agent-trial/src/evaluate.py <run-dir>/<cell>/<cell>-anonymized-output.json
```

`evaluate.py` is sealed to its pinned model and rejects overrides; it returns INDETERMINATE on
infrastructure failure (never a zero-quality score).

## 6. Reading results

- **Layer 1 (content):** mean of correctness+completeness+usefulness per cell; group by arm/task.
- **Layer 2 (ops):** exit codes — 0 PASS, 1 FAIL (prohibited side effect), 3 INDETERMINATE (missing
  receipt). Missing receipts are never inferred PASS.
- **Interpret honestly:** single executions are directional. In our pilot, coordinator+one agent
  beat solo on live multi-step tasks; the full three-agent arm added review/delivery capability but
  not general content lift; the biggest quality lever was **coordinator finalize discipline**.

## 7. Common failure modes (learned)

| Symptom | Cause | Fix |
|---|---|---|
| "Installing TUI dependencies" loop / Chat banner won't load | npm workspace lock mismatch **or** sandbox `PrivateDevices` blocking PTY | Full root `npm install` once; remove `PrivateDevices` from the dashboard unit (PTY-needing) |
| All agents fail to start after a host restart (`status=218/CAPABILITIES`) | systemd **user**-service drop-ins using settings user managers can't apply at boot: `CapabilityBoundingSet=`, `PrivateDevices`, `ProtectClock`, `ProtectKernelModules`, `ProtectKernelLogs` | Remove those 5 from `~/.config/systemd/user/*.service.d/`; keep the boot-safe set (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectKernelTunables`, `ProtectControlGroups`, `LockPersonality`, `RestrictRealtime`, `SystemCallArchitectures`, `RestrictAddressFamilies`, `MemoryMax`); validate from a clean login/start, not an in-session restart |
| All cells INDETERMINATE with empty task | Task bodies not embedded (ID-only prompts) | Put real bodies in `TASK_BODIES` |
| Arm-C review preflight fails | Coordinator re-verifies receipts via tool calls instead of finalizing | Scope prompt: "receipts are harness-verified; do NOT re-fetch" |
| Evaluator INDETERMINATE ×all | Evaluator gateway auth missing | Provide `OPENAI_CATALOG_TOKEN` (or your gateway token) |

## 8. Cleanup

- Evidence is sealed per run; archive runs you want to keep, delete or archive the rest.
- Remove the loopback webhook receiver when done (harness tears it down per block).
- Restore any sandbox flag you changed for PTY compatibility.

## 9. Boundaries

This runbook is for evaluating a **single-host** deployment (all agents on one machine, no host
migration). If your environment ever moves to per-agent hosts, re-validate the arm-isolation
assumptions before reusing the harness.
