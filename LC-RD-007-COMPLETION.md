# LC-RD-007 — direct OpenAI OAuth for three registry routes

Status: **code staged and verified; three routes NOT yet migrated.** Production
activation requires the operator GO. Nothing below has been run against
production.

## The requirement

Three Turnstone registry aliases must use direct OpenAI OAuth, independently of
Switchyard, over two underlying model identities:

| Alias | Provider model | Reasoning | Served identity |
|---|---|---|---|
| `gpt-6-luna` | `gpt-6-luna` | thinking enabled | `gpt-6-luna` |
| `gpt-6-luna-nt` | `gpt-6-luna` | none emitted | `gpt-6-luna` |
| `gpt-6.1-sol` | `gpt-6.1-sol` | (default) | `gpt-6.1-sol` |

## Measured "before" (2026-10-17, live registry)

All three route through Switchyard, and `gpt-6.1-sol` was mapped to the wrong
model version:

| Alias | provider | model | base_url | auth |
|---|---|---|---|---|
| `gpt-6-luna` | `openai-compatible` | `direct-openai-gpt-6-luna` | `https://switchyard-vm.tailc8ef7b.ts.net/v1` | `static` |
| `gpt-6-luna-nt` | `openai-compatible` | `direct-openai-gpt-6-luna-nt` | same | `static` |
| `gpt-6.1-sol` | `openai-compatible` | **`direct-openai-gpt-6-sol`** | same | `static` |

`model.auth_audience_allowlist` was empty, `surface_persisted_reasoning` false on
`-nt` only, and every row carried a static Switchyard key.

A full byte-level snapshot, including the stored keys, is at
`/var/lib/turnstone/route-snapshots/before.json` (0600).

## Target configuration

| Field | Value |
|---|---|
| provider | `openai` (Responses surface) |
| base_url | `https://api.openai.com/v1` |
| auth_mode | `openai_oauth` |
| obo_audience | `https://api.openai.com/v1` (the OAuth resource) |
| model | `gpt-6-luna` / `gpt-6-luna` / `gpt-6.1-sol` |
| reasoning_effort | `medium` / `none` / `medium` |
| credential | shared runtime store `…/runtime/openai-siwc` |
| context_window, enabled | preserved |

## Activation order — strictly this sequence

```bash
REPO=/var/lib/turnstone/github/turnstone

# 0. Read-only: confirms the pre-state and that nothing is half-installed.
bash "$REPO/scripts/stage_openai_oauth_v187.sh" plan
python3 "$REPO/scripts/migrate_openai_routes.py" status
bash "$REPO/scripts/stage_siwc_handoff.sh" check

# 1. CODE ACTIVATION — operator GO required.
sudo bash "$REPO/scripts/stage_openai_oauth_v187.sh" activate --confirm

# 2. SINGLE-WRITER CREDENTIAL HANDOFF — before any direct-model request.
sudo bash "$REPO/scripts/stage_siwc_handoff.sh" apply --confirm
bash "$REPO/scripts/stage_siwc_handoff.sh" verify

# 3. REGISTRY MIGRATION — the three aliases, allow-list added additively.
python3 "$REPO/scripts/migrate_openai_routes.py" apply --allow-residual-key
python3 "$REPO/scripts/migrate_openai_routes.py" verify

# 4. Ordinary model checks
#    gpt-6-luna      : stream a turn, reasoning present
#    gpt-6-luna-nt   : stream a turn, ZERO reasoning items/deltas
#    gpt-6.1-sol     : Responses request with one client-side tool call,
#                      served model reported as gpt-6.1-sol
```

Steps 2 and 3 are refused by their own preconditions if run out of order:
`migrate … apply` exits 2 while the live console does not accept
`auth_mode=openai_oauth`, and the handoff refuses to run while a refresh-capable
process is alive.

### Rollback

| Layer | Command |
|---|---|
| Registry rows | restore from `/var/lib/turnstone/route-snapshots/before.json` (full rows, including keys) |
| Code | `sudo bash scripts/stage_openai_oauth_v187.sh rollback --confirm` |

The credential archive (`siwc-creds.archived-<stamp>`) is deliberately not
auto-restored: reviving a rotated refresh token is what breaks the grant. If the
runtime store is unusable, re-authorize instead.

## Two constraints that are properties of the running system, not defects

**1. The static API key cannot be cleared through the admin API.** The update
handler treats `""` and `"***"` as "keep the existing value", so each migrated
row retains its old Switchyard key. It is inert: the `openai_oauth` dispatch
never reads `api_key` and fails closed unconditionally, so it can neither be
sent nor used as a fallback. It does remain at rest. `apply` requires
`--allow-residual-key` to acknowledge that. Clearing the column needs a direct
DB edit with the operator's approval.

**2. The `-nt` contract depends on a provider capability that is unverified
until a live call.** Turnstone forwards `reasoning.effort="none"` only when the
row DECLARES a `"none"` level (`capabilities.reasoning_effort_values`); without
that declaration it omits the parameter and the server default (possibly
reasoning ON) governs. The migration therefore declares `["none"]`. Whether the
provider accepts it for `gpt-6-luna` can only be proven by a request, which
needs the handoff. If the provider rejects it, the honest outcome is a reported
constraint — not a silent re-route through Switchyard and not a claimed success.
