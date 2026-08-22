# Skill Design — `fleet-governance-v1` (outline)

**Status:** DESIGN OUTLINE ONLY. The skill is NOT created in the catalog. Creation is
a runtime-integration step requiring a separate Vincent GO.

## Identity & purpose

- **Name:** `fleet-governance-v1`
- **Category:** governance
- **Kind:** any (interactive + coordinator)
- **Activation:** named (loaded for governance/execution-intent work)
- **Description (proposed):** Produce disciplined Bounded Work Packets (BWP) for
  Turnstone governance: task-sufficiency determination, structured work/resource
  requirements, authority envelopes, acceptance/evidence contracts, and
  adjudication — while preserving the boundary that FleetRouter selects
  resources, never Turnstone.

## Durable principles encoded (no mutable inventory)

1. **Turnstone governs intent; FleetRouter governs resource selection.**
   Provider/model identities never appear in governance requirements.
2. **Capabilities are semantic** — `text_generation`, `structured_extraction`,
   `web_retrieval`, `container_management`, `local_execution`, … from the v1
   vocabulary. Never `htpc-llm` / a model / a provider / a GPU endpoint.
3. **Sufficiency before dispatch** — AMBIGUOUS/INSUFFICIENT packets never
   dispatch; deterministic disposition SUFFICIENT→DISPATCH, AMBIGUOUS→CLARIFY,
   INSUFFICIENT→REJECT (escalation where governance demands).
4. **Four lifecycle objects, explicit ownership** — REQUEST (Turnstone),
   ASSIGNMENT (Turnstone-derived), RECEIPT (executor + observed), VERDICT
   (Turnstone adjudicated).
4a. **Executor selection is NOT a second router** — `derive_executor_candidates()` /
   `select_executor()` derive the executor from capabilities + authority + placement
   ONLY. `work_shape` / `reasoning_intent` describe inference semantics for
   FleetRouter and NEVER select an executor (no agentic→Hermes / bounded→Turnstone
   mapping).
5. **Two vocabularies stay separate** — evidence epistemology
   (PROVEN/PROPOSED/ASSUMED/FAILED/INDETERMINATE) vs work outcome
   (PASS/FAIL/INDETERMINATE/ESCALATED).
6. **Hard requirements determine eligibility; preferences operate only among
   eligible choices; a preference never rescues an invalid option.**
7. **Structural metadata only; prose never routing truth** — routing truth lives
   in `work` fields, never in prompt prose. Prose mention of providers/models/
   resources is permitted (warning/audit signal only); structural identity
   encoding is rejected.
8. **Evidence before acceptance** — PASS requires every BWP evidence requirement
   satisfied by PROVEN claims.

## Procedure outline

1. **Capture intent** — outcome, non-goals, operator constraints; identify
   material decisions needing operator confirmation.
2. **Determine sufficiency** — explicit gate; if AMBIGUOUS/INSUFFICIENT, produce
   the clarification/escalation packet and STOP (no dispatch).
3. **Author REQUEST** — per schema v0.1: work fields, semantic capabilities,
   inference_locality (resource-scoped only; never selects the executor),
   context/output-budget, authority envelope, acceptance, evidence,
   control.
4. **Validate** — run the deterministic validator; any error → fix, never
   dispatch invalid.
5. **Derive ASSIGNMENT** — executor from capabilities + authority
   (`derive_executor_candidates()` / `select_executor()`); capability gap →
   report, do not assign; never from work_shape; never from inference_locality
   (resource-scoped, FleetRouter only).
6. **Dispatch with delegation envelope** — existing hermes/openclaw protocols;
   structured metadata handoff (work_shape/reasoning_intent).
7. **Collect RECEIPT** — truthful executor report + observed facts (including
   observed resource where telemetry exists); epistemology statuses.
8. **Adjudicate VERDICT** — deterministic rules; close, escalate, or reconcile.
9. **Reconcile** — workstream closeout, receipt links, ledger where applicable.

## References (living docs, not duplicated)

- `operations/fleet-governance-v1-2026-08-22/` (design, schemas, validator, examples)
- `identity/working-rules.md` (executor selection)
- `operations/turnstone-deployment-facts.yaml` (deployment identity)
- Live discovery for MCP tool surfaces (`tool_search`, `mcp_servers_list`)

## Guardrails

- Never creates provider/model selection authority.
- Never overrides FleetRouter eligibility or acceptance authority.
- Never embeds credentials (logical refs only).
- Never lets an executor silently broaden the authority envelope.
- Never treats a run ID as completion proof.
- Runtime integration (skill creation, validator deployment, production use)
  requires separate Vincent GO.
