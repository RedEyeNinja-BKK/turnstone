# Manual Approval — Eligible Operator-Session Resolver Boundary (Gate III-B-F / R1)

**Scope:** native Turnstone correction for the strict-manual human-resolution
boundary, identified during Gate III-B-I investigation and corrected by
independent review (R1).

## Background

The `manual` tool-policy action routes a protected invocation to a fresh live
human ApprovalCycle. The enforcement gate itself was proven working
(fresh cycle, Smart Approval bypass, stale-Always bypass, deny→zero execution,
argument digest). Two native defects were identified and corrected:

1. **Accidental-approval UX (Defect A):** the interactive and coordinator UIs
   could turn generic keyboard activity into an Approve for a manual card.
2. **Resolver authority not structurally human-only (Defect B):**
   `require_any_permission(..., allow_service_bypass=True)` default + the
   approve endpoint accepting `tools.approve OR admin.coordinator` meant a
   service-scoped machine principal could pass the endpoint authority gate.

R1 (independent review) corrected a provenance over-admission in the initial
PR #4 predicate (`jwt`/`database`/`password`/`console-proxy`): only
`password` and `oidc` are eligible DIRECT human-session sources, `database`
(API token) and ambiguous `jwt` fail closed, and `console-proxy` requires a
trusted signed `orig_src` claim.

## Canonical AuthResult.token_source provenance

| token_source | Origin | Eligible for manual? |
|---|---|---|
| `password` | username/password login (`handle_auth_login` → `AuthResult(token_source="password")` → JWT `src="password"`) | **YES (direct operator session)** |
| `oidc` | OIDC/SSO callback (`create_jwt(source="oidc")` → JWT `src="oidc"`) | **YES (direct operator session)** |
| `database` | direct `ts_...` API-token auth (`_authenticate_api_token` → `token_source="database"`); also preserved when an API token is exchanged through `/api/auth/login` (JWT `src="database"`) | **NO — automation-capable** |
| `jwt` | `validate_jwt` default `payload.get("src", "jwt")` — a signed JWT with no explicit `src` claim | **NO — ambiguous, fail closed** |
| `console-proxy` | console `_proxy_auth_headers` re-mint for non-coordinator, non-service callers | **ONLY IF signed `orig_src` ∈ {password, oidc}** |
| `coordinator` | coordinator-minted JWT (`token_source="coordinator"`, preserves `coord_ws_id`) | **NO — automation** |
| `console` | console service identity (only with `service` scope) | **NO — service** |
| `cli` | CLI-origin | **NO — automation** |
| `service` scope | machine/service superset scope (bypasses RBAC) | **NO — unconditional rejection** |
| blank/unknown | — | **NO — fail closed** |

## Eligible operator-session predicate (server)

For a cycle containing a winning `manual` item, the approve handler enforces
(via the canonical helper `turnstone.core.auth.is_eligible_manual_resolver`):

- `tools.approve` is **required**;
- service scope is **unconditionally rejected** (overrides the normal
  `require_any_permission` service bypass);
- direct eligible sources: `password`, `oidc`;
- proxied eligible source: `token_source == "console-proxy"` **AND** the
  signed `orig_src` claim (minted by the trusted console proxy) ∈
  {password, oidc};
- rejected: `database` (even with `tools.approve`), ambiguous `jwt`,
  `coordinator`, `console`/service, `cli`, blank/unknown, and any
  `console-proxy` without a trusted human `orig_src`;
- `admin.coordinator` alone remains insufficient;
- ambiguous/missing provenance fails closed (401/403).

Ordinary non-manual `ask` approvals retain their existing semantics
(`admin.coordinator` and service-bypass behavior unchanged).

## Console-proxy trusted-origin attestation

`_proxy_auth_headers` now mints the node-facing JWT with an `orig_src` claim
equal to the OUTER request's `token_source`. This is a signed claim inside
Turnstone's existing JWT (never a client header/body field), so a generic
caller cannot self-assert the trusted human-origin claim. The node validates
it through the existing signed-JWT mechanism: a proxied API-token/service/
coordinator/ambiguous caller emerges with `orig_src` = `database`/`console`/
`coordinator`/`jwt` and is rejected for manual cycles.

## Resolver provenance evidence

`tool.manual_resolved` audit rows now include:

- `resolver_source` / `resolver_token_source` — node-visible authenticated
  source (JWT `src` claim);
