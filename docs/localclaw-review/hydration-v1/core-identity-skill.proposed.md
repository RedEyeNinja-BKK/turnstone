# Turnstone Core Identity Skill — Proposed Disposition (Revised per PR #8 Review)

## Disposition

| Property | Current | Proposed |
|---|---|---|
| `activation` | `default` (injected every session) | **retired (Option 3) — disabled, NOT deleted** |
| `is_default` | `true` | `false` |
| `enabled` | `true` | `false` |
| Content size | 7,963 bytes (~2.0K tokens) | **0 tokens injected** |
| Availability of doctrine | automatic | kernel (persona) + existing references |
| Skill row | present in catalog | **retained in storage** (hidden from `find` unless `enabled_only=false`; rejected by `load`/`spawn_workstream`) |

**Exact mechanism (per review #5000250206):** disable/deactivate **without deleting the row** — the native skills surface supports flipping the `enabled` flag (`disable`), and the admin skill update supports `is_default=false` (PUT `/v1/api/admin/skills/{skill_id}`). A disabled skill stays in storage, is hidden from `find` (unless `enabled_only=false`), and is rejected by `load` / `spawn_workstream(skill=…)`. **Deletion is NOT used in v1.**

**Rollback (byte-identical):** re-enable the retained row via the native skills `enable` — the skill row and its content were never deleted, so restore is **byte-identical** (no content change). If a later phase ever deletes the row (not planned), rollback would be **semantic/state-equivalent** with **non-restorable metadata**: `skill_id`, `created`/`updated` timestamps, and version/approval history.

## Rationale (PR #8 review finding 4 — adopted)

The unconditional **kernel** now lives entirely in the persona (`turnstone-kernel.proposed.md`). Review directed **Option 3 — retire the skill** unless genuinely unique value can be demonstrated that is not better housed in `working-rules.md` or Turnstone Management.

**Assessment after re-examination:** the previous Option-2 justification (session-start sequence, source-of-truth hierarchy, capability planes, MCP decision tree, reporting skeleton) is **not** sufficiently unique:

- Session-start sequence → belongs in **`identity/working-rules.md`** (already the executor-selection/routing reference) — add the read-first list there.
- Source-of-truth hierarchy → already covered by the persona kernel + `working-rules.md` (live introspection > facts > reference).
- Capability planes → the kernel already states capability ≠ authority and the sanctioned-path doctrine; detailed planes belong in Turnstone Management (named reference) if needed.
- MCP decision tree → the kernel's sanctioned-path rule + `working-rules.md` lane table cover the essential routing; details live in Turnstone Management.
- Reporting skeleton → already a native/management concern; retain in Turnstone Management reference.

**Conclusion: no genuinely unique value survives that is not better housed in `working-rules.md` / Turnstone Management. Retire the skill.** Unique session-start guidance is preserved by adding the read-first list to `identity/working-rules.md` (a one-line addition at implementation time, not part of the persona kernel).

## Exact implementation

1. At implementation time: append a short "Session Start Sequence" section to `identity/working-rules.md` (the read-first list: deployment facts → working-rules → API reference → schedule/governance READMEs → named skills as needed).
2. **Disable** the `Turnstone Core Identity` skill via the native skills surface: set `enabled=false` and `is_default=false` (admin skill update). The row is retained; nothing is deleted.
3. Verify: fresh-session prompt contains **no** Core Identity content; kernel (persona) doctrine intact; `working-rules.md` carries the session-start sequence; quality-gate cases 1–5 PASS.

Rollback: **re-enable the retained skill row** (native `enable`; byte-identical, no content restore needed); remove the added working-rules section if reverting. No deletion is performed in v1, so no semantic-restore path is required for the skill.
