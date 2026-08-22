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
- pointer/identity: **pointer/query, not an inventory** — `search(glob=<LOCAL_SHARED_WORKSPACE_PATH>/operations/checkpoint-*.md)` plus the current-baseline pointers already listed in §2. Superseded checkpoints are identified by status line in each file (e.g. "CLOSED"), not by a maintained list here.
- freshness: re-query at first use; do not maintain a duplicated index
- retrieval: native search/glob over the checkpoint directory

## 6. Required inputs for the active task
- authoritative source: **task working set** (workstream attachments / task brief), NOT this manifest
- pointer/identity: the active workstream's attachments and task brief
- freshness: per task
- retrieval: preflight step 0 (hydration protocol) resolves required inputs from the **task working set** — the manifest does not hold them

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
- Active-milestone required inputs (→ the task working set; the manifest does not hold them).
- Any mutable operational value whose authority lives elsewhere.
