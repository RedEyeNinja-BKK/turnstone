# Fleet Governance v1 — Validation Report (targeted-correction pass + Hermes review fixes)

**Date:** 2026-08-22. **Type:** structural + rejection/control validation of design artifacts (local, read-only).
**Validator:** `validator/validate_bwp.py` (stdlib, deterministic). **Result: ALL PASS (exit 0).**
**Corrections incorporated:** (1) prose is never hard-failing routing validation — mention is warning-only; structural identity encoding still rejected; (2) executor selection reframed as `derive_executor_candidates()` / `select_executor()` — capability/authority/placement only, never `work_shape`.
**Hermes independent material review:** run `run_ea85e5db699c4ba3a1307595451811c3` — REVISE with 2 load-bearing findings (LB-1, LB-2). Both fixed narrowly; Hermes confirmation run `run_6bb98db61331410594052d1eb50aa7f3` confirmed LB-2 fully fixed and LB-1 top-level fixed but identified the depth-1 LB-1 variant (nested additionalProperties:false not enforced). Depth fix landed (recursive schema-shape enforcement + R10c/R10d/R10e demos). Final confirmation run `run_6747ed9f41c74356ad4f76382fb81ba0`: **CONFIRMED-FIXED, no remaining load-bearing findings** — FGV1 DESIGN closes PASS / READY FOR RUNTIME-INTEGRATION PLANNING.

---

## 1. Artifact structural integrity

| Artifact | Check | Result |
|---|---|---|
| `schema/vocabularies.json` + 4 BWP schemas | Valid JSON, loaded by validator | PASS |
| `validator/validate_bwp.py` | `py_compile` clean | PASS |
| 5 example packets | Loadable JSON + structurally valid | PASS |

## 2. Qualification examples (full lifecycle demonstration)

Each positive example: `validate_bwp` → `validate_for_dispatch` → `select_executor`/`validate_assignment` → `validate_receipt` → `adjudicate` → `validate_verdict`.

| Trial | work_shape × reasoning | Derived executor | Dispatch | Verdict | Result |
|---|---|---|---|---|---|
| T1 bounded routine | bounded × none | turnstone-native (advisory + non-mutating + capable) | allowed | PASS | ✅ |
| T2 agentic non-deliberate | agentic × none | hermes (capability: network_access + long_context_processing) | allowed | PASS | ✅ |
| T3 deliberate/reasoning | agentic × deliberate | hermes (capability: document_parsing) | allowed | PASS | ✅ |
| T5 real-estate workload | bounded × none | hermes (capability: document_parsing) | allowed | PASS | ✅ |

**T4 (insufficient/ambiguous)** is structurally valid but **never dispatches**: disposition **CLARIFY**. It is a CLARIFY record in the workstream; no assignment is created.

## 3. Rejection demonstrations (must fail closed)

| ID | Requirement | Result |
|---|---|---|
| R1 | Ambiguous work must not dispatch (disposition CLARIFY) | ✅ |
| R2 | Resource identity as capability (`htpc-llm`) rejected | ✅ |
| R2b | `requirements.resource = "HTPC"` rejected (forbidden key) | ✅ |
| R8 | `requirements.model = "DeepSeek"` rejected (forbidden key) | ✅ |
| R3 | Missing structural `work_shape` rejected | ✅ |
| R4 | Preference never rescues an ineligible option | ✅ |
| R5 | Executor broadening mutation authority → FAIL verdict | ✅ |
| R6 | PASS without required evidence → INDETERMINATE | ✅ |
| R7b | Missing `reasoning_intent` rejected (prose cannot substitute) | ✅ |
| R9 | ASSIGNMENT carrying provider/model identity rejected | ✅ |
| R10 | Synonym-key identity smuggling in requirements (`required_model`) rejected (additionalProperties:false) | ✅ |
| R10b | Synonym-key identity smuggling in provenance (`resource`) rejected | ✅ |
| R10c | Nested synonym-key smuggling in `requirements.context` rejected (depth fix) | ✅ |
| R10d | Nested synonym-key smuggling in `assignment.assigned` rejected (depth fix) | ✅ |
| R10e | Unknown top-level key in receipt rejected (depth fix) | ✅ |
| R11 | Blocking INDETERMINATE claim prevents PASS → INDETERMINATE | ✅ |
| R12 | Proven hard-constraint violation (local_required + hosted resource) → FAIL, VIOLATED | ✅ |

## 4. Positive controls (must remain valid / shape-independent)

