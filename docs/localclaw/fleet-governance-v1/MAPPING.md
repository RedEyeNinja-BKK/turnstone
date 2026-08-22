# Fleet Governance v1 — Mapping to Existing Turnstone Mechanisms

**Status:** DESIGN ARTIFACT. Not deployed.

Fleet Governance v1 deliberately **reuses** the existing stack rather than adding
new infrastructure. This mapping is the contract between the BWP family and the
mechanisms that already exist and are proven.

## 1. Workstreams (native API) — provenance + lifecycle + evidence trail

| BWP need | Existing mechanism |
|---|---|
| Packet provenance (`workstream_id`) | Workstream object (native API `/v1/api/workstreams`) |
| Lifecycle (create → approve → close) | Workstream endpoints (`new`, `approve`, `close`, `restrict`, `trust`, `cancel`) |
| Evidence trail / receipt linkage | Workstream `events`, `history`, `export`, `attachments` |
| Independent review artifact | Attachments + review report in workstream |

**Rule:** a BWP REQUEST is authored *in* a workstream; `packet_id` is stable
across the four lifecycle objects; workstream export is a receipt link.

## 2. task_agent / adaptive-ingress metadata — the structural work-class wire

| BWP field | Existing mechanism |
|---|---|
| `work.work_shape` | `task_agent` REQUIRED `work_shape` (bounded\|agentic) — identical enum, fail-closed admission |
| `work.reasoning_intent` | Adaptive-ingress orthogonal contract `work_shape × reasoning_intent` |
| Wire transport | Turnstone alias `capabilities.server_compat.extra_body` → OpenAI `extra_body` → Switchyard extensions (request-sourced, never prose) |
| Missing metadata behavior | HARD ERROR — never guessed from prompt text (proven, closed S2-H) |

**Rule:** the BWP `work` fields are the governance-side origin of the exact
metadata that FleetRouter already consumes. BWP does not add a second transport;
it disciplines the production of the existing one.

## 3. Delegation envelopes (Hermes / OpenClaw) — executor assignment

| BWP need | Existing mechanism |
|---|---|
| Executor assignment | `hermes-delegation` / `openclaw-*` skills + MCP gateways (`hermes_agent_submit`, `openclaw_agent_run`, …); derived via `derive_executor_candidates()` / `select_executor()` (capability + authority + placement) |
| Envelope minimums | correlation id, intent+outcome, acceptance criteria, allowed/forbidden, risk class, timeout/cancel, evidence, escalation — already mandated in `identity/working-rules.md` + `three-agent-iteration-loop` |
| Run correlation IDs | `run_ids` on Hermes runs / OpenClaw `task_id` |
| Inference-resource selection | FleetRouter / Switchyard only — from structural `work_shape` + `reasoning_intent`; never from Turnstone, never from executor choice |

**Rule:** ASSIGNMENT formalizes the existing envelope; the BWP REQUEST supplies
the structured content (acceptance, authority, evidence) that the envelope prose
previously carried informally.

**Rule (second-router prevention):** executor selection is capability/authority/placement-derived
ONLY. `work_shape` and `reasoning_intent` describe inference semantics and flow to FleetRouter;
they NEVER select an executor. There is no mechanical `agentic → Hermes` or
`bounded → Turnstone/OpenClaw` mapping (validator control C1 proves shape-independence).

## 4. Evidence mechanisms — receipts and epistemology

| BWP need | Existing mechanism |
|---|---|
| Claim epistemology (PROVEN/PROPOSED/ASSUMED/FAILED/INDETERMINATE) | Existing evidence discipline (working rules, `hermes-verification`, `openclaw-verification`) |
| Read-back verification | `hermes_run_events`, transcript read-back, on-disk verification |
| Hashes | artifact sha256 conventions |
| Routing telemetry | `routing.jsonl`, journal, exporter daily JSON |
| Run IDs ≠ proof | Standing doctrine |

**Rule:** EVIDENCE RECEIPT is the structural container for what executors already
produce; VERDICT maps evidence → outcome with the explicit two-vocabulary split.

## 5. FleetRouter / Switchyard boundary

| BWP need | Existing mechanism |
|---|---|
| Resource eligibility from work requirements | FleetRouter (closed S2-H baseline): work_shape + reasoning_intent + readiness + context fit |
| Preference among eligible | Router preference stage; preferences INACTIVE in BWP v1 |
| Actual resource selection | Router selects; observed in RECEIPT `resource_observed` |

**Rule:** Turnstone produces requirements; FleetRouter selects resources. The BWP
never crosses this line (validator-enforced).

## 6. Control-plane reuse (no new infrastructure)

| BWP need | Existing mechanism |
|---|---|
| Governance record / tamper evidence | Mutation ledger (`mutation-ledger.jsonl`) |
| Deployment safety | Self-dependency guard (future integration step) |
| Semantic aliases | `switchyard-smart-bounded/agentic/reasoning` work-class pins (unchanged) |

## 7. What is genuinely NEW in v1 (and only this)

1. A **validated packet schema family** (REQUEST / ASSIGNMENT / RECEIPT / VERDICT) — previously prose conventions.
2. A **deterministic sufficiency gate** — previously implicit.
3. A **deterministic validator** — previously no executable contract.
4. **Explicit ownership/value-provenance** — previously implicit.

Everything else is reuse. This is the "consolidation, not parallel framework" test: the design document,
schemas, and validator can be re-read and none of them requires a new daemon, table, MCP server, or runtime hook.
