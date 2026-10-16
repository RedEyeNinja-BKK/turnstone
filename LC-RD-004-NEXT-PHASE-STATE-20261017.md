# LC-RD-003 closure and next-phase state (2026-10-17)

LC-RD-003 is accepted by the operator and closed. Commit `c5f9b69d` and its
acceptance record (`LC-RD-003H`) are the accepted evidence. No further
authentication work, credential issuance, or MCP reimplementation.

This document is the **state report for the next deployment phase** — measured
from the running system, not planned. Hostnames only; no addresses or secrets.

---

## Phase 1 — Native skills and Grafana capability

> **Corrected and superseded.** This section originally reported "skills: none
> installed — `skill_versions` has 0 rows". That used the wrong table:
> `skill_versions` holds only version *history*, while installed skills live in
> **`prompt_templates`**. Both skills have since been installed and are enabled
> — see `LC-RD-004-MODEL-ONBOARDING-20261017.md`. The section's other
> measurements (Grafana health, the 401, the external-only discover path) stand.

| Item | Measured state |
|---|---|
| Skills installed in Turnstone | **2, enabled** — `turnstone-self-management`, `grafana-operations` (stored in `prompt_templates`) |
| Skills ready in the repo | 2 — `skills/turnstone-self-management/SKILL.md`, `skills/grafana-operations/SKILL.md` |
| Skill install path | `POST /v1/api/admin/skills` (needs `admin.skills`), body carries `name`, `content`, `description`, `category`, `kind`, `version`, `author`, `license` |
| `discover` endpoint | searches an **external** registry (skills.sh) — not the local repo; it will not find these two |
| Grafana | **healthy**: `https://grafana.bkk.redeyeninja.com/api/health` → 200, version `13.2.4`, database `ok` |
| Grafana API auth | `/api/version` → 401 — the API needs a token; none is held and none should be issued |
| Session-level MCP injection | **not verified from here** — see Blockers |

The Grafana target in the skill (`grafana.bkk.redeyeninja.com`) is correct and
live. A local `127.0.0.1:3000` probe returns nothing: Grafana is not hosted on
turnstone-vm.

## Phase 2 — Model catalog reconciliation (Switchyard ↔ Turnstone)

Switchyard's live manifest was fetched from
`https://switchyard-vm.tailc8ef7b.ts.net/v1/models`: **28 models**, all
`owned_by: switchyard`, with `context_length` and `capabilities`.

Turnstone's catalog holds **5** definitions. Drift found:

| Alias | Turnstone `base_url` / model | Turnstone ctx | Finding |
|---|---|---|---|
| `tier1-decision` | `https://switchyard-vm.tailc8ef7b.ts.net` / `tier1-decision` | **0** | **Broken twice over** (see below) |
| `smart-bounded` | `…tailc8ef7b.ts.net/v1` / `smart-bounded` | 262144 | model exists; ctx drift (manifest says 1000000) |
| `deepseek-deepseek-flash` | `https://api.deepseek.com` / `deepseek-flash` | 500000 | direct provider; Switchyard also routes deepseek at 1048576 |
| `openrouter-glm-5.3-flash` | `https://openrouter.ai/api/v1` / `z-ai/glm-5.3-flash` | 500000 | direct provider; manifest has `direct-openrouter-glm-5_3-flash` at 1048576 |
| `thaillm-typhoon` | `https://thaillm.or.th/api/v1` / `Typhoon-S-ThaiLLM-8B-Instruct` | 16384 | matches `direct-thaillm-typhoon` (16384) |

### `tier1-decision` is live-broken, and the cause is twofold

1. **`base_url` is missing `/v1`.** Verified by probe:
   `/chat/completions` → **404** `endpoint_not_found`, while
   `/v1/chat/completions` → **400** (endpoint exists; only the body was empty).
2. **The model id does not exist on Switchyard.** The manifest contains
   `smart`, `smart-bounded`, `smartfree`, `smartlocal`, `smartlocal-bounded` —
   there is **no** `tier1-decision`. So even with `/v1` corrected, this
   definition would 404.

