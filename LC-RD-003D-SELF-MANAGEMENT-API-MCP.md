# LC-RD-003D — Turnstone self-management API/MCP bootstrap (2026-10-16)

## Operator objective

Before installing Grafana skill, provide Turnstone with **full operator-authorized self-management capabilities** over its own Console and server APIs, then corresponding authenticated MCP tools. No deletion, reduction or cleanup of existing elevated privileges without operator approval. Protect existing five-node production.

## Upstream references inspected — compare dev versus deployed v1.8.7

- https://github.com/turnstonelabs/turnstone/blob/dev/docs/api-reference.md
- https://github.com/turnstonelabs/turnstone/blob/dev/docs/operations/mcp-oauth-headless.md
- https://github.com/turnstonelabs/turnstone/blob/dev/docs/coordinator-api-tour.md
- https://github.com/turnstonelabs/turnstone/blob/dev/docs/mcp-oauth.md
- https://github.com/turnstonelabs/turnstone/blob/dev/docs/mcp-registry.md

All five files exist in the released v1.8.7 tag. **Use live v1.8.7 OpenAPI and its docs for actual endpoint and payload compatibility.** The dev branch has changes and is not deployed.

## Findings: self REST control plane

- Console at 127.0.0.1:8090 is separate from server head 127.0.0.1:8080, workers 8082–8085.
- Persistent API tokens (ts_ + 64 lower hex) created in the Console Admin > API Tokens tab, or admin API POST /v1/api/admin/users/{user_id}/tokens. Token values display once, hashed in DB, are valid directly as Bearer header. The token issuing principal and named RBAC permissions determine effective access. Recommended service principal dedicated to self management; do not alter existing users or tokens.
- Need requested token scopes read,write,approve; verify named RBAC permissions using GET /v1/api/auth/whoami. Built-in admin lacks explicit opt-in capabilities, including model.skills.write. Scope alone does not imply those named permissions.
- Console API supports GET /v1/api/cluster/overview, /cluster/nodes, /admin/skills, /admin/mcp-servers, /admin/model-definitions, /admin/settings, /admin/roles, /admin/users; POST /admin/skills/install and /admin/mcp-servers/reload and MCP Registry install, plus more via /openapi.json.
- JWTs are audience scoped to Console versus Server. Service/API-derived JWTs are not renewable via interactive refresh; use persistent raw API token for automation where endpoint supports it.

## Findings: MCP is a separate surface

Turnstone 1.8.7 is documented as an outbound MCP client (stdio and Streamable HTTP), including static auth, oauth_user and oauth_obo. There is no evidence in these five docs that Console Admin REST endpoints are automatically exposed by a native Turnstone self-management MCP server. Provide a reviewed MCP bridge that maps the actual Console REST methods into structured agent-callable tools. A GitHub skills import and native MCP registry install are not equivalent to such a bridge.

OAuth browser consent must be completed by user before headless scheduled or channel-driven oauth_user MCP runs. oauth_obo needs IdP delegated authority, credential capture and shared encryption key. Those are optional for this self-API bootstrap: don't configure OAuth based on its availability alone.

## Files committed

- scripts/turnstone_self_api_probe.py: read-only local GET probe. Uses root-owned API token at /srv/turnstone/credentials/api/turnstone_admin_token; prints status, scopes and named-permission booleans only; **no credential in output**, no write/redirect/proxy/external endpoints.
- skills/turnstone-self-management/SKILL.md: release-pinned knowledge, API/MCP distinction, full-admin acceptance, no privilege diminution.
- Existing skills/grafana-operations/SKILL.md: ready for later import but NOT yet installed.
- Existing scripts/turnstone_credential_add.sh: hidden prompt put turnstone-api, non-overwrite and root-only 0600.

## Step A — create dedicated self-management token

Through Console Admin UI, verify a suitably authorized existing or dedicated management principal with Admin role and all requested explicit capabilities. In Admin > API Tokens select that principal and create a new token (suggest name turnstone-self-mgmt-20261016), scope read+write+approve. Avoid expired sessions or taking over an existing token; grant missing named permissions additively on explicit operator instructions. Save the raw ts_ token once. No token may be posted in chat or GitHub.

Stage it on turnstone-vm using hidden TTY (no nano trailing newline):
```bash
git -C "$HOME/turnstone-five-node-source" pull --ff-only
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_add.sh" status
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_add.sh" put turnstone-api
sudo python3 "$HOME/turnstone-five-node-source/scripts/turnstone_self_api_probe.py"
```
If status shows already staged, DO NOT overwrite with the script; examine operator-approved update path. An HTTP 200 on whoami and admin list calls establishes read-level access, **not** mutating API or model-visible MCP.

## Step B — full management MCP bridge

Proposed design awaiting actual API authorization proof:
- separate process/tool integration accessing the authenticated Console control plane, with explicit root-only token custody/approved broker identity so generic model shell tools cannot read it;
- no raw token in MCP headers stored with reveal=true or environment inherited by generic workstream shell;
- named tools for cluster inspection, skills list/import/verify, MCP server list/create/test/reload, model settings and registry operations, RBAC inspection and workstream lifecycle, using the actual Console OpenAPI schemas;
- full operator-authorized capability coverage, with tool approvals/audit receipts and independent Console/SSH recovery path, not an arbitrary restricted subset;
- validate MCP initialize/tools/list on all five nodes (or document an intentionally centralized deployment), a read operation from a real workstream, then one authorized reversible skill install and read-back;
- protect bridge against self-unloading or recursive admin changes during in-flight calls; preserve old grants; never mutate Switchyard/Grafana merely to test self-management.

## Step C — validate self-install then Grafana

After API and MCP checks, install turnstone-self-management and grafana-operations via POST /v1/api/admin/skills/install using source URLs pinned to appropriate repository commit, then verify native skills registry rows. Explicit skill selection may replace defaults on workstream creation; do not mark default automatically. Grafana token has already authenticated (13.2.4, RedEyeNinja org, 159 permission actions), and no Grafana dashboard mutation is yet authorized.

## Status

PREPARED IN GITHUB, NOT DEPLOYED: Self API token absent/unverified, self-MCP bridge not installed, native skills not installed. No services, permission grants, model DB rows, or Grafana dashboard changed by this staging.
