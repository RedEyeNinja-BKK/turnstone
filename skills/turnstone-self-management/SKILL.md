---
name: turnstone-self-management
description: Administer Turnstone 1.8.7 through its authenticated Console and workstream REST APIs and separately proven self-management MCP tools, including skills, integrations, model routes, nodes and governance.
version: 1.0.0
author: Turnstone operations
tags: [turnstone, administration, api, mcp, oauth, cluster]
license: MIT
---

# Turnstone Self-Management — verified-release operating contract

Reference the deployment version rather than dev-branch assumptions.
Deployed release docs:
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/api-reference.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/operations/mcp-oauth-headless.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/coordinator-api-tour.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/mcp-oauth.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/mcp-registry.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/governance.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/security.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/console.md
- https://github.com/turnstonelabs/turnstone/blob/v1.8.7/docs/sdk.md

The corresponding upstream dev docs may be newer; do not claim that a
dev-only method or auth variant is available in production. Confirm
what is supported through the Console OpenAPI specification and live
read-only permission/status requests.

## Existing deployment

Turnstone Console: http://127.0.0.1:8090 on turnstone-vm.
Server head: http://127.0.0.1:8080; worker servers 8082-8085.
All five production nodes were reported healthy with PostgreSQL
and Switchyard integration. Preserve active configs and privileges.
Credential directory: /srv/turnstone/credentials
Grafana token: grafana/api_token — validated Grafana organization Admin.
Self API token: api/turnstone_admin_token — staging/verification pending.

## API control-plane versus MCP transport

1. Turnstone Console REST API: administrative users, roles, tokens,
   skill installation, MCP server definitions/registry, model definitions,
   settings, cluster nodes and orchestration.
2. Turnstone server REST API: individual workstreams, events, history,
   send, approvals, cancellations and tool interaction.
3. Turnstone MCP client: stdio or Streamable HTTP remote MCP servers,
   with model-visible tools. MCP server registration does not
   automatically expose Turnstone's own REST Admin API as MCP tools.
4. To achieve agent-callable self-management, provision and prove
   a dedicated authenticated MCP bridge/adapter exposing
   Turnstone Console API methods as named tools. Do not invent a
   native self-MCP endpoint that has not been observed.

Important Console REST paths:
- GET /v1/api/auth/whoami
- GET /v1/api/cluster/overview and /v1/api/cluster/nodes
- GET /v1/api/admin/skills
- POST /v1/api/admin/skills/install
- GET/POST /v1/api/admin/mcp-servers
- POST /v1/api/admin/mcp-servers/reload
- GET /v1/api/admin/mcp-registry/search
- POST /v1/api/admin/mcp-registry/install
- GET /v1/api/admin/model-definitions
- GET /v1/api/admin/settings
- GET /v1/api/admin/roles and /v1/api/admin/users
Discover the additional v1.8.7 methods from the authoritative live
OpenAPI specification rather than guesses.

## Persistent admin token and effective authority

Turnstone accepts database-issued tokens in ts_ + 64 hex format, used
directly as HTTP Bearer credentials. JWTs are distinct, short-lived,
and audience-scoped to Console or Server. Service/API JWTs do not
have password/OIDC refresh semantics.

An operator-authorized Admin identity should have its requested
read, write and approve token scopes, plus the actual named RBAC
permissions required for each API operation. Built-in Admin does
not automatically have every opt-in capability, notably
model.skills.write. Determine missing capabilities explicitly and
ask the operator about additions as needed. Never silently revoke
or diminish operator-authorized privileges or replace old accounts.

The token must live in a private credential store and never be
printed to logs, commands, model messages, debug traces, skill
content, or GitHub files. A root-only token in
/srv/turnstone/credentials/api/turnstone_admin_token is intentional.
Do not loosen it to allow arbitrary agent shell tools to read it.

Run the staged read-only verifier scripts/turnstone_self_api_probe.py
as root. It checks identity/scope evidence and GET endpoints against
Console and head without any mutation. Success proves API-level
read access, NOT write capability or an MCP connection.

## MCP authentication modes and headless constraints

Supported server modes on v1.8.7:
- none: stdio or Streamable HTTP; protect through process/network boundary.
- static: Streamable HTTP with shared configured headers.
- oauth_user: individual OAuth 2.1 + PKCE browser consent for each user
  and MCP server. Scheduled/headless tasks require pre-consent, or
  they surface a structured mcp_consent_required event.
- oauth_obo: reuse captured enterprise OIDC identity credentials,
  requires matching IdP and OBO grants.

OAuth credential encryption is configured in TOML and must be
available on Console and nodes sharing the database. Back up
the encryption keyring before configuring such connections.
OAuth for *external* MCP servers is NOT the authentication mechanism
required for a local self-API bridge unless the adapter itself is an
OAuth-protected MCP resource.

The MCP Registry is an outbound discovery/install surface, not a
hosted general-purpose self-management server. Registry installs
can reload MCP pools across the cluster: verify existing connections
before and after any such operation.

## Full-authority self MCP acceptance (pending implementation)

An authenticated, full-management bridge must:
- expose real, version-matched REST actions with valid tool schemas;
- preserve the operator's grants rather than silently narrow them;
- keep root-only token away from model output, prompts, logs, generic shell;
- provide audit receipts, method+route+node IDs, response redaction,
  and bounded timeouts;
- avoid recursively disabling its own connection mid-call; maintain
  an independent recovery path through Console login and SSH;
- be deployed/reloaded with all five nodes accounted for, not just head;
- prove model-visible MCP tool discovery and actual read tool execution;
- prove controlled writes (such as importing this or the Grafana skill),
  response versioning/read-back and rollback before claim of readiness.

Do not call self-admin complete merely because API token exists,
skill installed or MCP server row configured. Require the chain:
token STAGED -> Console AUTHENTICATED -> named PERMISSIONS VERIFIED ->
MCP bridge DEPLOYED -> discovered on desired nodes -> read tool PASS ->
authorized write tool PASS -> complete receipts.

## Governance and coordinating workstreams

Coordinator API covers spawning, supervising and closing child
workstreams; it is not equivalent to an Admin REST API credential.
Some coordinator power, including trust/send, is separately permission
gated. Never use revoke/restrict/close operations to clean up
operator-authorized access without explicit approval.

Skill installation and activation differ. A Git commit is NOT
a deployed skill. Console Admin API install requires admin.skills;
selecting an explicit skill in a new workstream can replace default
skills, so check inherited workstream configuration first.
