# LC-RD-003G — Self-management MCP: hardening, review, and deployment gates (2026-10-17)

Follows LC-RD-003D (bootstrap), LC-RD-003E (candidate) and the LC-RD-003 security
pass. Architecture is unchanged: **stdio** transport, trusted-administrator
operating model, no broker, no identity separation, no database privilege change.

Baseline commit for all work here: `9f2096e00d9662076e4ecef2c7232f9d2c853f62`
(`ops/turnstone-five-node-v187-20261016`).

## 1. Defects fixed

Each was reproduced with a probe before the fix and re-run after
(`probe_candidate_gaps.py`: 7/16 → 12/16 enforced; the 4 remaining items are
deliberate capability preservation, listed in §5).

| # | Defect | Fix |
|---|---|---|
| A | `POST /v1/api/admin/users/{id}/tokens` returned a plaintext one-time token; redaction cannot protect a route whose purpose is emitting a secret | Refused **explicitly**, message names the Console path. Trailing-slash spelling also refused |
| B1 | Query denylist covered only `reveal`/`include_secrets`/`show_secrets`; `token=`, `access_token=`, `client_secret=` passed | Key policy matched after bounded repeated percent-decoding, case-insensitive |
| B2 | Percent-encoded **path** was decoded for inspection but the still-encoded path was forwarded | Any `%` in the path refused |
| B3 | `;`-separated parameters could hide a sensitive sibling key | `;` in a query refused |
| B4 | **(review)** the patch had dropped the original CR/LF query guard | Restored over decoded keys **and** values |
| C | No predictable audit trail | Receipt on every response with canonical body digest and `self_guard` state |
| D | Self-unload possible | Own-row `PUT`/`PATCH`/`DELETE` refused when the id resolves |
| E | Redaction gaps | `client_id`, `*_ct`, `sk-`/`ghp-`/`xoxb_` shapes |
| F | **(review)** `HTTPError.headers is None` raised `AttributeError` inside the error path | Guarded, typed local |
| G | **`SELF_MCP_SERVER_ID` could not reach the privileged process** — the MCP row's `env` is passed to the spawned process, but the production launcher is `sudo`, whose default `env_reset` drops it, silently leaving the self-unload guard inactive | Id resolved from env → root-owned `self_server_id` file → admin-API self-lookup; guard state reported in every receipt as `active:<id> (<source>)` / `inactive_unresolved`. Launcher wrapper re-exports the id from the file so propagation is deterministic |

## 2. Tests

| Suite | Result |
|---|---|
| Original `tests/test_bridge.py` (11) | **11/11 PASS** against the patched source — no regression |
| `tests/test_bridge_patch.py` (34) | **34/34 PASS** |
| **Total** | **45/45 PASS** |
| Meaningfulness proof | the hardening suite executed against the **pre-patch** `server.py` fails **30** tests/errors, so its assertions genuinely exercise patched behaviour |
| Receipt checker | positive PASS; **refuses** a receipt containing credential-shaped text |

Two notable test-quality properties: the suite is parameterised by
`SELF_MCP_SERVER` so the same assertions can be aimed at either revision, and the
self-id resolution tests cover env, file, api, malformed-id and
**non-root-owned-file-rejected** paths.

## 3. Lint and type checking

`ruff` (repo config, `py312`/`py311` target, line-length 100, `E501` ignored) and
`mypy --strict` were run from an isolated lint virtualenv — the shared
root-owned venv was not modified.

Clean on the files this workstream changed. Pre-existing findings left in place
and recorded rather than silently absorbed:

- `N818` on the public exception name `BridgeRefusal` (renaming is API churn on a reviewed file).
- 4 × `SIM117` in the untouched `tests/test_bridge.py` (present in the baseline).
- 3 mypy strict errors present in the baseline: `NoRedirects.redirect_request` type
  annotation, and the untyped `FastMCP.tool` decorators.

## 4. Independent review

An unprimed reviewer was asked to attack the patch specifically for newly
introduced risks, with the accepted trust model explicitly out of scope. Verdict
**APPROVE WITH EDITS**, with two genuine regressions found (**B4**, **F**) plus
the trailing-slash variant, the semicolon gap, and a digest-canonicality nit.
All were fixed and covered by tests. The reviewer confirmed no overclaim of
unforgeability and no removal of legitimate capability.

## 5. Accepted limitations (do not restate these as guarantees)

1. **MCP approval is dispatch-level.** `auto_approve=false` bounds calls made
   through Turnstone's MCP pipeline. It is not enforced authorization, and the
   privileged launcher is a convenience for a trusted administrative principal.
2. **The service UID is not an isolation boundary.** A process running as that UID
   can invoke the same privileged argv and speak MCP over stdio, and can reach the
   governance tables directly through peer-authenticated PostgreSQL.
3. **Redaction is best-effort** (key names + known token shapes). The control that
   protects credential disclosure is the **route refusal**, not redaction.
4. **No generic route allowlist** by operator decision: full self-management
   capability is preserved. Consequences left open on purpose include
   prompt-policy creation, role/permission overrides, MCP reload, and other-admin
   row deletion.
5. **Audit attribution** names the shared token principal, not the requesting
   workstream.

## 6. Deployment gates (this workstream does not install anything)

1. Run `deploy/run_smoke_root.sh` as root. It hash-verifies the reviewed bytes,
   stages them root-owned under `/run`, performs the real MCP handshake, asserts
   both tools and their input schemas, performs six authenticated reads, and
   validates the receipt for credential disclosure and mutating verbs.
2. Only after a valid receipt: install per `integrations/turnstone-self-api/README.md`
   (root-owned `0755/0644` under `/opt/turnstone-integrations/self-admin/`, exact-argv
   sudoers, `transport=stdio`, `auto_approve=false`, no secrets in `env`).
3. Write the registered `server_id` to `self_server_id` so the self-disable guard
   is active, and confirm a receipt reports `self_guard: active:<id> (file)`.
4. Reload and verify tool discovery on the console and all worker nodes; a node
   that misses the reload fan-out serves a stale catalog until the next reload.
5. One bounded, reversible administrative write with read-back, binding the
   receipt's `request_sha256` to the API audit row.

## 7. Provenance

The candidate was recovered from the public fork
`RedEyeNinja-BKK/turnstone` at `9f2096e0…` after an earlier review concluded the
artifacts were unrecoverable. The cause was traversal permission, not absence:
`/home/vincent` is mode `0700`, so the `turnstone` service UID (102) cannot
traverse it and recursive searches silently skip it. Work on this integration now
happens in an agent-owned checkout to avoid depending on that directory.
