# LocalClaw Hydration v1 — Architecture Review Bundle

**Repository:** RedEyeNinja-BKK/turnstone (fork)
**Branch:** `review/hydration-v1`
**Purpose:** Review-staging only. This bundle is **LocalClaw-deployment-specific review material** and is **NOT proposed upstream product behavior**.

> **REVIEW ONLY — DO NOT MERGE**

---

## Objective

Hydration v1 restructures how context is assembled for the LocalClaw Turnstone agent from a single large unconditional injection into four explicit layers:

```
small kernel → stable-baseline manifest → bounded task working set → explicit archive retrieval
```

The goal is a ~3× reduction in **effective context cost** — defined as *static prefix + automatically retrieved preflight/context tokens before substantive execution* — with **no regression** in architecture correctness, authority boundaries, evidence semantics, factual continuity, retrieval correctness, or fail-closed behavior. Preflight must retrieve **only** context explicitly required by the active task; it is not an excuse to bulk-load archives.

## H1 measured baseline (2026-08-22, read-only investigation)

| Component | Bytes | ~Tokens | Status |
|---|---|---|---|
| Persona base prompt (default interactive) | 23,208 | ~5.8K | reduce to kernel |
| Prompt policies (9 enabled, all globally injected) | ~9,250 | ~2.3K | keep 3 universal, disable 6 domain |
| Default skill `Turnstone Core Identity` | 7,963 | ~2.0K | **retire (Option 3)** |
| Memory index (746 visible entries) | ~210–240K | ~52–60K | retire D-class, keep live |
| Env/tools/session/tool-schemas (native) | ~6–8K | ~6–8K | unchanged |
| **Static prefix total** | **≈ 294K** | **≈ 73K** | **target ≈ 21–24K** |
| **Effective cost (static + preflight/retrieval before substantive work)** | measured at gate | **target ≤ 26–28K** | preflight retrieves only task-required context |

Hermes measured ≈24K static tokens and is **not** a v1 optimization target.

## H2.1 accepted architecture (2026-08-22)

- **Persona = minimum unconditional kernel.** Detailed doctrine lives on-demand in named skills / `working-rules` / references; single-source authority does not imply unconditional injection.
- **Core Identity skill disposition: Option 3 — retire via disable (row retained, NOT deleted; `enabled=false` + `is_default=false`).** Session-start sequence moves to `identity/working-rules.md`; doctrine lives in the persona kernel + Turnstone Management reference.
- **Stable baseline = pointer manifest**, never a copied facts database; no active-task required inputs, no duplicated checkpoint inventories.
- **Memory retirement = semantic/state-equivalent restoration**, with a complete restoration manifest; evidence stays in the native archive path.
- **Preflight enforcement = governance fail-closed contract** (no new callable subsystem; skipping preflight is an acceptance failure).
- **Custom code: none** in v1. All changes are content/governance via native mechanisms.
- Default skill disposition: **Option 3 — retire `Turnstone Core Identity`** (session-start sequence moves to `working-rules.md`).

## Implementation boundaries (unchanged through the review gate)

- No production Hydration mutation has occurred and none is authorized by this PR.
- No package-core change. No Switchyard change. No Fleet Governance reopening.
- Fleet Governance v1 and Switchyard S2-H remain closed baselines.
- PR #7 (`feature/fleet-governance-v1-design`) and Switchyard PR #10 remain untouched, Draft/unmerged.
- No vector DB, memory daemon, new MCP server, new persistence layer, or new callable subsystem.
- No provider/model/Qwen/Comfy/Anthropic qualification; no economics work.
- Sensitive/private runtime state (exact memory inventory, deployment secrets, private addressing) intentionally remains local and is **not** published in this bundle.

## Bundle contents

| File | Purpose |
|---|---|
| `turnstone-kernel.proposed.md` | Exact proposed replacement persona/kernel text + size accounting |
| `core-identity-skill.proposed.md` | Core Identity skill disposition (Option 3 — retire) + session-start preservation |
| `prompt-policy-migration.md` | Per-policy keep/disable → destination → activation → verification |
| `stable-baseline.proposed.md` | Exact proposed pointer-only manifest structure |
| `memory-retirement-plan.md` | Lifecycle findings, classification rules, manifest schema, canary, rollback (no private inventory) |
| `quality-gate.md` | Seven pre/post correctness cases (fresh-session primary) + effective-cost measurement |
| `implementation-transaction.md` | Final staged transaction with action/verify/stop/rollback per step |

---

*LocalClaw architecture review material. Not an upstream contribution. Not authorized for merge. Base `dbdbcf9f…`, head per PR.*
