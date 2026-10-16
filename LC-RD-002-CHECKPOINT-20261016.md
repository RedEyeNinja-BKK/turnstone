# LC-RD-002 — Clean Turnstone redeployment checkpoint (2026-10-16)

**Checkpoint authority:** operator-supplied production CLI/journal results, current branch `ops/turnstone-five-node-v187-20261016`, and the initial redeployment plan LC-RD-001. This is a snapshot, not a directive to mutate production. Prior August 3 legacy `localclaw-vm` operational baseline is **historical reference**, not current system inventory.

## Intent held from LC-RD-001

- Deploy **clean upstream Turnstone 1.8.7** as independent head on `turnstone-vm` (Debian 13), and later a separate worker VM.
- Preserve prior `localclaw-vm` as legacy reference/rollback; do not restore the old Turnstone/Hermes/OpenClaw three-agent runtime, Process Engine, Operational Commons, legacy DB, schedules, experimental skills, or synthetic shared-workspace.
- Selectively reintroduce proven source patches behind tests/rollback gates. `eous/turnstone-memory` is a read-only design reference, not assumed production-installed.
- Owner-administered privileges must **never** be narrowed/revoked without explicit approval. Keep keys out of reports/GitHub and do not silently alter routing/credential ownership.

## Current production facts

| Area | Status | Evidence |
|---|---|---|
| Turnstone VM | **LIVE** | Native v1.8.7, `/opt/turnstone-venv`, PostgreSQL 17 `turnstone`, console :8090 |
| Head server | **PASS** | `turnstone-head-1` on 127.0.0.1:8080 |
| Four new workers | **PASS health/registration** | `turnstone-node-{2,3,4,5}` :8082..:8085 |
| Shared discovery | **PASS** | Five distinct, fresh `services` rows; worker starts checked HTTP /health with node identity/version and PG service registry |
| Memory snapshot | **PASS at idle only** | 4 vCPU, 3.8 GiB RAM, ~1.5 GiB used and 2.2 GiB available, zero swap usage after all workers enabled |
| Typed Output Guard | **PASS on five nodes** | `tier1-decision` contract `switchyard-decision:v1`, backend `direct-openrouter-span-01-lite`, child `smartfree-decision`, served `respan/span-01-lite-20260925` |
| Finishing overlay | **PASS** | `/opt/turnstone-overlays/typed-decision-v187-finish`, 34/34 regression, 10/10 mutation kills, six calibration fixtures, direct renderer smoke and real-workstream head/worker checks |
| Real bash execution | **PARTIAL evidence** | Explicit stdout/exit receipts supplied for head and worker 2. Worker 3–5 judge logs verified; command stdout and exit codes not supplied |
| Five simultaneous active jobs | **NOT TESTED** | Do not infer from idle health/memory |
| Full Switchyard model catalog | **PENDING** | Current route publication vs internal manifest and editable model DB inventory need read-only reconciliation |
| OpenRouter five models | **PENDING** | Existing manually entered `z-ai/glm-5.3-flash` expected credential anchor, uninspected |
| New Codex device OAuth | **PENDING** | `gpt-6-luna`, `gpt-6-luna-nt`, `gpt-6.1-sol` requested alias names; entitlement/provider identity and token refresh need proof |
| HTPC / comfyninja | **FUTURE PLAN** | Tailnet and direct API reachability, SSH/credential gates, 3070 TH/3090 CA, Switchyard smart route stays authoritative |
| Worker VM and chummythailand.com orchestration | **FUTURE** | Separate node/agent skill architecture and business workflows follow provider connectivity |

**Rollback/provenance:** 
- Tested head first-pass and finishing overlays retained with `90-typed-decision-v187.conf` and `91-typed-decision-v187-finish.conf`; upstream installation unmodified.
- Five-worker unit `turnstone-node@.service`; rollout script `scripts/turnstone_five_node_v187.sh`; proof `TURNSTONE-V187-FIVE-NODE-BASELINE.md` (node 3–5 journal acceptance commit `0b7dc457`).
- Root-only backups `/var/backups/turnstone/typed-decision-v187-finish-20261016` and `/var/backups/turnstone/five-node-v187-20261016`. No removal/restore authorized.

