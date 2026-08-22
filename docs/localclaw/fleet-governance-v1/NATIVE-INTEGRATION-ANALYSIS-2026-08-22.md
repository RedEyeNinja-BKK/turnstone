# Fleet Governance v1 — Native-Turnstone-First Integration Analysis (2026-08-22)

**Status:** DESIGN ANALYSIS (read-only inventory + planning). **NOT runtime integration.**
**Governing rule:** Use native Turnstone mechanisms wherever they already provide the required
lifecycle, storage, validation, assignment, evidence, governance, or control behavior. Custom code
only where a concrete requirement is not already satisfied natively. No fragile hacks, wrappers,
duplicated lifecycle machinery, parallel databases, sidecars, or package modifications.

---

## 0. Part 1 blocker (GitHub publication)

**STOPPED per instruction:** the design artifacts live at
`/home/vincent/shared-workspace/operations/fleet-governance-v1-2026-08-22/`, but the
`operations/` tree is **NOT inside any governed Git repository**:

- `shared-workspace/` root: no `.git`; `git rev-parse` from `operations/` → not a git repo.
- Candidate repos under shared-workspace: `skills/.git` (worktree root = `skills/`, tracks only the
  skills subtree incl. its own `skills/operations/` category folder — NOT the root `operations/`);
  `Operational-Commons/.git` (tracks 0 files under root `operations/`); `config/.git`,
  `trading-skills/upstream/.git` (unrelated subtrees). `/opt/turnstone` is the deployment-wrapper
  repo (master, no remote) and does not own shared-workspace.
- `git ls-files` across candidates: `fleet-governance-v1` matched 0 times.

Per the milestone rule, I did **not** invent a repo, copy artifacts into another repo, create a sync
workaround, or use an unrelated repo. **Blocker for publication: no existing governed repository owns
the `operations/` tree.** Resolution requires a Vincent decision (designate an existing repo as the
governed home for `operations/`, or explicitly authorize initializing one) — see final report.

---

## 1. Fresh read-only inventory of current native Turnstone mechanisms (2026-08-22, live)

| Mechanism | Live evidence | Relevant to |
|---|---|---|
| Workstreams (node-local :8082–:8086 + console route) | fields: `ws_id, name, state (running), kind (interactive), user_id, persistence_state (pending), parent_ws_id, pending_approval, pending_approval_details`; 23 ops incl. `children`, `events`, `history`, `export`, `attachments` (+content), `approve`, `restrict`, `trust`, `cancel`, `close`, `retry`, `rewind`, `open`, `delete` | Lifecycle/provenance; REQUEST storage; evidence; verdict closeout |
| Schedules (native task lifecycle) | 12 live schedules; keys: `task_id, cron_expr, initial_message, model, skill, persona, project_id, auto_approve_tools, target_mode, notify_targets, enabled, next_run, last_run, created_by`; `POST /schedules/{task_id}/run` (run-once, live) | Dispatcher boundary; per-schedule authority/tools; no second dispatcher |
| Skills catalog | 74 skills; fields `name, category, activation, is_default, version, allowed_tools, model, temperature, description`; `load`/`spawn_workstream(skill=…)` | Capability inventory; executor capability mapping; authority via allowed_tools |
| MCP registry | `/v1/api/admin/mcp-servers` → `{"servers":[…]}` (7 servers; dockhand seen; hermes-gateway/openclaw-gateway/openclaw-remote-gateway/proxmox/truenas/turnstone-api) | Executor/capability surfaces; sanctioned agent paths |
| Tool policies | `/v1/api/admin/policies` → `{"policies":[…]}`; e.g. `manual-mcp__openclaw-gateway__openclaw_agent_create` action=manual; allow/deny/ask/manual per `tool_pattern` | Authority envelope (tool-level native enforcement) |
| Prompt policies | 9 policies; fields incl. `tool_gate`, `enabled`, `priority`, `content` | Session governance; advisory policy helpers |
| Roles (RBAC) | 5 roles; fields `permissions, revokes, grants, effective, builtin` | Authority; operator gates |
| Personas | 18 personas; fields `base_prompt, applies_to_kinds, mcp_enabled, memory_enabled, tool_allowlist, is_default` | Executor persona binding; tool allowlist |
| Settings | 78 settings; typed, `restart_required`, per-node | Config surface (not needed for BWP storage) |
| Model definitions | `{"models":[…], "default_alias"}`; aliases carry `capabilities.server_compat.extra_body` → OpenAI `extra_body` → Switchyard extensions (proven S2-H) | Inference-semantics handoff (work_shape/reasoning_intent) — unchanged |
| task_agent | REQUIRED `work_shape` (bounded\|agentic), fail-closed admission, `_prepare_task` rejection, no dispatch without valid shape | Sufficiency-adjacent; structural work-class carrier |
| Audit | `/v1/api/admin/audit` → `{"events","total"}` (records tool.auto_approved, memory, model_definition, workstream/coordinator events; NOT schedule/MCP/policy mutations) | Evidence (partial; mutation ledger closes the gap) |
| Verdicts (native judge surface) | `/v1/api/admin/verdicts` → 100 records; fields `call_id, intent_summary, recommendation, user_decision, risk_level, tier, evidence, judge_model, resolver_principal_id` | Authority adjudication; operator decision record; DO NOT confuse with BWP work_outcome |
| Mutation ledger | hash-chained `operations/mutation-ledger.jsonl` + `integrations/mutation_ledger.py` (record/verify/head) | Evidence/governance record; tamper-evidence for packet/verdict events |
| Self-dependency guard | `integrations/self_dependency_guard.py` + policy doc | Deployment safety (future integration) |
| Working rules / delegation skills | `identity/working-rules.md` executor-selection model; `hermes-delegation` / `openclaw-*` / `three-agent-iteration-loop` skills (delegation envelope minimums, async protocols, read-back verification) | Executor assignment; evidence receipt discipline |
| turnstone-api MCP | 43-tool control surface shared by Hermes/OpenClaw (control-surface propagation 2026-08-16) | Cross-agent evidence/state read-back |

