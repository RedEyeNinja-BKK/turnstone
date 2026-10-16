# LC-RD-003E — Turnstone self-MCP candidate and deployment gates (2026-10-16)

## Operator receipt

- Console, head and workers 2–5 each authenticate the staged ts_ admin credential with HTTP 200, scopes read/write/approve.
- All six expose the same 33 named permissions. Verified present: admin.skills, admin.mcp, admin.users, admin.roles, admin.settings, admin.models, admin.coordinator, coordinator.trust.send.
- Verified absent: model.skills.write. This is explicitly ungranted to built-in Admin by v1.8.7. For the operator's full-self-management goal, add it to the management principal **additively**, preferably via a custom role associated with that principal, leaving existing roles/grants unchanged.
- Console GET cluster overview/nodes/skills/MCP/models/settings/roles/users all returned 200. This establishes READ acceptance, not writes or MCP tooling.
- Grafana 13.2.4 org RedEyeNinja service-account token already authenticated independently.

## Candidate MCP integration committed (not deployed)

- integrations/turnstone-self-api/server.py — FastMCP stdio backend. Uses only loopback Console/head/workers; verifies root-only ts_ credential on each HTTP call and refuses bearer-forwarding redirects, non-API or traversal paths and ?reveal parameters. Exposes turnstone_api_read and turnstone_api_change, with redacted JSON outputs.
- integrations/turnstone-self-api/tests/test_bridge.py — offline unit tests for loopback origin, URL/path guard, secret masking, root-only checks and mocked GET.
- Do NOT confuse this with the upstream examples/mcp-cluster-ops, which upstream marks superseded by native 1.5+ coordinators and which grants command execution on nodes rather than typed Admin API access.

## Privilege and deployment authority boundaries

- Token remains root:root 0600 at /srv/turnstone/credentials/api/turnstone_admin_token; never downgrade its mode, copy to config/env or print it.
- Candidate design: install server.py into a root-owned, non-writable path, e.g. /opt/turnstone-integrations/self-admin/server.py; allow only the exact command through a new additive sudoers entry for the turnstone service user if systemd allows privilege changes; each stdio MCP subprocess then reads the token as root and performs only authorized REST calls. This is a **candidate**, not an approved mechanism; it needs sandbox/UID/sudo validation and security review before any sudoers or systemd change.
- Do not grant generic model-controlled root shell. Avoid storing plaintext token in MCP admin env or headers. Keep independent Console login and SSH recovery.
- MCP registry row must have transport=stdio, no generic env secrets, auto_approve=false, and be tested for both read and mutating-tool approval behavior. Registration/reload updates MCP pools across nodes and MUST be separately operator-approved after preflight.
- Since tool results may contain arbitrary API output, output masking is best-effort: subject candidate to adversarial secret-leak tests before considering production; never call secret redaction complete on unit coverage alone.

## Next read-only operator preflight on turnstone-vm

```bash
git -C ~/turnstone-five-node-source pull --ff-only
python3 -m py_compile ~/turnstone-five-node-source/integrations/turnstone-self-api/server.py
python3 -m unittest discover -s ~/turnstone-five-node-source/integrations/turnstone-self-api/tests -v

echo '=== SERVICE HARDENING ==='
systemctl show turnstone-console.service turnstone-server.service turnstone-node@2.service \
  -p User -p NoNewPrivileges -p RestrictSUIDSGID -p PrivateUsers -p ProtectSystem -p DynamicUser

echo '=== SUDO AVAILABLE ==='
command -v sudo
echo '=== SERVICE IDENTITY ==='
id turnstone
echo '=== MCP PYTHON RUNTIME ==='
/opt/turnstone-venv/bin/python -c 'from mcp.server.fastmcp import FastMCP; print("FastMCP available")'
```

Do not install sudoers or register the MCP until the output is evaluated. Validate both no-new-privileges and sudoers exact-command behavior; if sudo cannot be used inside service sandbox, switch to a root-broker with peer UID validation, not weakening existing systemd security.

## After preflight

1. Decide role grant targeting (custom role with model.skills.write assigned to management token principal). Verify named permissions become 34 or more on all nodes, with no decrease in 33 existing.
2. Complete independent review of candidate generic Admin API tool and secret handling.
3. Install root-owned bridge code and narrowly matched privileged entry point, preserve all current privileges.
4. Verify MCP initialize/tools/list under service UID with fake token and controlled request first.
5. Register and reload MCP via live API only with GO; check all five nodes' MCP status and existing servers.
6. Real workstream discovery/read, operator-approved skill install, read-back and recovery; no Switchyard or Grafana mutation implied.

STATUS: API AUTHENTICATED + NAMED PERMISSIONS VERIFIED; SELF MCP CANDIDATE STAGED IN GIT; NOT DEPLOYED.
