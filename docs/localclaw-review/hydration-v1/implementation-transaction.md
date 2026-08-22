# Implementation Transaction — Hydration v1 (Final Staged Order)

Every mutation is content/governance via native mechanisms; **no custom code, no package-core change, no new subsystem**. Each step: `action → expected effect → verification → stop condition → rollback`. Steps execute sequentially; each must be verified and reversible before the next.

| Step | Action | Expected effect | Verification | Stop condition | Rollback |
|---|---|---|---|---|---|
| 1 | Verify baseline evidence + rollback inputs | Quality baseline file exists; manifest schema ready; memory index fully enumerated read-only; pre-change prefix measured | Battery file present; enumeration counts match H1; prefix measurement recorded | Missing baseline/rollback input → do not proceed | n/a (read-only) |
| 2 | Create stable-baseline manifest (`<LOCAL_SHARED_WORKSPACE_PATH>/operations/stable-baseline.md`) | Pointer layer exists; no duplicated state | File present; each entry has source→pointer→freshness→retrieval; pointer resolves | Pointer resolution fails → fix manifest before proceeding | Delete file |
| 3 | Reduce global prompt-policy injection (disable 6 domain policies; keep 3 universal) | 9 → 3 globally injected policies | Fresh-session prompt dump shows only 3; each domain persona still instructs correctly; battery cases 1–3 PASS | Domain behavior missing after disable → re-enable that policy | Re-enable disabled policies (admin API, byte-identical) |
| 4 | Core Identity skill → `activation=named`, reduced content (Option 2) | No Core Identity injection; on-demand reference remains | Fresh-session prompt has no Core Identity content; `skills load` works; battery cases 1–5 PASS | Kernel doctrine lost → restore prior content | Restore prior content + `activation=default` |
| 5 | Trim persona to minimum kernel (per `turnstone-kernel.proposed.md`) | Persona ≈ 23.2KB → ≈ 13KB; kernel complete | Persona bytes measured; kernel checklist complete; battery cases 1–5 PASS | Any kernel element missing → restore prior persona | Restore prior persona content (byte-identical) |
| 6 | Generate + freeze private memory restoration manifest + exact keep/retire set (read-only) | Full inventory classified; manifest complete; SHA256 of private candidate set recorded | Manifest validates; counts ≈ expected (keep ~130–160 / retire ~580–610); SHA recorded | Classification gaps → refine rules before review | n/a (read-only; manifest is additive) |
| 7 | Operator review of exact keep/retire set | Vincent approves exact set before any deletion | Written GO/approval recorded | No GO → no deletion | n/a |
| 8 | Memory-retirement canary (5–10 low-risk entries) | Small, reversible retirement | Index prefix reduction measured; archived-fact retrieval via manifest→evidence succeeds | Retrieval fails → restore canary, stop | Restore canary from manifest (semantic/state-equivalent) |
| 9 | Prove prefix reduction | Measured reduction ≈ expected per batch | Index bytes/tokens re-measured | No reduction / wrong reduction → stop | n/a (measurement) |
| 10 | Prove archive retrieval | Retired fact retrievable via native path | Retrieval probe succeeds for ≥1 retired canary | Retrieval fails → restore canary, stop | Restore canary |
| 11 | Restore ≥1 retired canary; verify semantic equivalence | Restoration works; state-equivalent | Re-saved entry present with same name/content/description/type/scope/scope_id | Restore fails → stop, investigate manifest schema | n/a (this IS the rollback proof) |
| 12 | Quality spot-check | No architecture/authority/evidence regression | Battery cases 1, 4, 6 PASS | Any regression → restore canary, stop | Restore canary |
| 13 | Bounded bulk retirement (batches ≤100) | Retire approved set incrementally | Manifest appended per batch (before delete); index re-measured; sample retrieval; spot-check | Any mismatch → stop, reconcile, roll back batch | Restore batch from manifest |
| 14 | Verification after every batch | Every batch independently verified | Same verification as step 13 | Any failure → stop | Restore failed batch |
| 15 | Full quality battery (7 cases) | No regression post-retirement | All 7 PASS (with retry handling) | Any FAIL → roll back offending step | Per-step rollback |
| 16 | Final token/context measurement | Static prefix ≈ 21–24K (from ≈ 73K) | Measured prefix matches target; report table | Target not met → report, do not force | n/a (measurement) |
| 17 | Reconciliation / closure | Manifest hash recorded; ledger entries; report saved | Chain verifies; evidence files present; quality baseline updated | Reconciliation mismatch → resolve before closure | n/a |

## Global boundaries (every step)

- **No production mutation before operator GO** on the exact approved transaction and, at step 7, on the exact keep/retire set.
- No package-core, vector DB, memory daemon, new MCP server, new persistence layer, new callable subsystem, Switchyard change, FGV1 reopen, provider/model/Qwen/Comfy/Anthropic qualification, or economics work.
- Fleet Governance v1 and Switchyard S2-H remain closed baselines; PR #7 and Switchyard PR #10 remain untouched.
- No service restarts required for any step; full revert leaves the platform byte-identical except memory rows (semantic/state-equivalent restoration by design).