- `resolver_orig_src` — trusted outer source for console-proxied resolvers;
- `resolver_service_scope` — whether the resolver carried service scope;
- existing: `user_id`, `call_id`, `cycle_id`, `arg_digest`,
  `always_suppressed`.

These are forensic/reconciliation fields (no secrets; no JWTs/cookies/tokens
stored). They do **not** prove physical humanness; authorization is
server-enforced. IP/User-Agent/Origin/Referer/client gesture strings are never
used as authorization evidence.

## Deliberate-gesture UX (client)

For `manual` approval cards/batches (marked `conv-batch--manual`):

- no auto-focus of the feedback field (interactive) or Approve button
  (coordinator);
- bare Enter / `y` / approve-all (`a` / Shift+A) never approve;
- Enter in an auto-focused feedback field never approves;
- the **only** keyboard approval is deliberate activation of an explicitly
  focused Approve control (`conv-btn--approve` via Tab + Enter/Space);
- pointer click on the explicit Approve button remains acceptable;
- Deny remains easy (Escape / `n` / `d`) — fail-closed;
- ordinary non-manual `ask` keyboard semantics unchanged.

## Files changed (PR #4, R1)

- `turnstone/core/auth.py` — canonical `is_eligible_manual_resolver` +
  `ELIGIBLE_HUMAN_TOKEN_SOURCES`; `orig_src` preserved as extra claim through
  `validate_jwt` (non-reserved).
- `turnstone/console/server.py` — `_proxy_auth_headers` mints `orig_src` claim
  (trusted outer provenance) on the proxy re-mint.
- `turnstone/core/session_routes.py` — manual-cycle eligible-operator-session
  gate + resolver provenance extraction (incl. `orig_src`).
- `turnstone/core/session_ui_base.py` — resolver provenance params on
  `resolve_approval` / `_emit_manual_resolutions` + audit fields.
- `turnstone/shared_static/interactive.js`, `turnstone/console/static/coordinator/coordinator.js`
  — deliberate-gesture keydown + no auto-focus + `conv-batch--manual` marker.
- `tests/test_manual_resolver_human_only.py` — handler-level E2E + real-JWT
  integration + console-proxy provenance tests.
- `tests/test_manual_card_ux_js.py` — source-structure guards (ordering-pinned).
- `docs/manual-approval-human-only-resolver.md` — this document.

## Tests (R1)

- `tests/test_manual_resolver_human_only.py` — 42 tests: handler-level
  (password/oidc allowed; database/jwt/coordinator/console/cli/blank/service/
  admin.coordinator-only/missing-tools.approve/unauth rejected; console-proxy
  requires trusted orig_src; deny allowed for eligible human), real
  `create_jwt`/`validate_jwt` integration (password/oidc/database/jwt-default/
  service/coordinator/console-proxy-with-and-without-orig), and console-proxy
  provenance end-to-end (password/oidc origin allowed; database/service/
  coordinator/ambiguous/missing origin rejected; forged proxy token without
  orig_src fails closed).
- `tests/test_manual_card_ux_js.py` — 15 source-structure guards, ordering-pinned
  (manual branches precede legacy approve; deliberate-control precedes
  manual-deny; returns before fall-through).
- Focused regression: **851 passed** (manual-policy, tool-policy,
  session_ui_base, session_routes, resolver-human-only, card-ux, auth,
  auth_identity, console_routing_proxy, coordinator_proxy_auth, web_helpers,
  console, openapi).
- Full local non-live suite (LOCAL regression evidence, NOT GitHub CI) on runtime-source head 1149db44: **12052 passed, 14 skipped, 8 deselected in 3445.61s (0:57:25), exit 0**.
- ruff check clean; ruff format clean; mypy clean on affected Python.

## Residual

- `always_suppressed` (Always-persistence suppression for manual cycles)
  remains structurally tested only — no live `always=true` manual resolution
  exercised. Unchanged by this gate.
- The accidental-approval UX fix is **prevention**; it does not retroactively
  prove what caused B1/B2 (causal trigger inconclusive).
- The JS tests are source-structure guards, not behavioral DOM tests (no
  jsdom/vitest harness in the repo; browser E2E tooling excluded per gate).
- `database`/API-token provenance cannot qualify even with `tools.approve`;
  ambiguous `jwt` cannot qualify; these are server-enforced, not label-only.