| ID | Requirement | Result |
|---|---|---|
| R7 (corrected) | Prose mention of identities (HTPC, flash thinking, DeepSeek, quoted log) → packet stays **valid** and dispatches; produces warnings only | ✅ (4 warnings, dispatch allowed) |
| C1 | Executor selection is **shape-independent**: T1 relabeled agentic still → turnstone-native; T2 relabeled bounded still → hermes; rationale never cites work_shape | ✅ |
| C2 | Sufficiency disposition deterministic: SUFFICIENT→DISPATCH, AMBIGUOUS→CLARIFY, INSUFFICIENT→REJECT | ✅ |
| C3 | ESCALATED outcome reachable (operator-gated) with hard constraints NOT_EVALUATED | ✅ |

## 5. Provenance separation checks

- **REQUEST** carries requirements only; structural identity keys rejected; capabilities vocabulary-enforced; prose mention warning-only.
- **ASSIGNMENT** carries executor + derivation only; provider/model identity rejected (R9).
- **RECEIPT** is the only object that may carry observed resource identity (`resource_observed`), explicitly marked observed fact.
- **VERDICT** separates `work_outcome` from `basis.evidence_epistemology`.

## 6. Hard-requirement vs preference semantics

Hard (capabilities, locality, context capacity, reasoning support, work-shape admission) enforced in `eligibility()`; preferences reserved (`active:false` enforced) and applied only among eligible. R4 proves an ineligible candidate stays ineligible regardless of preference.

## 7. Notes / limitations (honest)

- The prose scan is a deterministic **warning tripwire**, not a semantic classifier. Warnings are audit signals, never blocking. Structural field requirements are the primary control.
- `derive_executor_candidates()` / `select_executor()` are **specification** functions, not runtime code; C1 is the executable proof that executor selection does not follow `work_shape`.
- Acceptance adjudication in this design-phase validator is conservative; authoritative per-criterion adjudication is a Turnstone review step — a recommended focus for the independent material review.
- Sufficiency disposition (CLARIFY/REJECT) is minimal; escalation is operator-gated (never gate-computed) — the independent review assessed this as adequate.
- `apply_preferences()` is a stub in v1 (preferences inactive): R4 proves ineligible stays out; ranking among eligible is not exercised until a later phase.

## 8. Hermes review findings — disposition (run `run_ea85e5db…`)

### Load-bearing (fixed in this pass)

| ID | Finding | Fix | New demonstration |
|---|---|---|---|
| LB-1 | Validator did not enforce `additionalProperties:false`; synonym-key identity smuggling (`requirements.required_model`, `provenance.resource`) passed | `_enforce_schema_shape()` — recursive additionalProperties:false enforcement driven by the actual schema JSON files, applied to REQUEST (all depths) + ASSIGNMENT + RECEIPT + VERDICT; whole-packet forbidden-identity-key scan added. Hermes depth-1 EXT-1..6 probes (requirements.context / intent.sufficiency / authority.mutation_envelope / preferences[] / assignment.assigned / receipt.required_model) all now rejected | R10, R10b, R10c, R10d, R10e |
| LB-2 | `adjudicate()` could emit PASS with (a) a blocking INDETERMINATE claim, (b) unevaluated hard-constraint contradiction (`hard_constraint_status` hardcoded SATISFIED) | blocking-INDETERMINATE rule → INDETERMINATE (unless FAIL proven); hard-constraint evaluation from receipt facts: `local_required` + observed external resource → VIOLATED → FAIL | R11, R12 |

### Non-load-bearing (addressed where cheap; carried as integration-plan items)

- N3 malformed-packet crash → `sufficiency_disposition()` defensive (REJECT on unreadable) — fixed.
- N4 ESCALATED never demonstrated → C3 control added; FAIL-vs-ESCALATED boundary documented (forbidden-action attempt = FAIL with escalation_ref; ESCALATED is operator-gated, not automatically computed).
- N5 R4 by-construction → label now states ranking is not exercised in v1 (stub).
- N6 schema_version not validated on assignment/receipt/verdict → fixed in all three validators.
### Carried forward (integration-plan items, NOT design blockers — Hermes advisory)

- **Adjudicate-after-validate (advisory):** `adjudicate()` internally calls `validate_receipt()` but derives the verdict from authority/evidence-gap flags only. The runtime pipeline MUST gate on `validate_for_dispatch` / `validate_receipt` validity before adjudicating (validator-first), or make adjudicate fail closed on any validation error. Self-test lifecycle checks already cover this combination; the design artifact is sound.
- N7 mutation-envelope/credential granularity (no receipt-side carrier) — implement at integration.
- FAIL-vs-ESCALATED boundary documented: forbidden-action attempt = FAIL with escalation_ref; ESCALATED is operator-gated, never gate-computed.
- Per-criterion acceptance adjudication remains a Turnstone review step (documented).
- `apply_preferences()` ranking stub — preferences remain inactive in v1.