## 2. Required native-mechanism matrix

| FGV1 responsibility | Required behavior | Current native mechanism | Native sufficient? | Exact remaining gap | Smallest custom component | Why custom | Upgrade/stability |
|---|---|---|---|---|---|---|---|
| Lifecycle / provenance | ws identity, state, children, approve/restrict/cancel/close, history/events/export | Native workstreams (all above) | **YES** | packet_id ↔ ws_id linkage convention only | None (convention in skill) | — | Native API (reference-doc-stable); no custom lifecycle |
| REQUEST / BWP storage | BWP lives somewhere native; no new datastore | Workstream attachments (+ content GET/POST); workstream export | **YES** | attach BWP JSON as structured attachment; reference in workstream | None (attachment usage; maybe tiny JSON read/write glue) | Storage already native | Attachments are native; no DB/table |
| Task sufficiency gate | SUFFICIENT→DISPATCH; AMBIGUOUS→CLARIFY; INSUFFICIENT→REJECT; smallest interception BEFORE dispatch | task_agent requires work_shape (fail-closed); workstream approve/pending states; no native sufficiency concept | **PARTIAL** | Sufficiency is a governance decision at AUTHORING time; native has no such gate | Small policy helper: `sufficiency_disposition()` (C-class, ~20 lines) | Native has no sufficiency concept; must not become a second dispatcher — runs at authoring, not dispatch | Policy code, no runtime hook; resilient |
| Executor eligibility / assignment | capability + authority → candidates → Turnstone assignment; NOT work_shape; NOT inference_locality (resource-scoped, FleetRouter only); no parallel router | Skills catalog (74) + MCP registry + roles/personas/policies + working-rules executor-selection model + native delegation (child workstream / MCP agent_run) | **PARTIAL** | No native capability→executor index engine; Turnstone orchestration is the decision | Documented policy mapping in the skill; optional read-only advisory helper; NO precedence-code-as-router | Assignment is a governance decision; native delegation exists and is used; mapping is policy | Avoids hidden router; uses native delegation; skill content is upgrade-safe |
| Inference-semantics handoff | carry work_shape + reasoning_intent into Switchyard adaptive-ingress; no FleetRouter change; no new path; no prose | Alias `capabilities.server_compat.extra_body` → Switchyard extensions (proven); task_agent REQUIRED work_shape; fixed aliases | **YES** | None (BWP metadata_handoff maps onto existing contract) | None (mapping rule in skill) | — | Existing proven path; unchanged |
| Authority envelope | declare/bind native authority; no Python reimplementation | Roles, tool policies (allow/deny/ask/manual), prompt policies (tool_gate), personas (tool_allowlist), schedule auto_approve_tools, native verdicts (risk_level + user_decision), mutation ledger | **YES** (tool-level) | BWP risk_class/allowed/forbidden is a semantic declaration needing MAPPING to native surfaces (policy/allowlist/auto_approve_tools) | Declarative mapping table in skill; packet-consistency checks (overlap, credential_rule) small C-code | Native enforcement exists; mapping is policy | Uses native enforcement; no authz reimplementation |
| Evidence | reuse native history/tasks/attachments/artifacts/ledger/exports; no parallel evidence store | Workstream events/history/export/attachments; schedule runs; MCP run IDs; mutation ledger (hash chain); audit (partial); read-back verification skills | **YES** | Evidence RECEIPT is a governance REPORT format → workstream attachment; evidence_refs → native artifacts | Receipt schema (designed) + tiny formatter | Receipt is the governance contract object, not a store | Attachment-based; no parallel store |
| Verdict / adjudication | preserve epistemology vs outcome; don't duplicate lifecycle states | Workstream state (running/closed/pending) + native verdicts (approval decisions) | **PARTIAL** | BWP work_outcome is a governance decision ON TOP of lifecycle; map outcome→native transition (PASS→close, FAIL→reopen+report, ESCALATED→escalate) | `adjudicate()` policy function (C-class) + VERDICT object as attachment; no new workstream states | Outcome logic (blocking-INDETERMINATE, evidence sufficiency, hard-constraint) is genuinely new governance policy | Small pure function; maps to native transitions |

