# LC-RD-003H — Self-management MCP: production convergence and acceptance (2026-10-17)

Follows LC-RD-003G (hardening, review, deployment gates). Architecture is
unchanged: **stdio** transport, trusted-administrator operating model, no broker,
no identity separation, no database privilege change.

Deployed from published commit `469377605c4b9ee19572f1d98da7190cf5a1fc21`.

## 1. Deployment state: installed and converged

The operator-run smoke passed (hashes matched, handshake + tool schemas + six
authenticated reads, receipt valid, exit 0). Installation completed through
registration and reload: root-owned bridge files, the exact-argv sudoers entry,
and `self_server_id` are in place at 02:11.

Registered row (`mcp_servers`), verified read-only from PostgreSQL:

| Field | Value |
|---|---|
| name | `turnstone-self-api` |
| server_id | `e8bb5e5945db40748ec209d7da6ca337` |
| transport | `stdio` |
| command / args | `/usr/bin/sudo` `["-n", "/opt/turnstone-integrations/self-admin/launch-self-mcp.sh"]` |
| enabled | true |
| auto_approve | false |
| env | `{}` (no secrets) |

All six MCP endpoints are connected. Per-node gauges sampled read-only at
02:20:57 (see §4 for the method):

| Endpoint | `turnstone_mcp_servers` | `turnstone_mcp_server_errors` | `turnstone_errors_total` |
|---|---|---|---|
| console (pseudo-node) | connected at step 6, 2 tools | — | — |
| head-1 | 1 | 0 | 0 |
| node-2 | 1 | 0 | 0 |
| node-3 | 1 | 0 | 0 |
| node-4 | 1 | 0 | 0 |
| node-5 | 1 | 0 | 0 |

## 2. The step-6 failure was an acceptance-script race, not a deployment fault

**Symptom.** Deployment acceptance exited 8 at step 6 with console, node-4 and
node-5 connected with 2 tools, and head-1, node-2, node-3 reporting
`connected=false, tools=0`.

**Cause.** `POST /reload` returns when *reconcile* finishes, not when a *connect*
completes, and step 6 sampled exactly once.

The chain, from source:

1. Each node's `reconcile_sync` reports a server in `added` **only** if
   `add_server_sync(...)` reports `connected` (`core/mcp_client.py`). It is not a
   schedule-now/connect-later list.
2. But the console's fan-out to each node is bounded at 30 s
   (`_notify_nodes_mcp_reload`, `console/server.py`) while a connect is bounded at
   `_CONNECT_TIMEOUT = 30` per phase. A node can therefore reply "no delta"
   while its connect is still in flight.
3. Crucially, an **empty** `added/removed/updated` list means *only* "no config
   delta" — the row was already registered, so reconcile had nothing to add. It
   says nothing about connection state in either direction.
4. Step 6 asserted once, immediately after reload. The three nodes whose
   handshakes had not yet completed were reported as `connected=false,
   tools=0, has_error=false` — the signature of an **in-flight** connect, not a
   failure. There is no retry in the script, so it exited 8.

**Evidence that nothing was actually broken.** The six bridge processes were
spawned by the reload fan-out at 02:11:16 and were **still the same PIDs at
02:20:57** (580 s old) — no teardown, no respawn, therefore **no reconnect
occurred**. The connections completed on the original processes. Every node now
reports one connected server with zero errors, and `turnstone_errors_total` is 0
across the fleet.

**Corroborating latency evidence.** Each node recorded exactly 2 `POST
/v1/api/_internal/mcp-reload` calls averaging ~0.31 s (sum 0.616 s over 2), i.e.
the reload endpoint returns long before a `sudo` exec + interpreter start + stdio
`initialize` round-trip can finish. Recovery after a failed connect is handled by
the health loop with capped exponential + full jitter backoff
(`_STATIC_RECONNECT_BASE_S = 1.0`, `_STATIC_RECONNECT_MAX_S = 60.0`, retry
forever), so a node that *did* genuinely fail would have healed within about a
minute regardless.

