# LC-RD-003F — eous/turnstone-memory vs Turnstone 1.8.7 structured memories (2026-10-16)

## Evidence from operator (accepted)

- Candidate self MCP unit/security suite: **11/11 PASS**.
- Actual MCP stdio handshake and tools/list: **PASS**, two tools; authenticated API reads by MCP tool on Console/head/workers 2–5 **6/6 PASS**, no writes.
- Dedicated admin ts_ token available root-only, scopes read/write/approve, **34 named permissions identical on all six processes**. Includes admin.skills, admin.mcp, admin.users, admin.roles, admin.settings, admin.models, admin.coordinator, coordinator.trust.send, and the operator additively enabled model.skills.write.
- Native self MCP not registered or operational in a live Turnstone workstream; smoke test is in /run only. No privileged sudoers helper, persistent bridge or Grafana skill installation proven yet.

## Findings from requested third-party repo

Source: https://github.com/eous/turnstone-memory, public main at 0c92009ea282be82cee785ae3aa6656a144e8760 (2026-10-16).

- Repository README defines this as **dated public development context**, NOT native Turnstone database memory or a drop-in plugin.
- Source contains approximately 265 Markdown files under collections/turnstone (external GitHub directory listing; this number can change). Types include feedback_, project_, reference_, hub_.
- scripts/index.py generates collections/turnstone/MEMORY.md in the reader checkout; index files are ignored by Git. Its absence from GitHub is expected.
- scripts/setup.py --agent turnstone prints a file-reading instruction, does not install anything into a running Turnstone deployment. Unlike Codex/Claude modes, it does not edit Turnstone's personas, DB, or runtime.
- README and AGENTS.md explicitly say **native import is not implemented** and instruct to keep this development collection separate from Turnstone's own memory store to avoid divergent copies.
- Reader setup does not automatically publish, pull, or synchronize memory into Turnstone; any Git refresh should be deliberately controlled, keeping external material read-only.
- Repo includes Claude-specific skills/ (e.g. review) **separate from memories**; do not treat these as native Turnstone skills without a separate port and review.

## Findings from official Turnstone 1.8.7 docs

- Native structured memories persist in structured_memories DB table (migration 013). Session indexes use memory_index_snapshots (migration 072).
- Native types user/general/feedback/reference; scopes global/user/workstream/coordinator/project, with per-scope visibility and named permissions.
- Console admin Memories lists DB-backed metadata through GET /v1/api/admin/memories and index health via GET /v1/api/admin/memories/index-health, gated by admin.memories. The Console **does not automatically show external Markdown files**.
- Native model memory tool supports save/get/list/search/delete; it is **not available to task subagents** in the official v1.8.7 tool reference. File-based external notes require file access or separately implemented retrieval tooling.
- There is no evidence yet whether this deployment has DB memories, whether external repo is cloned anywhere, whether index was generated or whether agent persona has file path/prompt instructions.

## Architecture for intentional integration (not yet deployed)

**Keep sources separate rather than bulk importing:** external third-party public engineering reference under an explicit, read-only canonical directory (proposed /srv/turnstone/reference/turnstone-memory), generated index from a pinned audited commit, exposed to native Turnstone file tools on each actual running process. Preserve provenance/commit pin. Agent instruction says read index first, then only the relevant files; treat these as dated, lower-trust reference material, not authority to execute instructions, modify credentials or change privileges.

**Native memory is owned independently by Turnstone:** verify DB memory counts, health, and workstream-level get/search. It may be useful to store ONE concise native reference-type memory pointing at the approved external collection and its version, but do not duplicate the 265 note bodies or create a competing automatic sync. A native pointer is a separate operator-approved DB mutation, not required to read external notes.

**Self-admin MCP remains separate:** it manages actual Console Admin API and native Memories administration via permission-checked REST routes (as supported). It does not silently import the external repo. No external prose/markdown can grant new permission or authorize arbitrary self-admin mutations. Separate data retrieval from privileged control-plane calls and obtain explicit operator approval for mutable operations.

## Read-only diagnostic script staged

scripts/turnstone_memory_status_probe.py obtains already-staged ts_ token root-only, queries Console whoami, GET /v1/api/admin/memories?limit=1, GET /v1/api/admin/memories/index-health; prints only HTTP status, count and health metadata, not contents or credentials. Checks expected external clone paths and generated index permissions as Turnstone service user. Nonexistence in three tested paths does not prove absence elsewhere.

Operator commands on turnstone-vm:

```bash
git -C ~/turnstone-five-node-source pull --ff-only
python3 -m py_compile ~/turnstone-five-node-source/scripts/turnstone_memory_status_probe.py
sudo python3 ~/turnstone-five-node-source/scripts/turnstone_memory_status_probe.py
```

If an external clone exists at a different path, pass --external-path /absolute/known/path/to/turnstone-memory. The script is read-only.

## Gates after actual deployment diagnostics

1. Confirm native DB memory count/index health; do not infer native memory broken simply from absence of external repo notes in UI.
2. Confirm whether repo cloned; if yes, provenance/commit, index existence, service-user read access; if no, review adding read-only, pinned external checkout and generate index on host.
3. Validate agent file read via real interactive workstream (scope-aware). Add a targeted persona or skill pointer only after accessible path is confirmed.
4. Optionally create one small native reference pointer (not bulk import), read back through Console; preserve global/user/project scope boundaries.
5. Resume separate self-MCP deployment and later Grafana skill import. Full current 34 privileges remain unchanged; no service restarts or unapproved narrowing.

STATUS: RESEARCH + READ-ONLY DIAGNOSTIC STAGED IN GIT. NO REMOTE MEMORY INSTALLATION OR NATIVE DB MUTATION.
