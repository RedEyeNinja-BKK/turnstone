# Memory Retirement Plan — Hydration v1

**This document contains NO private memory inventory and NO memory contents.** The exact keep/retire set is generated locally at implementation time (Step 6 of the transaction) and remains local pending operator review. A SHA256 identity of the private candidate set will be recorded here/ledger **only after it is generated** (it has NOT been generated as of this staging gate).

## Native memory lifecycle findings (verified read-only, 2026-08-22)

- Memory index = every visible-scope row rendered as one injected block (`render_memory_index` over `list_visible_memory_index_entries`). There is **no per-entry exclusion/archive flag**.
- Visibility: interactive = global + workstream + user (+ attached project when authorized); coordinator = coordinator-scope (+ attached project).
- Save = atomic **upsert by (name, scope, scope_id)**. Delete = the only removal mechanism. No PATCH/admin-update of memory rows in this build.
- Table columns: `memory_id, name, description, type, scope, scope_id, content, created, updated, last_accessed, access_count`; unique `(name, scope, scope_id)`.
- Admin list endpoint caps at 200 rows with no offset (tooling limitation; full enumeration via the sanctioned memory-tool list pagination).
- Sampled retirement candidates are **description-only summaries** (`content` empty); the authoritative evidence for each closed milestone lives in `<LOCAL_SHARED_WORKSPACE_PATH>/operations/` files/checkpoints.

## Classification rules (evidence-based, applied mechanically, not per-entry adjudication)

**Retire (D-class) when the entry name/description matches:**
- `daily_reflection_*`, `schedule*_*` historical runs (closed dates, superseded by current schedules)
- `docker_vm_*`, `apparmor_*`, `operational_commons_*` closed milestone workstreams
- `switchyard_*` / `routing_*` closed phases (superseded by S2-H baseline)
- `thai_gold_*` / `schedule7_*` closed report dates (each day is a closed event)
- `checkpoint_*` entries pointing to superseded milestones
- `incident_*` resolved incidents (evidence retained in operations/)
- duplicate/renamed records where a canonical successor exists

**Keep (live/current) when the entry represents:**
- Active baseline identities (FGV1 closed record, S2-H cutover record, current phase records)
- Governance/security references, active feedback, current operating rules
- Active project/workstream pointers and current-phase records

## Expected category counts (estimates, NOT quotas — from H1/H2 measurement, confirmed at generation)

- Session-visible index: ≈ 746 entries
- Estimated keep (live/current): ≈ 130–160
- Estimated retire (D-class): ≈ 580–610
- Target injected index: ≈ 10–13K tokens (from ≈ 52–60K)

These ranges are **estimates used for planning, not quotas**. The exact keep/retire set is determined by the classification rules + per-candidate anchor/restorability check (below), then **operator-approved before any deletion** (transaction Step 6–7).

## Per-candidate gate (every retired entry must satisfy one of)

1. **Verified archival retrieval anchor** — a concrete, existing path to the authoritative evidence outside the memory subsystem (e.g. `<LOCAL_SHARED_WORKSPACE_PATH>/operations/<checkpoint|evidence>.md`), verified to resolve at retirement time; or
2. **Fully recoverable from the retirement manifest** — the entry's `content` is non-empty and captured in the manifest, so it can be re-saved semantically/state-equivalently at any time.

A candidate that satisfies **neither** is **kept**, not retired. No candidate is retired on the basis of classification alone.

## Restoration manifest — full schema

JSONL at `<LOCAL_SHARED_WORKSPACE_PATH>/operations/hydration-memory-retirement-manifest-<DATE>.jsonl`:

```jsonc
{
  "memory_id": "<original id>",            // informational/audit
  "name": "<name>",                        // RESTORABLE
  "description": "<description>",          // RESTORABLE
  "content": "<content>",                  // RESTORABLE (empty for summary rows)
  "type": "reference|general|feedback",    // RESTORABLE
  "scope": "project|global|user|workstream",// RESTORABLE
  "scope_id": "<project/user/ws id>",      // RESTORABLE
  "created": "<original created ts>",      // informational (not restorable)
  "provenance": "<checkpoint/evidence path or 'description-only summary'>", // retrieval anchor
  "retired_at": "<retirement ts>",         // informational
  "reason_class": "closed_milestone|superseded_checkpoint|duplicate|historical_report|other"
}
```

## Semantic / state-equivalent restoration guarantees

Sanctioned restore = native memory save (atomic upsert by name+content+description+type+scope+scope_id).

- **Restorable exactly:** `name`, `content`, `description`, `type`, `scope`, `scope_id`.
- **NOT restorable identically:** `memory_id` (regenerated), `created`/`updated` (reset), `last_accessed`/`access_count` (reset).
- Restoration is therefore **semantically/state-equivalent**, not byte-identical. Documented and accepted; rollback claims must not overstate identity.
- Project-scoped restore requires a session attached to that project (sanctioned path).

## Canary design (before bulk retirement)

1. Retire 5–10 low-risk entries (resolved daily-reflection dates).
2. Prove: (a) prefix reduction measured; (b) archived-fact retrieval via manifest → evidence file succeeds; (c) **restore ≥ 1 retired canary** via manifest → native save → verify present + state-equivalent; (d) quality spot-check (cases 1, 4, 6) PASS.

## Batch size / stop conditions

- Bulk retirement in bounded batches ≤ 100 entries.
- After each batch: manifest append (before delete), index count/bytes re-measure, sample retrieval, quality spot-check.
- **Stop condition:** any retrieval failure, quality regression, or manifest/verification mismatch → stop, reconcile, roll back that batch from the manifest before continuing.

## Archive retrieval path (v1, confirmed)

```
retired summary pointer → retirement manifest / checkpoint index → authoritative
operations/ checkpoint or evidence file → native read_file / search
```

Archive-project memory relocation is **parked** (not proven for the sanctioned save path; not a v1 dependency). Hydration goal is archive *retrieval*, not preservation of closed facts inside the memory subsystem.

## Rollback path

- Memory: restore from retirement manifest via native save (semantic/state-equivalent). Evidence files never deleted.
- Non-memory changes: see `implementation-transaction.md` (byte-identical restore).
- No service restarts; no package-core change; full revert leaves the platform byte-identical except memory rows (state-equivalent by design).
