# Fleet Governance v1 — Design Artifacts (2026-08-22)

**Status:** DESIGN-ARTIFACT MILESTONE — authorized by Vincent. **NOT integrated, NOT activated, NOT deployed.**
**Boundary:** Workspace artifacts + local validation only. No Switchyard production, no FleetRouter, no routing semantics, no agent roster, no MCP config, no model catalog, no PR #10, no GitHub, no production governance/runtime behavior.

---

## Purpose

Establish the **Bounded Work Packet (BWP)** contract: a consolidation/structuralization of existing
Turnstone governance primitives that makes Turnstone a disciplined **producer of work requirements**,
while preserving the boundary:

> **Turnstone may declare RESOURCE REQUIREMENTS. Turnstone should not normally select PROVIDER/MODEL identities.**

The actual Switchyard-selected inference resource is an **observed receipt fact**, never a BWP input.

## Artifact manifest

| # | Artifact | Path | Contents |
|---|---|---|---|
| 1 | Design document | `FLEET-GOVERNANCE-V1-DESIGN.md` | Architecture, lifecycle, doctrine, corrections, boundary/leakage audit, out-of-scope |
| 2 | BWP schema v0.1 | `schema/bwp-request-v0.1.json` | Request / requirements object (the packet) |
| 3 | Assignment schema v0.1 | `schema/bwp-assignment-v0.1.json` | Executor assignment object |
| 4 | Evidence Receipt schema v0.1 | `schema/bwp-evidence-receipt-v0.1.json` | Executor/observed facts object |
| 5 | Verdict schema v0.1 | `schema/bwp-verdict-v0.1.json` | Turnstone adjudication object |
| 6 | Vocabularies | `schema/vocabularies.json` | Semantic capability / action / evidence-type / enum vocabularies |
| 7 | Validator | `validator/validate_bwp.py` | Deterministic validator + adjudicator + self-test suite (stdlib only): sufficiency gate + disposition (DISPATCH/CLARIFY/REJECT), structural identity rejection, prose-is-not-routing-truth warnings, executor eligibility/assignment (`derive_executor_candidates()`/`select_executor()`, never work_shape), evidence sufficiency, deterministic verdict |
| 8 | Qualification examples | `examples/qualification-{1..5}.json` | Five representative packets |
| 9 | Validation report | `VALIDATION.md` | Structural + rejection-matrix validation results |
| 10 | Skill outline | `fleet-governance-v1-skill-outline.md` | Skill design for packet production |
| 11 | Ownership matrix | `OWNERSHIP-MATRIX.md` | Decision ownership across layers |
| 12 | Mapping | `MAPPING.md` | Mapping to existing Turnstone mechanisms + FleetRouter boundary |

## How to validate (local, read-only)

```bash
cd /home/vincent/shared-workspace/operations/fleet-governance-v1-2026-08-22
python3 validator/validate_bwp.py
```

Exit code 0 = all structural checks + all five qualification examples + all seven
rejection demonstrations passed as expected.

## Lifecycle model (four distinct objects, shared provenance)

```
operator intent
  → BWP REQUIREMENTS   (Turnstone-authored; sufficiency gate; hard requirements + inactive preferences)
  → ASSIGNMENT         (Turnstone derives executor from capabilities + authority; NOT a manual routing list)
  → execution
  → EVIDENCE RECEIPT   (executor reports truthfully + observed facts; claims carry epistemology status)
  → VERDICT            (Turnstone adjudicates PASS / FAIL / INDETERMINATE / ESCALATED)
```

Provenance (`packet_id`, `correlation_id`, `workstream_id`) is shared across all four objects.
Ownership and timing are explicit per object.

## Key corrections incorporated (from Vincent, 2026-08-22)

1. Capabilities are **semantic** — never inference-resource identities (no `htpc-llm`, no model/provider/GPU endpoint).
2. **REQUEST / ASSIGNMENT / RECEIPT / VERDICT** are separate objects with explicit ownership/timing.
3. **Evidence epistemology** (`PROVEN/PROPOSED/ASSUMED/FAILED/INDETERMINATE`) is separate from **work outcome** (`PASS/FAIL/INDETERMINATE/ESCALATED`).
4. **Hard requirements vs preferences**: hard → eligibility; preference → among eligible only; preference never rescues an invalid option.
5. **No hydration/economics absorption**: minimal context seams + explicitly inactive preference hooks only.
