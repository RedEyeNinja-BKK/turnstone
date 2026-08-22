# Turnstone Core Identity Skill — Proposed Disposition (Revised per PR #8 Review)

## Disposition

| Property | Current | Proposed |
|---|---|---|
| `activation` | `default` (injected every session) | **retired** (Option 3) |
| `is_default` | `true` | removed |
| Content size | 7,963 bytes (~2.0K tokens) | **0 tokens injected** |
| Availability of doctrine | automatic | kernel (persona) + existing references |
| Skill row | present in catalog | disabled/removed via native skills surface (reversible) |

**Change mechanism:** native skills update (disable) / admin skill removal per supported API. Rollback: restore prior content + `activation=default` (byte-identical).

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
2. Disable/remove the `Turnstone Core Identity` skill via the native skills surface.
3. Verify: fresh-session prompt contains **no** Core Identity content; kernel (persona) doctrine intact; `working-rules.md` carries the session-start sequence; quality-gate cases 1–5 PASS.

Rollback: restore prior skill content + `activation=default` (byte-identical); remove the added working-rules section if reverting.
