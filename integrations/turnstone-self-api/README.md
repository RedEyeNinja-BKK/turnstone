# turnstone-self-api — self-management MCP bridge (v1.8.7)

Exposes Turnstone's authenticated Console/server Admin REST API as two
model-callable MCP tools, so an agent can administer Turnstone through the same
permission-checked routes an operator uses.

| File | Purpose |
|---|---|
| `server.py` | FastMCP **stdio** bridge. Two tools: `turnstone_api_read`, `turnstone_api_change`. |
| `smoke_mcp.py` | Read-only stdio handshake + six authenticated reads (console + head + workers 2-5). No writes. |
| `receipt_check.py` | Validates a smoke receipt and refuses one containing credential-shaped text. |
| `tests/test_bridge.py` | Original offline suite (11 tests). |
| `tests/test_bridge_patch.py` | LC-RD-003 hardening suite (34 tests). |
| `deploy/launch-self-mcp.sh` | Privileged launcher wrapper (re-exports the server id; survives `env_reset`). |
| `deploy/sudoers-turnstone-self-mcp` | Exact-argv sudoers template. NOT installed by this repo. |
| `deploy/run_smoke_root.sh` | Operator-run root smoke gate: hash-verifies, stages root-owned, runs, validates receipt. |
| `deploy/deploy-self-mcp.sh` | Operator-run installer/registrar: gates on a valid receipt, installs root-owned, registers the MCP row, activates the self-unload guard, reloads, verifies discovery. `--dry-run` previews. |

## Operating model — read this before deploying

This bridge runs as **root** so it can read a root-only API credential, and the
privileged launcher is a **convenience for a trusted administrative principal,
not a security barrier**.

Accurately stated:

- The Turnstone service UID is **not** an isolation boundary. Anything running as
  that UID can invoke the same privileged command and speak MCP over stdio.
- MCP approval is a **dispatch-level** control. `auto_approve=false` applies to
  calls made **through** Turnstone's MCP pipeline; it does not constrain a
  directly-launched process.
- Console audit attributes swept actions to the **shared token principal**, not
  to the workstream that requested them.

What the bridge *does* avoid: creating credential disclosures, silently
disabling itself, and leaving no audit trail. It does not, and cannot under this
model, prove that a given call was operator-approved.

## Deployment

Prerequisite: a root-only API token at
`/srv/turnstone/credentials/api/turnstone_admin_token` (`root:root 0600`,
content exactly `ts_` + 64 lowercase hex).

```bash
# 1) install root-owned, non-writable by the service user
sudo install -d -o root -g root -m 0755 /opt/turnstone-integrations/self-admin
sudo install -o root -g root -m 0644 integrations/turnstone-self-api/server.py \
     /opt/turnstone-integrations/self-admin/server.py
sudo install -o root -g root -m 0755 integrations/turnstone-self-api/deploy/launch-self-mcp.sh \
     /opt/turnstone-integrations/self-admin/launch-self-mcp.sh

# 2) exact-argv launcher (validate before installing; no generic root shell)
sudo install -o root -g root -m 0440 integrations/turnstone-self-api/deploy/sudoers-turnstone-self-mcp \
     /etc/sudoers.d/turnstone-self-mcp
sudo visudo -c -f /etc/sudoers.d/turnstone-self-mcp

# 3) register the MCP row (stdio, no secrets in env, auto-approve OFF)
#    POST /v1/api/admin/mcp-servers
#    {"name":"turnstone-self-api","transport":"stdio",
#     "command":"/usr/bin/sudo",
#     "args":["-n","/opt/turnstone-integrations/self-admin/launch-self-mcp.sh"],
#     "auto_approve":false,"enabled":true}
#    Capture the returned server_id, then activate the self-disable guard:
sudo sh -c "printf '%s' '<server_id>' > /opt/turnstone-integrations/self-admin/self_server_id"
sudo chown root:root /opt/turnstone-integrations/self-admin/self_server_id
sudo chmod 0644 /opt/turnstone-integrations/self-admin/self_server_id

# 4) reload and verify on every node (Console :8090 and workers :8082-8085)
#    POST /v1/api/admin/mcp-servers/reload
#    GET  /v1/api/admin/mcp-servers   -> status.connected true, 2 tools
```

