# Fleet Governance v1 — Ownership Matrix

**Status:** DESIGN ARTIFACT. Not deployed.

Every decision in the BWP lifecycle has exactly one owner. This matrix is the
authority contract; leakage is a design defect (see §10 of the design document
for the leakage audit).

## Primary ownership

| Decision | Owner | Mechanism |
|---|---|---|
| Operator intent, priorities, tradeoffs, material decisions | **Vincent (operator)** | Input to Turnstone intent capture; approval gates; escalation terminus |
| Intent normalization, outcome definition, non-goals | **Turnstone** | `intent` section of REQUEST |
| Task-sufficiency determination | **Turnstone** | `intent.sufficiency` gate — only SUFFICIENT dispatches |
| Work class (work_shape) and reasoning intent | **Turnstone** | `work` section — structural, REQUIRED, never from prose |
| Required capabilities (semantic) | **Turnstone** | `requirements.capabilities_required` — vocabulary-constrained, identity-forbidden |
| Abstract resource requirements (locality, context, output budget) | **Turnstone** | `requirements.*` — constraints, not selections |
| Authority envelope (risk class, allowed/forbidden actions, mutation envelope) | **Turnstone** (from operator grants) | `authority` section |
| Acceptance criteria | **Turnstone** (operator-confirmed where material) | `acceptance.criteria` |
| Evidence requirements | **Turnstone** | `evidence.requirements` |
| Timeout / cancel / escalation control | **Turnstone** | `control` section |
| Executor selection | **Turnstone** (capability + authority + placement derived; NEVER from work_shape) | ASSIGNMENT object (`derive_executor_candidates()` / `select_executor()` spec) |
| Execution within the delegated envelope | **Hermes / OpenClaw / Turnstone-native** | Executor runtime + delegation envelope |
| Truthful reporting of what happened | **Executor** | EVIDENCE RECEIPT (claims + epistemology status) |
| Resource eligibility + preference from structured requirements + factual readiness | **FleetRouter / Switchyard** | Router runtime (closed S2-H baseline) |
| Factual GPU/resource/current-state and transitions | **ComfyNinja** | Factual authority (read-only consumption) |
| Work outcome adjudication (PASS/FAIL/INDETERMINATE/ESCALATED) | **Turnstone** | VERDICT object (deterministic adjudicator) |
| Independent material review (pre-integration) | **Independent reviewer (Hermes or separate lane)** | Review gate per working cadence |

## Value provenance per lifecycle stage

| Value | Origin |
|---|---|
| Outcome, non-goals, operator identity, authorized scope | **Operator supplied** |
| Sufficiency, work_shape, reasoning_intent, capabilities, locality, authority, acceptance, evidence, control | **Turnstone derived** |
| Executor, derivation rationale, run correlation IDs | **Turnstone assigned at dispatch** |
| Resource/model/provider actually selected | **FleetRouter selected — observed during execution** |
| Actions taken, artifacts, claims, failures | **Observed during execution (executor-reported)** |
| Work outcome, authority compliance, evidence sufficiency, closeout | **Turnstone adjudicated afterward** |

## Explicit non-ownership (leakage prevention)

| Non-owner | Must NOT decide |
|---|---|
| Turnstone | Provider/model/resource identity selection (may declare requirements that constrain eligibility, e.g. `local_required`); executor selection via `work_shape`/`reasoning_intent` (those describe inference semantics for FleetRouter only) |
| FleetRouter | Task importance, governance, acceptance, sufficiency |
| Executors | Broadening authority beyond the envelope; re-deriving routing from prose |
| Prompt prose | Anything structural — routing metadata exists only in `work` fields |
| Preferences | Eligibility (never rescue an invalid option) |
| Hydration machinery | Governance intent (seams only in v1) |