`context_window` is also `0`, which is why `turnstone-doctor` reported
"context_window is 0 (auto-detect) but the endpoint probe failed (404)".

This alias is **exposed to live sessions** — it appears in the model choices
available to this workstream — so the breakage is user-visible, not latent.

**Not changed.** The model field and any Switchyard routing choice are routing
decisions, and the instruction is to preserve them. Correcting the `base_url`
is mechanical; repointing the `model` requires a decision about which
Switchyard model `tier1-decision` should mean. Proposed, pending a decision.

### Switchyard capacity not yet exposed

23 of 28 Switchyard models have no Turnstone definition, including the whole
`direct-openai-gpt-6-*` family (1.05M ctx, vision) and several 1M-ctx
openrouter routes. Adding definitions is additive and does not disturb existing
routes.

## Phase 3 — Remote infrastructure and the worker VM

Tailnet peers currently **online**: `turnstone-vm`, `switchyard-vm` (active,
direct), `openclaw-vm`, `agent-vm`, `docker-vm`, `localclaw-vm`, `htpc-wsl`,
`reninja`, `vivobook16-vc`, `pfsense`, `pfs-1`.

SSH reachability (port 22):

| Host | SSH |
|---|---|
| `openclaw-vm` | open |
| `agent-vm` | open |
| `docker-vm` | open |
| `localclaw-vm` | open (legacy reference only) |

**No `hermes` or `dsh` host exists in the tailnet.** The separate
Hermes/OpenClaw/DSH execution-worker VM has therefore **not been provisioned** —
this is the single largest gap in the next phase, and it gates priority 4.
`openclaw-vm` is online and is the natural first candidate to host or host
alongside the executor, but that is a decision, not a measurement.

## Phase 4 — chummythailand.com

`https://chummythailand.com` → **HTTP 200**, live. No agent workflow is wired to
it yet, and a reliable executor is the stated precondition.

---

## Actionable immediately (no new authorization needed)

> Items 1–3 and 5 below are **done** — see
> `LC-RD-004-MODEL-ONBOARDING-20261017.md`. Item 2's session-level half
> (fresh-workstream MCP injection) remains unverifiable from this session.

1. **Install the two repo skills** via `POST /v1/api/admin/skills` — content is
   in-repo, no external dependency, no privilege change. Verify with
   `GET /v1/api/admin/skills` and by seeing them in a session's skill list.
2. **Verify session-level MCP tool injection** in a fresh workstream started
   after registration, and run one authenticated read there. This is the
   outstanding half of the operator's priority 1.
3. **Correct `tier1-decision`'s `base_url`** to `…ts.net/v1` (mechanical,
   keeps the alias and the routing target unchanged).
4. **Add Switchyard model definitions** for high-value unexposed routes
   (additive; existing definitions untouched).
5. **Re-snapshot the catalog** against the manifest after any change, and record
   the diff — the reconciliation is repeatable now that both sides are readable.

## Genuine blockers

| # | Blocker | Type |
|---|---|---|
| 1 | **No fresh workstream can be created from this side** — session-level MCP injection and skill visibility cannot be verified in-session; this session predates MCP registration and its tool list is fixed at session start | capability |
| 2 | **`tier1-decision` needs a decision, not a fix**: which Switchyard model should back it? | decision |
| 3 | **Grafana HTTP API needs a token** (401). Not requested; no issuance authorized | credential |
| 4 | **Worker VM not provisioned** — no hermes/dsh host; blocks priority 4 | infrastructure |
| 5 | Installed-bytes smoke **receipt artifact** needs root (`euid 0`); operator-run housekeeping, substance already verified via the launcher path | privilege |

## Preserved (verified unchanged)

Existing credentials, privileges, the memory policy, the running MCP
installation, and all healthy services. No permission reduction, credential
rotation, or infrastructure replacement was performed.
