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

## 7. Deploy-script defects (operator-reported round)

A review of the operator-run deployment tooling found six defect classes in the
shell scripts. All were reproduced, fixed, and re-tested. One further defect was
found **in the fix itself** during verification and is recorded as #7.

| # | Defect | Fix |
|---|---|---|
| 1 | **Wrong validator path** — both scripts invoked `$SRC_DIR/../receipt_check.py`; the file lives at `$SRC_DIR/receipt_check.py` | Corrected in both; a readability guard fails closed with a named message |
| 2 | **`\|\| true` masked failures** — `run_smoke_root.sh` discarded the validator's exit status, and the deploy script read the admin token with `2>/dev/null \|\| true` | Removed; both now refuse explicitly (empty/unreadable token is fatal) |
| 3 | **Gate integrity** — because of (1)+(2) the root smoke could report success on an *invalid* receipt, so the deployment gate consumed a meaningless exit status | The smoke script now exits non-zero on an invalid receipt; the deploy aborts (`exit 3`) **before** any install, sudoers write, or API call |
| 4 | **Assertions printed, never asserted** — per-node verification only pretty-printed, so any node could be disconnected and the script still exited 0 | Replaced with an assertion block: `transport`/`enabled`/`auto_approve` on the row, plus per-node `connected`/`tools`/`has_error` for all six expected node ids; any problem → `SystemExit(1)` → `\|\| exit 8` |
| 5 | **Sudoers matched *any* arguments** — a command spec with no argument list matches any arguments in sudoers, so `sudo …/launch-self-mcp.sh anything` was permitted | Trailing `""` constrains the entry to exactly zero arguments |
| 6 | **`api()` never checked HTTP status** — a non-2xx body was piped straight into the JSON parser, producing silent garbage or an opaque traceback | Every call asserts 2xx and returns non-zero on failure; each call site has an explicit `exit N` |
| — | *(hardening)* `visudo -c` validated the file **after** installing it into `/etc/sudoers.d/` | Validation now runs against the staged temp file **before** installation |
| 7 | **(self-found) the fix for (2)/(3) reintroduced the masking bug** — `check_rc=$?` inside an `if ! cmd; then` branch captures the status of the *negation* (always 0). The script would print `SMOKE RECEIPT INVALID (receipt_check exit 0)` and then **exit 0** | Captured on the command's own line: `"$PY" … receipt_check.py "$OUT" \|\| check_rc=$?` |
| 8 | **(operator-reported) the per-node verifier could never read its input** — `api GET "$SERVERS_URL" \| "$PY" - … <<'PYEOF'` gives the Python program stdin via the heredoc, and a heredoc redirect **overrides the pipe**. `json.load(sys.stdin)` therefore read EOF and raised `JSONDecodeError`, so `\|\| exit 8` aborted the deployment **even on a completely healthy payload**. The bug is invisible to review and to isolated testing of the Python block | Program and payload separated: the program stays on stdin (heredoc) and the API response is delivered on **fd 3** via `3< <(api GET "$SERVERS_URL")`; Python does `json.load(open(3))` with explicit `OSError`/`JSONDecodeError` handling → `SystemExit(1)` → `exit 8`. argv indices unchanged |

Defect 7 is the reason the verification below is empirical rather than a
syntax check: a script that prints "REFUSING TO PASS / RECEIPT INVALID" and then
exits 0 looks correct in review and in `bash -n`.

Defect 8 is the same lesson one level up: it survived review, `bash -n`, **and**
an isolated test of the Python block. The isolated test passed precisely because
it fed JSON over a pipe to a *standalone file*, never exercising the heredoc
invocation the script actually uses. The regression test for it therefore runs
the **real block, extracted verbatim from the script by marker**, so it cannot
drift from what executes.

### Verification evidence

| Check | Result |
|---|---|
| `receipt_check.py` discrimination | good receipt → `0`; credential-leak receipt → `1`; missing file → `2` |
| Per-node assertion block, 6 synthetic payloads | all-good → `0`; disconnected node, absent node, wrong tool count, `auto_approve=true`, no matching row → `1` |
| `run_smoke_root.sh` exit status | `rc=0`+good → `0`; **`rc=0`+leak → `1`**; `rc=5`+good → `5`; `rc=5`+leak → `5` |
| Pre-fix tail on a leak receipt | printed `RECEIPT INVALID` … then **exited 0** (masking proven) |
| **`tests/test_deploy_verifier.sh`** (drives the real extracted block) | **11/11 PASS** — 1 positive (healthy → `0`), 5 unhealthy (→ `8`), 3 fail-closed (empty / non-JSON / unreadable channel → `8`) |
| Same test aimed at the pre-fix script | **fails** — structural guard fires, and behaviorally a healthy payload returned `8` instead of `0` |
| `deploy-self-mcp.sh --dry-run` (non-root) | exit `0`, performs no install, no sudoers write, no API call |
| Python suites | `45/45 PASS`, `55 subtests` — unchanged (no Python file touched this round) |
| Syntax | `bash -n` / `sh -n` clean per declared shebang; no `\|\| true` remains |

Hashes after the fixes:

| File | sha256 |
|---|---|
| `deploy/deploy-self-mcp.sh` | `8340557772381f3c877493a971c106a78c491c2289572104a534428e31874bf9` |
| `deploy/run_smoke_root.sh` | `36ababb2baf06c7bae82ab9980ce121fac4f765678c88fca7e2bbb9d517b3379` |
| `deploy/launch-self-mcp.sh` | `8ba6c83e96c5f6267bec677df780e1505d1b850eb9c7d2db63fa2b3eb7ba9c5a` |
| `deploy/sudoers-turnstone-self-mcp` | `7a344ab8059d13de27ebad6f70ee2d2ae8440b059edb2e4e4c7c0c85f8dfd355` |
| `tests/test_deploy_verifier.sh` | `a9375bf731d69c8f9c186a26886463b9d203268373390e44c8cd11e057359386` |

> **Superseded by LC-RD-003H.** `deploy/deploy-self-mcp.sh` (now
> `455ce932bf2647f5800e3dae8791b7247b8ff335d8f00efb6896c8a9550992df`) and
> `tests/test_deploy_verifier.sh` (now
> `18acb917da49f8710c6b2d0b0ef8ca1b54dcd3aa8b1f59f62dcf9a78e8e76344`) gained a
> bounded convergence window after step 6 was observed to false-fail on nodes
> whose MCP handshake was still in flight. See LC-RD-003H §2–§3.

Nothing in this round changes `server.py`, the MCP row body, or any accepted
limitation in §5 — it makes the gates that protect the deployment actually
enforce what they claim.

## 8. Provenance

The candidate was recovered from the public fork
`RedEyeNinja-BKK/turnstone` at `9f2096e0…` after an earlier review concluded the
artifacts were unrecoverable. The cause was traversal permission, not absence:
`/home/vincent` is mode `0700`, so the `turnstone` service UID (102) cannot
traverse it and recursive searches silently skip it. Work on this integration now
happens in an agent-owned checkout to avoid depending on that directory.