**Guard propagation.** The MCP row's `env` reaches the spawned process, but
`sudo`'s default `env_reset` drops variables before exec, so `SELF_MCP_SERVER_ID`
must not be relied on through the launcher. The bridge resolves its own id from
(a) the env var, (b) the root-owned `self_server_id` file, (c) an admin-API
self-lookup matching this module's path in a row's `command`/`args`. When none
resolves, every receipt says `self_guard: inactive_unresolved` — the guard is
never silently inactive.

## Tools

- `turnstone_api_read(path, target="console")` — GET any `/v1/api/` route.
- `turnstone_api_change(method, path, body_json="", target="console")` — POST/PUT/PATCH/DELETE.

`target` is `console`, `head`, `worker_2` … `worker_5` (all loopback).

## Security properties (verified by the test suite)

Enforced:
- Loopback-only targets; redirects refused; proxies ignored.
- Token read requires root, `O_NOFOLLOW`, regular file, `root`-owned, mode `& 0o077 == 0`, exact `ts_[0-9a-f]{64}`.
- Path: no traversal, no `//`, no `@`, no control chars, no **percent-encoded characters** at all.
- Query: sensitive keys (`reveal`, `token`, `secret`, `api_key`, …) refused after bounded
  repeated percent-decoding; CR/LF refused; `;`-separated params refused; no query on writes.
- `POST /v1/api/admin/users/{id}/tokens` (credential minting) **refused explicitly**, with a
  message pointing at the Console path. Redaction cannot be trusted for a route whose purpose
  is to emit a plaintext token, so the route itself is denied.
- Self-disable refused: `PUT`/`PATCH`/`DELETE` of this bridge's own MCP row (when the id resolves).
- Response redaction by key name plus known token shapes; `client_id`/`*_ct` included.
- Every response carries a receipt: `receipt_id`, `ts`, `method`, `path`, `target`, `status`,
  `request_sha256` (canonical body digest), `self_guard`.
- Bounded: 15 s timeout, 64 KiB request, 256 KiB read, 18 KB return, truncation flagged.

Not enforced (by design — full operator-authorized capability is preserved):
- No generic route allowlist. Anything the token may do, the bridge may do, except the mint
  route and its own row.
- Redaction is best-effort, not a guarantee. It is **not** the control protecting minting.
- The receipt is a correlation record, **not** an unforgeable authorization token.

## Rollback

```bash
# graceful
#   PUT /v1/api/admin/mcp-servers/<server_id>  {"enabled": false}   then reload
# full removal
#   DELETE /v1/api/admin/mcp-servers/<server_id>                    then reload
sudo rm -f /etc/sudoers.d/turnstone-self-mcp
sudo rm -rf /opt/turnstone-integrations/self-admin
```

Rollback touches only the newly installed components. It does not revoke agent
privileges, disable nodes, or affect the LC-RD-003F memory policy.

## Tests

```bash
python3 -m unittest discover -s integrations/turnstone-self-api/tests -v   # 45 tests
# prove the hardening tests are meaningful (point them at the pre-patch file):
SELF_MCP_SERVER=/path/to/original_server.py \
  python3 -m unittest discover -s integrations/turnstone-self-api/tests -p 'test_bridge_patch.py'
```

Lint: `ruff check` and `mypy --config-file pyproject.toml` are clean on the files this
workstream changed. Two pre-existing findings are intentionally left alone:
`N818` on the public exception name `BridgeRefusal`, and 4 × `SIM117` in the untouched
`tests/test_bridge.py`; plus 3 pre-existing mypy strict errors (`NoRedirects.redirect_request`
and the untyped `FastMCP.tool` decorators).
