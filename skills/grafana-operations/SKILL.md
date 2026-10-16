---
name: grafana-operations
description: Version-aware Grafana HTTP API operations, dashboard lifecycle, datasources, Prometheus and Loki observability, permissions, alerting, inventory and safe deployments for grafana.bkk.redeyeninja.com.
version: 1.0.0
author: Turnstone operations
tags: [grafana, observability, dashboards, prometheus, loki, alloy, api]
license: MIT
---

# Grafana Operations — official-API-aligned operating contract

Use this skill whenever the task involves the Grafana instance at
https://grafana.bkk.redeyeninja.com/, its dashboards, datasources, alert rules,
Grafana HTTP API, Prometheus, Loki or Grafana alerting.

## Sources of truth (verify current live docs, never memorized routes alone)

- Master reference: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/
- Official machine-readable docs: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api.md
- API structural migration: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/apis/
- Service-account token auth: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/authentication/
- New dashboard APIs: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/dashboard/
- New folder APIs: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/folder/
- Legacy datasource APIs: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/
- Legacy dashboard/folder search: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/folder_dashboard_search/
- Legacy dashboard version history: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/dashboard_versions/
- Alerting provisioning: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/alerting_provisioning/
- Effective token permissions: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/access_control/
- Grafana service-account roles: https://grafana.com/docs/grafana/latest/administration/service-accounts/
- This skill's bundled reference file: references/HTTP-API-RUNBOOK.md.

The upstream **current/latest** documentation may be newer than the installed Grafana.
First identify local Grafana version and API support. Grafana 12 introduced
versioned /apis APIs for some resources. Grafana 13 deprecates legacy /api
endpoints, but they are not immediately removed. Availability differs by
version and resource; a new replacement does not necessarily exist yet.
Prefer the resource's supported, version-specific canonical endpoint;
do not mechanically rewrite all /api to /apis. Query /swagger-ui and
available instance OpenAPI when accessible; upstream docs are advisory
until local version/capability probing.

## Identity and secret use

The operator has stated an organization-Admin Grafana service-account token
was minted and staged, with no-echo input, at
/srv/turnstone/credentials/grafana/api_token (root:root 0600).
This path is NOT proof of a working API connection. An organization Admin is
NOT an instance-wide Grafana server administrator. Server-admin operations
require separate instance-admin authority, which is not assumed.

The token is deliberately ROOT-ONLY. Do NOT ask a Turnstone tool running as
the unprivileged OS user "turnstone" to cat it, chmod it, copy it into a
world-readable file, or include it in prompts/commands/logs.
Do NOT give arbitrary model sessions ambient token access.
Root-run, read-only verifier is in:
  scripts/turnstone_grafana_api_probe.py
under the existing operations source checkout; it outputs HTTP status and
small metadata only, not token, response body or data values.
Credential staging is not an authenticated runtime integration. To grant
Turnstone live Grafana control, deploy a separately reviewed Grafana adapter
or MCP connector that reads the token under narrowly owned privileges and
returns redacted structured responses. Connection, read and write receipts
are necessary before marking the integration operational.

Never use the token as a Grafana Cloud management API key; it addresses this
specific Grafana instance. Use Bearer authentication over certificate-verified
HTTPS. Do not follow redirects with bearer credentials to other origins, echo
secrets, inject secrets into shell command line/process listings, save token
in GitHub, or log Authorization headers. No TLS verification bypass.

## Before the first mutating task

1. Discover installed version using safe GET /api/health and server API features;
   record Grafana version, organization ID, edition if available and supported API
   routes. Do not infer edition from hostname.
2. Verify effective token access with GET /api/org/, GET /api/search,
   GET /api/datasources, GET /api/access-control/user/permissions.
   Do not print entire datasource JSON: it contains operational metadata.
   Treat 401 as invalid/expired token, 403 as scope failure, 404 as
   endpoint/version incompatibility, 429 as backoff/retry-later.
3. Inventory dashboards via UID, folder UID, title, version, row/panel IDs,
   datasource UIDs, templating variables, transformations, alert references,
   provisioned-versus-editable flag, and folder permissions. Handle
   pagination/continuation rather than assuming one page is exhaustive.
