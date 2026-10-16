# Grafana HTTP API — endpoint map and Turnstone operations reference

This is a curated navigation and operational reference, not a replacement
for Grafana's official versioned API schema. Verify deployed version first.

Canonical docs: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/
Markdown index: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api.md
New structure: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/apis/
Authentication: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/authentication/

## Resource-oriented map

| Task | Legacy HTTP API (verify support) | New generation /apis (verify installed version) | Identity notes |
|---|---|---|---|
| Instance health/version | GET /api/health | n/a | Public endpoint does NOT validate bearer token |
| Current org | GET /api/org/ | Consult installed API schema | Tests token authenticated org context |
| Token permissions | GET /api/access-control/user/permissions | Consult installed API schema | Lists effective scopes (edition dependent) |
| Search dashboards/folders | GET /api/search?type=dash-db&limit=...&page=... | New folder/dashboard resource list APIs | Search RBAC filtered and paginated |
| Get dashboard | GET /api/dashboards/uid/{uid} | GET /apis/dashboard.grafana.app/v1/namespaces/{namespace}/dashboards/{uid} | Return schema differs |
| List dashboards | GET /api/search then get each UID | GET /apis/dashboard.grafana.app/v1/namespaces/{namespace}/dashboards?limit=...&continue=... | New list uses metadata.continue |
| Create/update dashboard | POST /api/dashboards/db | POST/PUT new resource API | Preserve UID/version and folder; envelopes differ |
| Dashboard versions | GET /api/dashboards/uid/{uid}/versions | Resource history APIs vary by version | Preserve rollback anchor |
| Get/list folders | GET /api/folders, /api/folders/{uid} | /apis/folder.grafana.app/... | Folder namespace and UID distinct from dashboard title |
| List datasources | GET /api/datasources | Check installed schema | May contain sensitive configuration metadata |
| Get datasource | GET /api/datasources/uid/{uid} | Check installed schema | Prefer UID |
| Datasource health | GET /api/datasources/uid/{uid}/health | Check installed schema | Some plugins don't implement |
| Query datasource | POST /api/ds/query | Check installed schema | POST performs a query, not a CRUD write; can be expensive |
| Provisioned alert rules | GET /api/v1/provisioning/alert-rules | New alerting APIs depend on component/version | Export differs from mutation JSON |
| Alert contact points | GET /api/v1/provisioning/contact-points | Check new alert notification endpoints | Contact information sensitive |
| Org members | GET /api/org/users | Check installed schema | Permission required |
| Instance-wide users | /api/users | Legacy server Admin endpoint | Org Admin not enough |

Docs:
- New dashboards: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/dashboard/
- New folders: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/folder/
- Datasources: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/
- Legacy search: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/folder_dashboard_search/
- Dashboard version history: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/dashboard_versions/
- Alerting provisioning: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/alerting_provisioning/
- RBAC introspection: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/access_control/

## /apis resource envelope versus /api legacy JSON

New dashboard APIs (Grafana 12+, check target version) use a
Kubernetes-style object with metadata.name (dashboard UID),
metadata.namespace, annotations (including folder UID), and spec
(dashboard JSON). Example READ:

GET /apis/dashboard.grafana.app/v1/namespaces/default/dashboards/{uid}

The legacy GET /api/dashboards/uid/{uid} response wraps dashboard JSON
as dashboard and metadata as meta (folder/provisioning/permissions).
Never send legacy POST /api/dashboards/db request bodies to /apis
resource routes. Inspect installed namespace conventions rather than
assuming a namespace always equals an organization.

Version behavior:
- Grafana 12 introduced new dashboards/folders APIs; feature flags
  and patch versions can influence what is available.
- Grafana 13 deprecates legacy /api routes; many remain functional.
  Not every old resource has a new replacement.
- Use the instance's version-specific API docs and /swagger-ui.
- Traverse all list pages; partial inventory is incomplete.

## Credential and scope guidance

Grafana origin: https://grafana.bkk.redeyeninja.com
Staged credential: /srv/turnstone/credentials/grafana/api_token

- Operator states an org-Admin service-account token was staged; its
  live API access has NOT yet been verified.
- File is root:root 0600 and Turnstone OS user is unprivileged.
  This is intentional. Never distribute plaintext for convenience.
