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

## 5. Acceptance: executed through the deployed MCP bridge

Acceptance was completed by **acting as an MCP client against the deployed
bridge**, over the exact same path the cluster nodes use:

```
sudo -n /opt/turnstone-integrations/self-admin/launch-self-mcp.sh
```

That rule is the only privileged argv available to the service account
(`(root) NOPASSWD: …/launch-self-mcp.sh ""` — no generic root shell, no
arguments). The bridge reads the root-only API credential itself; no token was
read, printed, minted or handled by the client.

### 5.1 Discovery (standard MCP `tools/list`)

```
server   turnstone-self-api 1.30.0
protocol 2024-11-05
turnstone_api_read    path*, target
turnstone_api_change  method*, path*, body_json, target
```

Both tools and their input schemas were discovered via the normal MCP
handshake — no bespoke client, no out-of-band configuration.

### 5.2 Authenticated reads: all six endpoints HTTP 200

`GET /v1/api/auth/whoami` through the bridge, one call per cluster target.
Every target returned the same authenticated identity:

| Target | Bridge receipt status | API HTTP | Identity returned |
|---|---|---|---|
| console | 200 | 200 | user `f8fae234…`, scopes `approve,read,write`, `Vincent` |
| head | 200 | 200 | same |
| worker_2 | 200 | 200 | same |
| worker_3 | 200 | 200 | same |
| worker_4 | 200 | 200 | same |
| worker_5 | 200 | 200 | same |

### 5.3 Registration and per-node tool counts

`GET /v1/api/admin/mcp-servers` through the bridge confirmed the row and, for
**all six endpoints**, `connected: true`, `tools: 2`, `has_error: false`,
`circuit_open: false`, `consecutive_failures: 0`:

| Endpoint | connected | tools |
|---|---|---|
| console | true | 2 |
| turnstone-head-1 | true | 2 |
| turnstone-node-2 | true | 2 |
| turnstone-node-3 | true | 2 |
| turnstone-node-4 | true | 2 |
| turnstone-node-5 | true | 2 |

Row `e8bb5e5945db40748ec209d7da6ca337`, `transport: stdio`,
`enabled: true`, `auto_approve: false`, `env` redacted, `created`/`updated`
`2026-10-16T19:11:17` (unchanged by acceptance).

### 5.4 Self-unload guard: ACTIVE

Every receipt from the deployed bridge carries:

```
self_guard  active:e8bb5e5945db40748ec209d7da6ca337 (env)
```

Source is `env`, not `file`, and that is correct: `launch-self-mcp.sh` reads the
root-owned `self_server_id` file and **re-exports** it, because the bridge checks
`SELF_MCP_SERVER_ID` from the environment first and falls back to the file. The
guard is active through both paths; `(env)` is simply the one that wins. (An
earlier draft of this record expected `(file)`; the launcher's re-export makes
`(env)` the expected reading.)

### 5.5 Bounded, reversible administrative write with read-back

Target: `heuristic_rules` — 0 rows before and after, both create and delete
audited, API-reversible, no process spawn and no cluster fan-out.

| Step | Receipt | Method / path | Status | `request_sha256` |
|---|---|---|---|---|
| create | `577c266fa2f2481780277678d4ea66d9` | POST `/v1/api/admin/judge/heuristic-rules` | **201** | `28501ead8da47d9c9e6b4936253cd67a305ea85d96957da6ee8d4ce965675f58` |
| read-back | `b6bd4f50cf6547f08c0770185e454241` | GET `…/heuristic-rules/74343d949cf04b87a2807493a04f5c3a` | 200 | `ce6239da8bd460a6540e680448dafe2b947995e5354cd676c2c21299f62e5a69` |
| delete | `d34aaf3d861940bb93ed318b446cd2e1` | DELETE `…/heuristic-rules/74343d949cf04b87a2807493a04f5c3a` | 200 | `7a0659c4ce0b66e64a5c6620a0f18b023d75fbecd4ef5bde8dc177dac06645e6` |