**Conclusion: no reconnect or reload was required.** The deployment was correct;
the acceptance assertion was wrong.

## 3. Fix: a bounded convergence window in step 6

Step 6 now polls the same assertions — unchanged in substance — until they pass
or a bounded window expires:

| Behaviour | Detail |
|---|---|
| Window | `NODE_CONVERGE_TIMEOUT_S` (default 120 s), polled every `NODE_POLL_S` (default 5 s) |
| Pass | all expected node ids connected with exactly the expected tool count and no error |
| Fail (converged never) | prints the collected per-node problems, `exit 8` — **bounded, never hangs** |
| Fail (channel unusable) | transport/auth/parse failure exits `2` from the verifier → `exit 8` immediately, no pointless retry |

The verifier still reads its payload from fd 3, keeping the program (stdin) and
the payload on separate channels.

### Tests

`tests/test_deploy_verifier.sh` drives the **real block, extracted verbatim from
the deploy script by marker**, so it cannot drift from what executes. **14/14
pass**:

| Class | Cases | Expect |
|---|---|---|
| Positive | healthy payload accepted + `PASS:` summary | 0 |
| Convergence | `disco,good`; `disco,disco,good` — mirrors the incident | 0 |
| Bounded | never converges | 8, within the window |
| Negative | node disconnected · node reports no status · wrong tool count · `auto_approve=true` · row disabled · no matching row | 8 |
| Fails closed | empty body · non-JSON body · unreadable channel | 8 |

Against the pre-fix script the same suite **fails**: the structural guard reports
the missing convergence wait, and behaviorally the pre-fix block returns `8` for
a sequence that converges on the second sample (the production incident exactly),
where the fixed block returns `0`.

Python suites are unchanged: **45 passed, 55 subtests**.

## 4. Method note: reading per-node MCP state without a credential

The per-node MCP status endpoint is auth-gated, but two surfaces are not:

- `GET /metrics` on each node exposes `turnstone_mcp_servers` ("Number of
  connected MCP servers"), `turnstone_mcp_server_errors`, and
  `turnstone_errors_total`.
- `GET /health` is unauthenticated and identifies the node.

This made the per-node connection state verifiable read-only, with no token and
no new credential. It reports whether a server is *connected*, not how many tools
it serves, so tool-count assertions still need the API (or the installed-bytes
smoke).

## 5. Remaining acceptance (operator-run)

| # | Item | Needs |
|---|---|---|
| 1 | Per-node tool-count assertion across all six endpoints | API token (`read`) |
| 2 | Installed-bytes smoke against `/opt/turnstone-integrations/self-admin/` (this is the deploy script's step 7, which did not run) | root |
| 3 | Model-visible authenticated read — a **fresh** workstream started after registration should see both bridge tools | workstream slot |
| 4 | Self-unload guard active — a live receipt reports `self_guard: active:<id> (file)` | API token |
| 5 | One bounded, reversible administrative write with read-back, binding the receipt's `request_sha256` to the API audit row | API token (`read,write,approve`) |

Re-running `deploy/` with the convergence fix in place is now deterministic for
items 1–2 and does not change the installed artifacts (the installer is
idempotent and registration detects the existing row).

## 6. Rollback (unchanged, for the record)

```bash
# disable:      PUT    <console>/v1/api/admin/mcp-servers/e8bb5e5945db40748ec209d7da6ca337  {"enabled": false}
#               then POST <console>/v1/api/admin/mcp-servers/reload
# remove row:   DELETE <console>/v1/api/admin/mcp-servers/e8bb5e5945db40748ec209d7da6ca337
#               then POST <console>/v1/api/admin/mcp-servers/reload
# remove files: rm -f /etc/sudoers.d/turnstone-self-mcp && rm -rf /opt/turnstone-integrations/self-admin
```

Existing credentials, roles, personas, policies and services are untouched.