## 3. Validator decomposition (A / B / C / D)

Function-by-function classification of `validator/validate_bwp.py`:

| Validator responsibility | Class | Decision |
|---|---|---|
| `_enforce_schema_shape()` recursive unknown-key mirror | **A** | REPLACE with the **native Turnstone/Pydantic v2 model idiom** (see §5a finding): `pydantic>=2.0` is already a core dependency, `jsonschema` is NOT a dependency and is unused in Turnstone source. The hand-rolled mirror already drifted from the schemas once (LB-1); never ship a duplicate validator. Keep JSON Schema as interchange/documentation only. |
| JSON load/parse + vocab conformance (capabilities/actions/evidence types) | **A** | JSON Schema `enum`/`const`/`pattern` covers most; thin glue only. |
| `sufficiency_disposition()` / `validate_for_dispatch()` (DISPATCH/CLARIFY/REJECT) | **C** | Keep as small policy helper invoked at authoring time. Not a runtime hook; not a dispatcher. |
| `derive_executor_candidates()` / `select_executor()` + `LANES` table + precedence | **B/C (policy only)** | Do NOT deploy as a router. Authoritative assignment = Turnstone orchestration via native delegation (skills catalog + MCP registry + working-rules). If kept, it is an advisory/audit helper only, explicitly marked non-authoritative. |
| `eligibility()` / `apply_preferences()` | **D** | Specification of FleetRouter doctrine — already implemented in Switchyard (S2-H). Do not deploy in Turnstone. |
| `validate_assignment()` integrity checks (packet/correlation/metadata_handoff) | **C** | Small policy checks (packet consistency); authority itself is native (tool policies/delegation envelope). |
| `validate_receipt()` authority compliance + evidence-sufficiency mapping | **B/C** | Action-level authority is natively enforced; packet-level evidence-sufficiency mapping (PROVEN-only + blocking-INDETERMINATE) is C policy. |
| `adjudicate()` | **C** | Genuinely new governance outcome logic; keep thin, pure, and mapped to native transitions. |
| Prose-warning tripwire (`_scan_string_for_tokens` warnings) | **C (optional)** | Audit signal only; keep only if a deterministic warning is wanted at authoring; never blocking. |
| `build_assignment` / `build_receipt` fixtures, `load_example`, `run_self_test`, `main`, R/C demos | **D** | Qualification/test-only. Do NOT deploy. |

**Conclusion:** the production FGV1 validator can be **substantially smaller** — roughly a thin
Pydantic model layer (native dependency) + a C-class policy module (sufficiency,
authority-consistency, evidence-sufficiency, per-criterion acceptance, adjudicate, formatters) + the
skill content. The bulk of the 59.5 KB artifact (fixtures, demos, recursive mirror, eligibility spec,
LANES table) is A/D and must not be shipped.