4. Inventory Prometheus and Loki datasource UIDs, health/support and access
   modes, plus relevant Alloy target labels, where accessible. Grafana
   datasource proxy access is not equivalent to direct Prometheus/Loki access.
5. Archive exact current JSON of every dashboard touched, with original UID,
   version, folder and a secure checksum + rollback procedure. Preserve
   current production Grafana dashboards and Alloy/Loki configuration.
6. Create an explicit per-dashboard/alert/datasource diff. Match based on UIDs,
   not names. Preserve user edits and permissions; no mass replacement.
7. Stage and validate changes against actual PromQL/LogQL labels, query results
   and datasource UIDs. Monitor query range, cardinality, timeouts and cost.
8. Require an operator GO before deploying the dashboard reformation,
   datasource writes, deletion, alert notification changes, or provisioning.
   Retain full operator-authorized access; do not silently remove or reduce it.
9. Apply with version-concurrency controls. Prefer non-destructive update,
   preserve UID and folder and maintain dashboard version history, then read
   back the installed resource to verify the exact result.
10. Produce per-resource evidence: HTTP response, UID, version/revision,
   intended versus effective state, before/after diff, rollback anchor,
   datasource queries verified, and test/render checks.

**Important:** HTTP API operations do not replace host-side configuration,
Grafana provisioning files, Docker/systemd, Prometheus storage APIs, or
Alloy/Loki deployment and SSH management. Track these separately.

## Functional domains Turnstone should recognize

| Domain | Read operations | Change operations (after GO) |
|---|---|---|
| Discovery | /api/health, /swagger-ui, /api/org/ | none |
| Permissions | /api/access-control/user/permissions | service-account/RBAC changes |
| Dashboards | /api/search, /api/dashboards/uid/:uid; new /apis/dashboard... | version-aware create/update, folder moves, delete |
| Dashboard history | /api/dashboards/uid/:uid/versions | rollback only after comparison |
| Folders | /api/folders, new /apis/folder... | create/update/move permissions |
| Datasources | /api/datasources, /api/datasources/uid/:uid | POST/PUT/DELETE, credentials, provisioned resources |
| Queries | /api/ds/query (POST is normally non-mutating query) | no deployment implied |
| Alerting | /api/v1/provisioning/alert-rules and GET exports | provisioning writes with provenance and rule-group validation |
| Observability | Prometheus query API, Loki query API | targets/Alloy/prometheus configs require separate SSH/deploy gate |
| Access management | users, teams, service accounts, org info | verify token type and admin scope first |

When direct Prometheus query access is requested, use its own API
(/api/v1/query, /api/v1/query_range, /api/v1/targets) if reachable and
authorized; Grafana service-account credentials are not automatically valid
there. Likewise Loki uses its own /loki/api/v1 endpoints and credentials.

For Grafana-managed alerts: the provisioning export format is not the same
as the HTTP API create/update format. Preserve rule provenance, folder and
group, evaluation frequency, contact points and mute timings. Don't assume
that Grafana's new alert APIs replace all older alerting routes yet.

## Known fleet deployment objective (NOT performed by this skill itself)

A later independent project will reconcile the Grafana dashboard with
Switchyard's actual routes, smart tiers, limits, readiness and cost model,
and the metrics exposed by Alloy/Prometheus/Loki across agent-vm and
switchyard-vm. Do not import all Switchyard routes into Turnstone or
reconfigure Switchyard lanes merely because Grafana credentials are present.
No dashboard update has been authorized by installing this skill.

## Working evidence and failure reporting

Use explicit statuses: STAGED, AUTHENTICATED, READ-ONLY VALIDATED,
WRITE CAPABLE (UNTESTED), CHANGE STAGED, DEPLOYED/READ-BACK VERIFIED.
Never call a token 'connected' solely because its file exists.
Never equate 200 from /api/health with token authentication.
No direct tool or HTTP client access? Clearly report that access is not
available instead of inventing successful API checks or dashboard state.

Be able to answer workflow questions with accurate methods, endpoint
boundaries and change plans even before runtime API transport is installed.
