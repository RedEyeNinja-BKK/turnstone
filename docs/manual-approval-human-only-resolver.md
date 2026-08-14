# Manual Approval — Human-Only Resolver Boundary (Gate III-B-F)

**Scope:** native Turnstone correction for the strict-manual human-resolution
boundary, identified during Gate III-B-I investigation.

## Background

The `manual` tool-policy action routes a protected invocation to a fresh live
human ApprovalCycle. The enforcement gate itself was proven working
(fresh cycle, Smart Approval bypass, stale-Always bypass, deny→zero execution,
argument digest). However, two native defects were identified:

1. **Accidental-approval UX (Defect A):** the interactive and coordinator UIs
   could turn generic keyboard activity into an Approve for a manual card
   (auto-focused feedback field + Enter-approves; bare `y`/Enter; approve-all
   shortcut; auto-focused Approve button + Enter).
2. **Resolver authority not structurally human-only (Defect B):**
   `require_any_permission(..., allow_service_bypass=True)` default + the
   approve endpoint accepting `tools.approve OR admin.coordinator` meant a
   service-scoped machine principal (or coordinator automation with
   `admin.coordinator`) could pass the endpoint authority gate without a
   qualifying human.

The causal trigger of the observed `approved:true` resolutions (B1/B2) remains
**inconclusive** — the physical client input is not recoverable from retained
evidence. The two defects are independent of that causal question.

## Human-only resolver invariant (server)

For an approval cycle containing a winning `manual` item, the approve handler
now enforces:

- `tools.approve` is **required**;
- `admin.coordinator` alone is **not** sufficient;
- generic **service-scope bypass is rejected** (`allow_service_bypass` is
  overridden for manual cycles);
- coordinator/service automation (token_source `coordinator`, `console`,
  `cli`, etc.) is **rejected**;
- ambiguous/missing provenance **fails closed** (401/403).

Qualifying human paths: direct browser (`token_source="jwt"`), or
console-proxied human browser (`token_source="console-proxy"` — the console
re-mint preserves the user's identity and source for human-originated
proxied calls).

Ordinary non-manual `ask` approvals retain their existing semantics
(`admin.coordinator` and service-bypass behavior unchanged).

## Resolver provenance evidence

`tool.manual_resolved` audit rows now include:

- `resolver_source` / `resolver_token_source` — the authenticated authority
  path (JWT src claim: `jwt`, `console-proxy`, `coordinator`, `console`, …);
- `resolver_service_scope` — whether the resolver carried service scope.

These are forensic/reconciliation fields. They do **not** prove physical
humanness; the authorization decision is server-enforced.

## Deliberate-gesture UX (client)

For `manual` approval cards/batches:

- no auto-focus of the feedback field (interactive) or Approve button
  (coordinator);
- bare Enter / `y` / approve-all (`a` / Shift+A) never approve;
- Enter in an auto-focused feedback field never approves;
- the **only** keyboard approval is deliberate activation of an explicitly
  focused Approve control (`conv-btn--approve` via Tab + Enter/Space);
- pointer click on the explicit Approve button remains acceptable;
- Deny remains easy (Escape / `n` / `d`) — fail-closed.

Keyboard accessibility is preserved (Tab to Approve + Enter/Space works; no
mouse required).

## Files changed

- `turnstone/core/session_routes.py` — manual-cycle human-only resolver gate +
  resolver provenance extraction.
- `turnstone/core/session_ui_base.py` — resolver provenance params on
  `resolve_approval` / `_emit_manual_resolutions` + audit fields.
- `turnstone/shared_static/interactive.js` — manual-card deliberate-gesture
  keydown + no auto-focus + `conv-batch--manual` marker.
- `turnstone/console/static/coordinator/coordinator.js` — manual-batch
  deliberate-gesture keydown + no auto-focus + `conv-batch--manual` marker.

## Tests

- `tests/test_manual_resolver_human_only.py` — handler-level E2E: service /
  coordinator / admin.coordinator-only / missing-tools.approve / unauth
  rejected; human direct + console-proxy allowed; non-manual ask semantics
  unchanged.
- `tests/test_manual_card_ux_js.py` — source-structure guards for the
  deliberate-gesture UX in both panes.

## Residual

- `always_suppressed` (the Always-persistence suppression for manual cycles)
  remains structurally tested only — no live `always=true` manual resolution
  has been exercised. This is unchanged by this gate.
- The accidental-approval UX fix is a **prevention**; it does not retroactively
  prove what caused B1/B2. The causal trigger remains inconclusive.
- No browser E2E tooling was run (per gate).
