# Fleet Governance v1 — Validation Report (corrective pass 2026-08-22)

**Date:** 2026-08-22. **Type:** structural + rejection/control validation of design artifacts (local, read-only).
**Validator:** `validator/validate_bwp.py` (stdlib, deterministic). **Result: ALL PASS (exit 0).**
**Direct review:** ChatGPT GitHub review #4999999331 on Draft PR #7 (authoritative steering).
**Corrections incorporated (2026-08-22 corrective pass):**

1. **Executor placement separated from inference-resource locality** — `requirements.inference_locality`
   is resource-scoped only (`local_required | hosted_allowed | any`); it NEVER selects the executor.
   Executor placement derives from capabilities + authority + sanctioned surfaces.
2. **Observed-locality hard-constraint adjudication** — RECEIPT
   `resource_observed.inference_resource.locality` (`local | hosted | unknown`) from authoritative
   telemetry; provider/model names never establish locality; `local_required`+PROVEN hosted → FAIL;
   +PROVEN local → compliant; +unknown → INDETERMINATE (never inferred FAIL).
3. **Per-criterion acceptance adjudication** — every criterion adjudicated
   `MET | NOT_MET | INDETERMINATE` from positive PROVEN `criterion_refs`; absence of FAILED claims
   alone never makes a criterion MET; executor self-assessment never authoritative.
4. **Nullable nested schema strictness** — `_enforce_schema_shape` handles union types
   (`["object","null"]`); unknown properties under nullable `resource_observed` and nested
   `inference_resource` are rejected; valid `null` accepted. Runtime still retires the mirror in favor
   of the native Turnstone/Pydantic idiom.
5. **Native Turnstone validation first** — inspected Turnstone source: `pydantic>=2.0` is a core
   dependency; `jsonschema` is NOT a dependency and unused. Runtime BWP objects should use the native
   Pydantic v2 idiom (JSON Schema as interchange/documentation only); no new dependency.

## 1. Artifact structural integrity

All 4 BWP schemas + vocabularies + 5 qualification examples + validator report parse clean;
`validate_bwp.py` `py_compile` clean; validator runs from the artifact tree exit 0.

## 2. Qualification examples (full lifecycle demonstration)

| Trial | work_shape × reasoning | inference_locality | Derived executor | Dispatch | Verdict | Result |
|---|---|---|---|---|---|---|
| T1 bounded routine | bounded × none | local_required | turnstone-native | allowed | PASS | ✅ |
| T2 agentic non-deliberate | agentic × none | any | hermes | allowed | PASS | ✅ |
| T3 deliberate/reasoning | agentic × deliberate | any | hermes | allowed | PASS | ✅ |
| T5 real-estate workload | bounded × none | local_required | hermes (document_parsing) | allowed | PASS | ✅ |

**T4 (insufficient/ambiguous)** structurally valid but **never dispatches**: disposition **CLARIFY**.

## 3. Rejection demonstrations (must fail closed) — ALL PASS

| ID | Proof |
|---|---|
| R1 | Ambiguous work must not dispatch (CLARIFY) |
| R2 / R2b / R8 | Resource/model identity as STRUCTURAL requirement rejected (`htpc-llm`, `resource=HTPC`, `model=DeepSeek`) |
| R3 / R7b | Missing structural `work_shape` / `reasoning_intent` rejected (prose cannot substitute) |
| R4 | Preference never rescues an ineligible option |
| R5 | Executor broadening authority → FAIL |
| R6 | PASS without required evidence → INDETERMINATE |
| R9 | ASSIGNMENT carrying provider/model identity rejected |
| R10 / R10b–e | Synonym-key identity smuggling rejected (additionalProperties:false, incl. nested + receipt top-level) |
| R11 | Blocking INDETERMINATE claim → INDETERMINATE |
| R12 | `local_required` + PROVEN hosted locality → FAIL (VIOLATED) |
| R13 | `local_required` + unknown locality → INDETERMINATE (never inferred FAIL) |
| R14 | Provider/model presence alone cannot prove locality → INDETERMINATE |
| R15 | One NOT_MET acceptance criterion → FAIL |
| R16 | A required criterion without positive PROVEN adjudication → INDETERMINATE |
| R17 | Absence of FAILED claims alone cannot make criteria MET |
| R18 | Executor self-assessment cannot force PASS |
| R19 | Unknown property nested under nullable `resource_observed` rejected |
| R20 | Unknown property under nested `inference_resource` rejected |

## 4. Positive controls (must remain valid / shape-independent) — ALL PASS

| ID | Proof |
|---|---|
| R7 (corrected) | Prose mention of identities is warning-only; packet stays valid and dispatches |
| C1 | Executor selection follows capability/authority, not work_shape |
| C2 | Sufficiency disposition deterministic (DISPATCH/CLARIFY/REJECT) |
| C3 | ESCALATED outcome reachable (operator-gated), NOT_EVALUATED |
| C4 | **inference_locality does NOT select the executor** (local_required vs hosted_allowed → same executor) |
| C5 | Executor assignment never carries/rewrites inference locality |
| C6 | local_required + PROVEN local resource compliant (PASS) |
| C7 | Local provider/model with PROVEN local locality compliant (names never prove locality) |
| C8 | PASS requires all criteria MET + evidence sufficient |
| C9 | Valid nullable `resource_observed=null` accepted |

## 5. Provenance separation checks

- **REQUEST** carries requirements only; structural identity keys rejected; capabilities
  vocabulary-enforced; prose mention warning-only.
- **ASSIGNMENT** carries executor + derivation only; provider/model identity rejected; no locality fields.
- **RECEIPT** is the only object carrying observed resource identity AND observed locality
  (`resource_observed.inference_resource.locality`), explicitly observed-fact-only.
- **VERDICT** separates `work_outcome` from `basis.evidence_epistemology`; acceptance is
  per-criterion with `MET/NOT_MET/INDETERMINATE` + evidence_refs + rationale.

## 6. Notes / limitations (honest)

- The prose scan is a deterministic **warning tripwire**, not a semantic classifier (audit signal only).
- `derive_executor_candidates()` / `select_executor()` are **specification** functions, not runtime
  code; C1/C4/C5 prove they do not follow work_shape or inference_locality.
- The recursive schema mirror is a **design artifact**; the runtime design retires it in favor of the
  native Turnstone/Pydantic v2 idiom (see NATIVE-INTEGRATION-ANALYSIS §3a). Enums/consts remain schema
  declarations (not all mirrored in the shape checker).
- Sufficiency disposition (CLARIFY/REJECT) is minimal; escalation is operator-gated.
- Hermes advisory carried to integration plan: adjudicate only after `validate_receipt` passes (or
  fail closed on any validation error).