## 3a. Native Turnstone validation-model finding (direct-review correction 5, read-only)

Inspection of the Turnstone source at the PR base (fork `main` @ dbdbcf9f) found:

- **`pydantic>=2.0` (+ `pydantic-settings>=2.14.2`) are core dependencies** (`pyproject.toml`);
  `jsonschema` is **not** a dependency and has **0 usages** in `turnstone/`.
- **Pydantic v2 BaseModel is the dominant supported schema idiom**: `turnstone/api/{schemas,
  server_schemas, console_schemas}.py` define request/response models as the single source of truth
  for the generated OpenAPI spec. The module docstring states these models are intentionally NOT used
  for runtime validation in handlers (they drive OpenAPI); runtime handlers use
  `turnstone/core/web_helpers.read_json_or_400()` / `read_multipart_create_or_400()` for minimal
  structural checks, and the SDK uses `response_model.model_validate(body_data)` for typed client
  responses.
- **Workstream attachments are content-addressed blobs** (`turnstone/core/attachments.py`,
  `attachment_buffer.py`) with byte caps + MIME/type classification; there is **no typed JSON-payload
  validation pattern** for arbitrary attachment content today.
- **Extension point:** a BWP runtime object can be validated with a **thin Pydantic v2 model** (native
  dependency, matching the project idiom) hosted on a native workstream attachment, without modifying
  package internals and without adding a new dependency. JSON Schema remains useful as
  interchange/documentation only.
- **Exact native gap:** the BWP *governance policy* layer (sufficiency disposition, per-criterion
  acceptance, evidence-sufficiency mapping, adjudicate) is not provided by native Pydantic models or
  workstream APIs — it remains thin C-class policy code. Nothing else requires a new mechanism.
