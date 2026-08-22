# Stable Baseline Manifest — Proposed Structure

**File (proposed, production path):** `<LOCAL_SHARED_WORKSPACE_PATH>/operations/stable-baseline.md`

**Design rule:** pointer-only. The manifest **never copies mutable operational state** whose authority lives elsewhere. It does **not** enumerate memory populations or live inventories; it points to the authoritative native mechanism/query or to an immutable/current pointer artifact.

For every entry the manifest carries exactly:

```
authoritative source → pointer/identity → freshness rule → retrieval mechanism
```

## Proposed manifest outline

```markdown
# Stable Baseline Manifest — LocalClaw Turnstone

Purpose: one pointer layer for current authoritative context. No duplicated state.
Freshness: verify each pointer at the freshness rule stated; re-verify on first use of any baseline.

## 1. Deployment identity
- authoritative source: deployment facts file
- pointer/identity: <LOCAL_DEPLOYMENT_FACTS_PATH>
- freshness: verify at session start; updated only on deployment/recovery
- retrieval: read_file <LOCAL_DEPLOYMENT_FACTS_PATH>

## 2. Closed-baseline identities
- authoritative source: checkpoint files in <LOCAL_SHARED_WORKSPACE_PATH>/operations/
- pointer/identity: per-closed-milestone checkpoint path + closed status + head SHA
  (current at time of writing: Fleet Governance v1 — checkpoint-fleet-governance-v1-closed-<DATE>.md;
   Switchyard S2-H — checkpoint-switchyard-s2h-cutover-deployed-<DATE>.md; later milestones append)
- freshness: re-verify on first use of that baseline (file exists; SHA matches)
- retrieval: read_file <checkpoint path>

## 3. Current project / workstream pointers
- authoritative source: native project + workstream surfaces
- pointer/identity: <PROJECT_ID> + active workstream handles
- freshness: live at session start (native API)
- retrieval: native API (/v1/api/projects, /v1/api/workstreams) — do not copy into this file

## 4. Routing / authority policy
- authoritative source: deployment facts file, routing sections + S2-H closure record
- pointer/identity: section reference (do NOT copy routes/hashes here)
- freshness: as deployment facts
- retrieval: read_file <LOCAL_DEPLOYMENT_FACTS_PATH> (routing sections)

## 5. Current vs superseded checkpoints
- authoritative source: <LOCAL_SHARED_WORKSPACE_PATH>/operations/checkpoint-*.md
- pointer/identity: ordered index of current → superseded checkpoints (one line each: status, path, date)
- freshness: update when a checkpoint is created or closed
- retrieval: read_file each listed checkpoint

## 6. Required inputs for the current milestone
- authoritative source: this manifest (operator/planning)
- pointer/identity: required file paths for the active milestone
- freshness: per milestone
- retrieval: preflight step 0 (hydration protocol) reads this section and verifies each path

## 7. Archive / retrieval locations
- authoritative source: <LOCAL_SHARED_WORKSPACE_PATH>/operations/ + archives/
- pointer/identity: directory paths + retirement manifest path (immutable)
- freshness: static pointers; content changes do not alter the pointers
- retrieval: read_file / search / retirement manifest lookup
```

## Explicitly excluded (to avoid a drift-prone facts database in prose)

- Live memory population / active-memory name lists (→ native memory surface, live query).
- Model/routes hashes, MCP tool counts, schedule states, panel counts (→ deployment facts / native APIs).
- Full checkpoint contents (→ the checkpoint files themselves).
- Any mutable operational value whose authority lives elsewhere.