Created row `74343d949cf04b87a2807493a04f5c3a`, name
`lc-rd-003-acceptance-probe`, `enabled: false`, `tool_pattern:
__lc_rd_003_probe__` (cannot match a real tool name), confidence 0.1 —
inert by construction as well as by the disabled flag. Read-back returned the
row verbatim; DELETE returned `{"status":"ok","rule_id":"74343d94…"}`.

**Receipt digests were independently reproduced.** Recomputing
`sha256(f"{method}\n{path}\n{target}\n{canonical_body}")` from the client-side
request bytes yields all three `request_sha256` values exactly, so each receipt
is bound to the precise request that produced it, not merely to a timestamp.

**API audit evidence** (`audit_events`, queried read-only):

| timestamp | user_id | action | resource_id | detail |
|---|---|---|---|---|
| 2026-10-16T19:28:58 | `f8fae2349aa94e1eafe9ddb7c08c6f93` | `heuristic_rule.create` | `74343d949cf04b87a2807493a04f5c3a` | `{"name": "lc-rd-003-acceptance-probe"}` |
| 2026-10-16T19:29:03 | `f8fae2349aa94e1eafe9ddb7c08c6f93` | `heuristic_rule.delete` | `74343d949cf04b87a2807493a04f5c3a` | `{"name": "lc-rd-003-acceptance-probe"}` |

Binding chain: `request_sha256` → exact request body → created `rule_id` →
`audit_events.resource_id`, at timestamps matching the receipts to the second.

### 5.6 State preserved (verified after the write)

`personas=6`, `prompt_policies=1`, `mcp_servers=1`, `api_tokens=1`,
`role_permission_overrides=1`, `heuristic_rules=0`, `users=1`. The MCP row's
`updated` timestamp is unchanged (`19:11:17`), the token inventory is unchanged
(same `token_id`, no additions), and all six endpoints remain connected with
zero MCP errors. `structured_memories` went 1 → 4 solely from three memories
saved by the agent during this workstream; the acceptance write did not touch
the memory subsystem.

## 6. One item still needs root

The only part of the installed-bytes smoke that cannot be reproduced from the
service account is its **receipt artifact** (`smoke_mcp.py` refuses to run
without `euid 0`, and the exact-argv sudoers rule permits only the launcher).
Its *substance* — root-context handshake, both tool names, both input schemas,
and six authenticated reads — was reproduced above through the launcher path.
The residual item is producing/validating the receipt file, one command:

```bash
sudo bash -c 'cd /opt/turnstone-integrations/self-admin && \
  /opt/turnstone-venv/bin/python smoke_mcp.py 2>&1 | tee /run/self-admin-installed-smoke.txt && \
  /opt/turnstone-venv/bin/python receipt_check.py /run/self-admin-installed-smoke.txt'
```

Expected: `MCP smoke PASS: 6 authenticated reads; zero writes performed`, then a
valid receipt. No credential issuance is involved and no other root action is
needed.

## 7. Documentation correction

`deploy/launch-self-mcp.sh`'s header comment illustrated the sudoers rule
**without** the trailing `""`, i.e. the weaker form that in sudoers matches
*any* arguments. The authoritative template
(`deploy/sudoers-turnstone-self-mcp`) and the installed rule both correctly end
in `""`. The comment now matches, with the reason stated. Comment-only change:
the executable body of the installed copy is byte-identical, and re-installing
the launcher is not required.

## 8. Rollback (unchanged, for the record)

```bash
# disable:      PUT    <console>/v1/api/admin/mcp-servers/e8bb5e5945db40748ec209d7da6ca337  {"enabled": false}
#               then POST <console>/v1/api/admin/mcp-servers/reload
# remove row:   DELETE <console>/v1/api/admin/mcp-servers/e8bb5e5945db40748ec209d7da6ca337
#               then POST <console>/v1/api/admin/mcp-servers/reload
# remove files: rm -f /etc/sudoers.d/turnstone-self-mcp && rm -rf /opt/turnstone-integrations/self-admin
```

Existing credentials, roles, personas, policies and services are untouched.