## Next gates (in strict order)

### P1 — Switchyard editable model discovery
Capture authoritative Switchyard route manifest **and** authenticated `GET /v1/models`; distinguish smart routes, local/cloud direct routes, typed `/v1/decisions` routes, hidden/internal routes, and output that is not an inference model. Inspect the existing Turnstone `model_definitions` records with **API-key presence only**, not plaintext. Stage a source-aware **diff** (add/unchanged/manual override/conflict/stale), preserve user-editable console rows and all manual changes; no automatic delete/update of owner edits. Use Turnstone's upstream Models CRUD (DB-backed), **never hardcode an alias list into product source**. Probe Responses, reasoning control, served model and capability metadata on representative lanes before promoting.

### P2 — OpenRouter same-key five-model import
Respect exact operator-requested API slugs:
1. `inclusionai/ling-3.0-flash-fin`
2. `liquid/lfm-2.5-2.6b:free`
3. `qwen/qwen3.8-flash`
4. `qwen/qwen3.7-flash`
5. `nvidia/nemotron-3.5-super-vl:free`

Current manually added `z-ai/glm-5.3-flash` is the intended reference. Confirm its provider/URL and active credential without printing secrets; use OpenRouter `GET /api/v1/models` and per-model details to verify exact IDs, modalities, current terms, contexts, pricing/free endpoint status. Stage five independent editable Turnstone DB model definitions via existing CRUD. Avoid secret duplication where a supported credential reference exists; do not invent one. Run bounded harmless probes and verify served model/route. **Free** Liquid and NVIDIA endpoints may retain/process user content, and the NVIDIA free model warns against uploading sensitive personal or confidential information; no sensitive workloads before policy review.

### P3 — OpenAI Codex device OAuth and requested aliases
Headless Codex CLI supports `codex login --device-auth` and `codex login status` ([CLI docs](https://learn.chatgpt.com/docs/cli/reference)). Confirm CLI availability on VM and intended credential-owning OS user before invoking interactive sign-in. The operator completes browser code prompt. Do not log or expose tokens, or assume Codex CLI OAuth credential is an arbitrary OpenAI API key. Validate supported Codex model catalog/auth boundary and actual provider model IDs for operator-requested aliases `gpt-6-luna`, `gpt-6-luna-nt` (genuine no-thinking semantics required), `gpt-6.1-sol`. Stage adapter only if a supported transport exists; test interactive login, provider response, refresh/revocation and routing, then publish editable model rows. Keep old Switchyard OAuth refresh **single-writer**, do not create competing refresh timers.

### P4 — HTPC + comfyninja direct mesh / worker-VM plan
First inventory Tailscale identities/ACL, actual inference/media ports and supported protocols, serving model catalogue, latency. HTPC 3070 Thailand remains first low-cost lane; comfyninja/RENINJA 3090 Canada is escalation and concurrently handles Unsloth + ComfyUI. Design explicit difference between *direct API entries* and Switchyard-mediated smart routing; **Switchyard is not replaced**. SSH/credentials/elevated access changes are separate operator-go gates, with target host/user privileges specified and preserved.

## Next read-only operator inventory

```bash
sudo -u postgres psql -d turnstone -X -P pager=off -c "
SELECT alias, model, provider, base_url, enabled,
       CASE WHEN coalesce(api_key,'') <> '' THEN 'configured' ELSE 'absent' END AS credential,
       context_window, reasoning_effort, auth_mode
FROM model_definitions ORDER BY alias;"
printf 'Codex CLI: '; command -v codex || true
if command -v codex >/dev/null 2>&1; then codex login status 2>&1 | head -n 3; fi
```

Model discovery/synchronization is **not** deployed at this checkpoint. All subsequent writes require a reviewed diff, staged receipts, operator approval and read-back. Workstream-baseline acceptance still awaits explicit bash stdout/exit receipts on worker 3–5 if the operator wishes full per-node tool-execution closure.