- **Strictness invariant (direct-review #5000030053):** future runtime Pydantic models MUST preserve
  the schemas' `additionalProperties:false` by declaring `model_config = ConfigDict(extra="forbid")`
  (and evaluating strict typing deliberately). Do not rely on Pydantic BaseModel defaults, which do
  not forbid extras by default. JSON Schema files remain the canonical interchange/documentation
  contract; the Pydantic models mirror them with `extra="forbid"`.

## 4. Executor-selection decomposition

- Current spec (design artifact): static `LANES` capability table + precedence
  (operator gate → capability gap → emission → browser → mutation+risk → smallest path →
  default hermes/openclaw-local/openclaw-remote).
- Native alternatives: skills catalog (74, with allowed_tools/category) = the live capability
  inventory; MCP registry = sanctioned execution surfaces; roles/personas/policies = authority;
  working-rules executor-selection model (intent → capability → fit → availability → authority →
  risk → evidence) = the operational decision process; child workstreams + MCP agent_run = native
  delegation.
- **Decision:** do NOT transplant the handwritten precedence into production. A static fallback
  ordering must not silently become Turnstone's permanent hidden orchestration policy. Executor
  assignment stays a Turnstone governance decision (native orchestration), informed by a documented
  policy mapping (skill) that references the live catalog; any deterministic preference order, if
  ever desired, must be an explicit operator-approved policy, reviewed, not code.

## 5. Proposed runtime integration architecture (native-first)

```text
Turnstone authoring workflow (skill: fleet-governance-v1)
  → author BWP REQUEST (schema v0.1; requirements.inference_locality is RESOURCE-scoped
    only — executor placement derives from capabilities + authority, never this field)
  → store as workstream attachment                        [native: attachments]
  → validate: thin Pydantic v2 model layer (native Turnstone idiom,
    extra="forbid") + C-class policy checks
  → sufficiency gate: SUFFICIENT | CLARIFY | REJECT       [C helper, authoring-time]
        CLARIFY/REJECT → recorded in workstream; NO assignment; NO dispatch
  → Turnstone assignment (native orchestration):
        capability + authority + placement via skills/MCP/working-rules
        (optional read-only advisory helper; never a router)
  → dispatch via native lanes:
        task_agent (work_shape REQUIRED) | hermes MCP | openclaw MCP
        metadata_handoff → alias extra_body → Switchyard adaptive-ingress (UNCHANGED)
  → executor returns evidence
  → EVIDENCE RECEIPT JSON produced as workstream attachment; evidence_refs →
        run IDs, ledger entries, artifact hashes             [native: attachments/ledger]
  → Turnstone adjudication [C]: VERDICT object
        map outcome → native transition (PASS→close, FAIL→reopen+report,
        ESCALATED→escalate)                                 [native: workstream state]
  → append packet/verdict events to mutation ledger         [native: mutation-ledger helper]
```

No daemons, no database/table, no MCP server, no parallel router, no parallel evidence store, no
package-core modification, no monkey-patches, no wrapper layer around native APIs.

## 6. Custom code that would still be required (explicit, minimal)

1. **Pydantic v2 model layer (native dependency)** — thin Pydantic BaseModel classes mirroring the
   four BWP schemas at the runtime boundary (native Turnstone idiom; NO new dependency; JSON Schema
   retained as interchange/documentation).
2. **Policy module (C-class, thin)** — `sufficiency_disposition()`; packet-consistency checks
   (authority overlap, credential_rule, metadata_handoff match); evidence-sufficiency mapping
   (PROVEN-only + blocking-INDETERMINATE); `adjudicate()` (PASS/FAIL/INDETERMINATE/ESCALATED +
   basis); receipt/verdict JSON formatters. Pure functions; no runtime hooks.
3. **`fleet-governance-v1` skill** (content) — authoring procedure, capability→executor mapping
   (policy, referencing live catalog), mapping tables, guardrails.
4. **Schema files + vocabularies** — already designed (the canonical contract; owned in the
   governed repo once Part 1 blocker resolves).

(Optional, advisory-only, NOT recommended for runtime: `derive_executor_candidates()` as a read-only
consistency helper; `adjudicate()` should gate on receipt validity before adjudicating — Hermes advisory.)

## 7. Validator code that should NOT be deployed

- `run_self_test()`, `main()`, `load_example()`, `build_assignment()`, `build_receipt()`, all R/C
  demonstration logic (D).
- `_enforce_schema_shape()` recursive mirror (A — replaced by the native Pydantic v2 model idiom;
  JSON Schema retained as interchange/documentation only).
- `eligibility()` / `apply_preferences()` (D — FleetRouter specification; already in Switchyard).
- `LANES` table + `select_executor()` precedence as runtime router (B/C — policy document only).
- `_scan_subtree_for_identity_keys` belt-and-suspenders (A-replaced; drop once standard validation
  is in place).
- Prose-warning tripwire: keep only as optional authoring-time warning, never blocking.

## 8. Upgrade-survivability assessment

- **Supported/native interfaces (resilient):** workstreams, schedules, skills, roles, personas,
  tool/prompt policies, MCP registry, model definitions (alias extra_body), settings, audit,
  verdicts — all documented native API surfaces in `turnstone-api-reference.md`; no reliance on
  internal function signatures.
- **Inference handoff:** the alias `capabilities.server_compat.extra_body` → Switchyard extensions
  path is proven (S2-H) and stable; reuse it, never add a second inference path.
- **Fragile couplings to avoid:** (a) monkey-patching or modifying the Turnstone package core;
  (b) wrapping native APIs merely to avoid understanding them; (c) embedding the BWP in schedule
  `initial_message` (4096-char silent truncation — store as attachment instead); (d) the hand-rolled
  recursive schema mirror (drifts from schemas — already happened once); (e) hidden precedence
  ordering as permanent policy.
- **Ledger:** use the existing `mutation_ledger.py` helper for tamper-evident governance records;
  do not duplicate a second audit trail.
- **Dependency surface:** policy module uses stdlib + native `pydantic` (already a Turnstone core
  dependency); NO new runtime dependency (`jsonschema` NOT added); no new daemons/DB/MCP.

## 9. Recommendation

The native-first integration analysis finds **no architectural blocker** to a thin, native-first
runtime integration (JSON Schema + small C-class policy module + skill content, all riding native
workstreams/attachments/ledger/delegation and the existing Switchyard handoff).

However, **runtime integration GO is NOT given** and should not be given yet:
1. **Part 1 blocker stands** — no governed Git repository owns `operations/`; Vincent must decide
   the governed home for the design artifacts (designate an existing repo or explicitly authorize
   initializing one) before publication.
2. Runtime integration requires a **separate Vincent authorization** (not issued by this milestone).

Recommendation: resolve the repository ownership decision first; then, upon a separate GO, proceed
with the native-first integration above. Do NOT begin runtime integration now.