- Use Authorization: Bearer <token> to the pinned TLS-verified origin.
- GET /api/access-control/user/permissions shows effective privileges
  where RBAC introspection exists; role label alone is not proof.
- Org Admin does not imply Grafana server Admin, host SSH, or permission
  to override provisioned resources.
- No automatic reduction or revocation of operator granted privileges.
- Grafana API key is NOT a Prometheus/Loki/Alloy infrastructure key.

## Safe dashboard redeployment

1. Discover org and token scope, then enumerate dashboards/folders
   completely with version-aware API and pagination.
2. Snapshot affected dashboards: UID, folder UID, version, JSON, panels,
   library elements, datasource UIDs, variables, annotations, checksum.
   Store sensitive snapshots in authorized locations only.
3. Identify provisioned dashboards separately; their source may be
   infrastructure provisioning, not the HTTP write API.
4. Inspect Prometheus and Loki datasource UID/type/health, and actual
   metrics/query schemas. Don't guess datasources or series names.
5. Validate PromQL/LogQL, labels, null/stale behavior, table joins,
   variables, panel IDs, units, legends, thresholds and time zones.
6. Produce an explicit per-resource / per-panel diff. Preserve IDs,
   folders, existing user modifications and permissions.
7. Request operator GO for the exact approved deployment scope.
8. Apply bounded changes, checking dashboard version for concurrent
   edits; refuse silent overwrite. Avoid mutation if upstream state
   changed since snapshot.
9. Read back deployed resources; verify UIDs, versions, folder, panel
   counts, datasource references, and real panel-backed query results.
10. Preserve rollback snapshots and present a detailed acceptance receipt.

## Prometheus / Loki / Alloy boundaries

Prometheus direct routes include /api/v1/query, /api/v1/query_range,
/api/v1/targets, /api/v1/series, and /api/v1/label/{label}/values.
Loki direct routes include /loki/api/v1/query, /loki/api/v1/query_range,
/loki/api/v1/labels and /loki/api/v1/label/{label}/values.
These are NOT Grafana API routes without datasource proxy integration.
Prometheus and Loki may require distinct authenticated access and
query budgets. Alloy collection config/targets/scrape/push behavior
must be verified through authorized host-management access.

For Grafana-managed alerts: the export format differs from its
provisioning update payload. Preserve rule UID/provenance, folder,
group, contact point, notification policy, mute timings and
evaluation cadence. Do not assume all new alert APIs replace the
legacy provisioning interface.

## Permission and error classification

- 200 /api/health: instance alive only; NOT token auth.
- 200 /api/org/: token accepted in that organization.
- 200 permissions list: inspect actual allowed scopes.
- 401: expired/invalid token; report and request corrective action.
- 403: action/target not allowed; report exact shortfall, no silent
  privilege changes.
- 404: absent resource OR incompatible version. Investigate.
- 409/412: conflict/race. Stop, compare, reconcile.
- 429/5xx: bounded retries; don't repeat non-idempotent writes.
- Redirect: never forward Authorization to different origin.
- Logs/reporting: do not print token, full datasource JSON, contact
  information, or other sensitive response bodies unnecessarily.

## Future Switchyard dashboard project

When separately authorized:
- Inventory source Grafana dashboards/versions/UIDs first.
- Read Switchyard authoritative route manifest; compare to live metrics.
- Distinguish smart, bounded, local, tiered and direct routes.
- Validate metrics labels and values at real Prometheus data sources;
  route names do not prove execution or served-model identity.
- Reconcile Alloy scraping on agent-vm and source exporters on
  switchyard-vm, with Loki logs and Prometheus metrics kept distinct.
- Scope and deploy dashboard changes with rollback and independent
  query/render/read-back verification.

## Skill installation acceptance

This file in Git is documentation, not a live Turnstone skill.
Install via Turnstone native Skills UI/API and select it in a new
workstream. Explicit selection replaces default skills in 1.8.7, so
verify workstream policies before activating.
The credential file alone is not an active API connection. Install
or expose an authenticated Grafana tool/adapter separately and test it.
Skill acceptance: recognizes official version-specific endpoints,
understands Admin role boundary, never leaks token, handles paging and
version conflicts, can interpret probe results, and never claims a
dashboard changed without read-back evidence.
