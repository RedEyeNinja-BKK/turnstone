# Grafana API enablement — Turnstone v1.8.7 — 2026-10-16

## Goal
Make Turnstone *actually capable* of understanding the installed Grafana version, authenticated API surface, and safe dashboard deployment practice before refreshing the Switchyard dashboard. Do not infer operational Grafana access from a stored token.

## Source of truth
Official Grafana HTTP API reference:
https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/

Grafana instance: https://grafana.bkk.redeyeninja.com/
Current token source: /srv/turnstone/credentials/grafana/api_token (root:root 0600). Operator describes an organization-Admin service-account token. It was hidden-TTY staged but not yet API-verified.

## Committed resources
- skills/grafana-operations/SKILL.md — native Turnstone skill, procedures and operating boundary
- skills/grafana-operations/references/HTTP-API-RUNBOOK.md — lifecycle, endpoint map and compatibility
- scripts/turnstone_grafana_api_probe.py — read-only root-executed HTTPS API validation, no redirects, no secrets printed, reports installed version, authenticated org, token permission categories, dashboard search, datasources and org-user readability

## Install the skill (native Turnstone 1.8.7)
1. Pull latest operations branch on turnstone-vm.
2. Open Turnstone Console > Administration > Skills > Discover > Import from GitHub.
3. The native GitHub skill importer parses branch in a URL as a *single URL segment*. Because the existing operations branch name contains slashes, avoid a tree URL using that branch. Instead use a **pinned commit SHA**:
   https://github.com/RedEyeNinja-BKK/turnstone/tree/0af809706dbe62081537464846f38ef3e3679dd6/skills/grafana-operations
4. Preview skill name grafana-operations and its included reference resource. Import; do not mark it default until explicit review. Installed skill content is source-owned/readonly, but runtime config is editable in the UI.
5. In a NEW workstream, choose the skill explicitly and ask it to explain the instance's Grafana endpoint compatibility, the difference between org Admin and server Admin, and how it would back up dashboards for the Switchyard project. An explicitly chosen skill may replace default workstream skills; verify policies before using it operationally.
6. Verify skill is selectable and its answers link the official API source; re-check when upstream or instance version changes.

## Live API credential validation (read only)
On turnstone-vm:
```bash
git -C "$HOME/turnstone-five-node-source" pull --ff-only
python3 -m py_compile "$HOME/turnstone-five-node-source/scripts/turnstone_grafana_api_probe.py"
sudo python3 "$HOME/turnstone-five-node-source/scripts/turnstone_grafana_api_probe.py"
```
Expected organization=HTTP 200 for *authenticated* verification; instance_health may return HTTP 200 without token validity. Token permissions endpoint may be edition or version dependent; inspect the result. Do not print tokens, full RBAC scopes, datasource response bodies or other sensitive data. Probe script performs **only GET**. HTTPS verification is on and auth-header redirects are refused.

## Active management access separate from skill
The skill instructs the model; it does not create a network tool or grant direct access to secret files. To manage Grafana from agent workstreams, install a supported authenticated Grafana MCP/API adapter, with structured read/write capabilities and receipt logging. Resolve how it gets access to the existing root-only token; do not loosen token file permissions or embed the token in the model prompt. Confirm read-only operations first. Operator-approved dashboard mutation may follow after inventory and rollback.

## Acceptance
- Native skill imported/visible, correct version pinned and source acknowledged.
- Live version discovered; Grafana org authentication and RBAC actions verified.
- Dashboard and datasource inventory produced read-only; pagination accounted for.
- Service account's organization Admin verified, not assumed server Admin.
- No token logged or embedded in GitHub.
- New Turnstone agents can perform API operations only after tool connector available, not merely skill-loaded.
- Dashboard redeploy waits for current dashboard snapshot and operator GO.
