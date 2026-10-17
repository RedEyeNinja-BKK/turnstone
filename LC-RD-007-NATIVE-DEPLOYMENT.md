# LC-RD-007 — native OpenAI OAuth: deployment package

Status: **revision 2 candidate staged and verified in sandbox. Not activated.** The
Switchyard-backed `gpt-6.1-sol` alias remains authoritative.

## 1. What this package changes

`auth_mode="openai_oauth"` lets a model row take its credential from the
coordinated local token store instead of an OIDC exchange. Introducing that mode
requires **four cooperating changes**. Shipping any subset produces a row the
registry accepts and the layers beneath refuse, so the staging script copies all
four and then asserts the complete set:

| # | File | Change |
|---|---|---|
| 1 | `core/model_registry.py` | `openai_oauth` in `MODEL_AUTH_MODES`; `DIRECT_OAUTH_MODEL_AUTH_MODES`; deliberately **not** in `MODEL_AUTH_MODE_PROFILES` |
| 2 | `core/model_backend_auth.py` | `_resolve_direct_oauth_token` branch, **before** the delegated-user check |
| 3 | `core/oauth_token_store.py` | the coordinated store (flock + re-read under lock + atomic replace) |
| 4 | `console/server.py` | posture tier: this mode needs the local store, not OIDC/OBO (copied whole; it also carries the R2 picker guard) |

Plus `core/settings_registry.py` (`model.openai_oauth_store`) and the accepted R2
typed-decision content, which the package inherits rather than duplicating.

R2 and this package are deliberately disjoint at the file level: R2 *patches*
`model_registry.py` and `console/server.py` with only the typed-decision changes
and its verify refuses any `openai_oauth` reference; this package then *copies*
the full repository versions on top. That is what makes "R2 alone" and "R2 +
OAuth" both internally consistent, instead of R2 shipping half a feature.

### Why the mode is not given an OBO "mint leg"

`MODEL_AUTH_MODE_PROFILES` pairs a mode with an `OBO_GRANT_PROFILES` leg
(`entra`/`rfc8693`). A direct-account OAuth refresh is not an Entra exchange.
Inventing a leg would place a non-OBO credential in the OBO cache, keyed and
invalidated under rules that do not describe it. The registry invariant is
therefore *"every dynamic mode **except** direct-OAuth names a real mint leg"* —
the strongest true statement, and the "every profile has a leg" half is unchanged.

### Why the console needed touching

`_validate_dynamic_model_auth` refuses **every** dynamic mode unless an MCP token
store exists, `[oidc]` is configured, and `obo_grant_profile` is valid. Without the
change, saving an `openai_oauth` row fails with *"single sign-on is not set up"* —
a misleading error for a mode that never touches OIDC.

One production consequence to plan for: the audience (`https://api.openai.com/v1`)
must still be in `model.auth_audience_allowlist`, because that check is
value-based and runs for every dynamic mode.

## 2. Staged verification (sandbox, real 1.8.7 bytes)

```
completeness: all four cooperating changes present
R2 content:  preserved
import provenance: all modules from <slot>/turnstone
    10 passed  [test_openai_oauth_auth_mode]
    28 passed  [test_oauth_token_store]
    13 passed  [test_generative_alias_exclusion]
    34 passed  [test_output_guard_typed_decision]
     7 passed  [test_direct_oauth_request_profile]
VERIFY OK
```

Negative controls (the gate must refuse, and does):

| Removed | Result |
|---|---|
| `core/oauth_token_store.py` | `REFUSED: missing the credential store module` |
| console posture guard | `REFUSED: admin write path still demands OIDC/OBO posture for this mode` |

## 2b. Reconciliation of the root staging receipts (2026-10-17)

Independently re-measured against the staged bytes, not inferred from the
receipt text:

| Claim | Measurement |
|---|---|
| `openai-oauth-v187` (rev 1) is a superset of the R2 slot | 263 `.py` vs 262; **no file present in R2 is absent**; exactly 4 files differ (`model_registry`, `model_backend_auth`, `settings_registry`, `console/server`) + 1 added (`oauth_token_store.py`) |
| R2 content preserved | the 4 differing files contain only OAuth additions; all other R2 files are byte-identical |
| all four cooperating changes | 14/14 structural markers present; the dispatch branch precedes the delegated-user check |
| syntax | 263/263 files compile |
| import provenance | `turnstone`, `core.model_registry`, `core.model_backend_auth`, `console.server` all resolve under the slot; `MODEL_AUTH_MODES` includes `openai_oauth` |
| superset of the *running* tree | no file in `typed-decision-v187-finish` is missing from the slot |

Known inert delta: `core/settings_registry.py` is copied whole from the repository,
so it also registers `model.auto_continue_truncated`, `tools.rerank_candidate_pool`
and `tools.rerank_timeout_cap_s`. Their implementation is *not* in this slot, so
they change no behaviour; they are visible in the Settings tab and do nothing.
They were not stripped because the slot is verified and immutable and is not
being restaged. `model.openai_oauth_store` is the only intended new setting.

## 2c. Revision 2 - security corrections (2026-10-17)

Revision 1 is preserved and unmodified. Two findings required application
changes, so the package moves to a new immutable slot
(`/opt/turnstone-overlays/openai-oauth-v187-r2`) with `94-` drop-ins.

| Finding | Correction | Pinned by |
|---|---|---|
| Fail-closed was conditional on `api_key`/`model.auth_fail_closed`; a missing resource returned `None` (= "use the static key") | Direct-OAuth failure is unconditional; resource must be present and exact | `test_static_key_never_enables_a_fallback`, `test_global_fail_closed_flag_is_irrelevant`, `test_missing_resource_refuses_instead_of_returning_none` |
| The bearer was not bound to an endpoint | Shared predicate + dispatch check: exact resource, provider `openai`, exact https base_url, no userinfo/query/fragment/port; redirects disabled on this lane's transport; Console create **and** update call the same predicate on the post-merge row | `test_alternate_host_is_refused`, `test_plain_http_is_refused`, `test_trailing_slash_variant_is_refused`, `test_credential_bearing_url_is_refused`, `test_non_default_port_is_refused`, `test_compat_provider_is_refused`, `test_direct_oauth_client_disables_redirects` |
| Direct-OAuth-only deployments inherited the MCP token-store boot requirement | Guards read `has_token_store_dynamic_auth()`; mixed registries still require it | `test_direct_oauth_only_does_not_require_the_mcp_token_store`, `test_entra_still_requires_the_mcp_token_store` |
| Inert settings arrived with the whole-file `settings_registry.py` copy | The staging step patches in only `model.openai_oauth_store`; verify refuses an inert row | verify step `settings: model.openai_oauth_store only` |

Both negative families were mutation-checked: restoring the old semantics makes
the new tests fail (`2 failed` for the fallback mutation, `4 failed` for the
binding mutation), and the restored tree is green again.

Staged verification (rev 2, sandbox, real 1.8.7 base):

```
completeness: all four cooperating changes present
settings: model.openai_oauth_store only (no inert rows)
R2 content: preserved
import provenance: all modules from <slot>/turnstone
    29 passed  [test_openai_oauth_auth_mode]
    28 passed  [test_oauth_token_store]
    13 passed  [test_generative_alias_exclusion]
    34 passed  [test_output_guard_typed_decision]
     7 passed  [test_direct_oauth_request_profile]
    14 passed  [test_combined_activation]
     9 passed  [test_activation_failure_paths]
VERIFY OK
```

## 3. Canary evidence already obtained

In-process, through the **new dispatch seam** (`registry → resolve_model_backend_auth_token
→ store → Responses`), not the probe's own client:

```
turn 1: response.completed, tool_calls=['get_weather']
turn 2: response.completed, "Bangkok: 33°C and clear."
served_model='gpt-6.1-sol'
```

Still required before cutover: a **fresh-workstream** canary through Turnstone,
which needs this package active.

## 4. Activation sequence (operator-gated)

The switch is implemented in the staging script so the same bytes that were
staged are the bytes that get activated:

```bash
# 0. Read-only inspection (no privilege, no writes)
bash scripts/stage_openai_oauth_v187.sh plan

# 1. Re-confirm the candidate (re-runs the acceptance suites)
sudo bash scripts/stage_typed_decision_v187_r2.sh verify
sudo bash scripts/stage_openai_oauth_v187.sh verify

# 2. Optional: exercise the full preflight without writing
bash scripts/stage_openai_oauth_v187.sh activate --confirm --dry-run

# --- approval boundary ---

# 3. One restart window
sudo bash scripts/stage_openai_oauth_v187.sh activate --confirm

# Rollback, if needed
sudo bash scripts/stage_openai_oauth_v187.sh rollback --confirm
```

The script refuses to activate without the literal `--confirm` and without root.
It writes the three `94-` drop-ins (all-or-nothing), records a receipt
under `/var/log/turnstone-v187/` **before** the switch, `daemon-reload`s, then
restarts `turnstone-server`, `turnstone-node@2..5` and `turnstone-console` in a
single `systemctl restart`. It declares success only after, for **every** unit:

* `is-active` is `active`,
* the live process's `PYTHONPATH` equals the slot (proven from `/proc/PID/environ`,
  not inferred from the drop-in),
* `GET /health` returns 2xx on 8080, 8082–8085 and 8090,
* the journal since the restart contains no `ModuleNotFoundError`, `ImportError:`
  or traceback.

Any failure triggers automatic rollback (remove the three drop-ins,
`daemon-reload`, restart the same units) and a non-zero exit.

The gate itself is pinned by `tests/test_combined_activation.py` (13 executed,
0 skipped on this host): the refusals and the dry run are executed, and the
restart/provenance/rollback wiring is asserted structurally.

**Scope of this switch.** It changes code only. No model row, credential, setting
or database row is touched, and no alias is repointed. `auth_mode=openai_oauth`
remains unreachable until a canary row is deliberately created (section 5). The
SIWC runtime store and the audience allow-list are prerequisites for that
**canary**, not for this activation.

**Rollback** removes only the three `94-` drop-ins. The `90-`/`91-` anchors restore
`typed-decision-v187-finish`, which is what is running today. There is **no `92-`
anchor on this host** because the R2 slot was never activated on its own; if it
ever is, `92-` sorts before `94-` and removing `94-` would land on R2 instead.
Both `92-` and the superseded `93-` family are refused at preflight for that reason.
Model rows are untouched by activation and rollback.

## 5. Alias cutover (a separate, later gate)

The canary is a **new** alias; the existing `gpt-6.1-sol` row keeps pointing at
Switchyard until the canary passes.

1. Add a canary row, e.g. `gpt-6.1-sol-native`:
   `provider=openai`, `model=gpt-6.1-sol`,
   `base_url=https://api.openai.com/v1`,
   `auth_mode=openai_oauth`, `obo_audience=https://api.openai.com/v1`,
   `api_key=''` (keyless ⇒ fail-closed), `context_window` as measured.
2. Prove a fresh workstream on the canary: streaming, a client-side function call,
   multi-turn continuity, reasoning effort (never `none`), and
   `served_model=gpt-6.1-sol`.
3. Only then repoint the `gpt-6.1-sol` alias. Keep the Switchyard row's exact
   configuration recorded for rollback.
4. **No silent fallback to GPT-6 Sol.** If the native route fails, the alias must
   fail visibly; the Switchyard row is restored deliberately, not automatically.

`gpt-6-luna` and `gpt-6-luna-nt` are not touched by this package or the cutover.

## 6. Credential cutover (single-writer handoff)

The working store `/var/lib/turnstone/github/siwc-creds` stays authoritative until
this package is live. Two stores holding one grant is the failure mode this module
exists to prevent: the first refresh retires the token the other store still
holds.

**Measured state (2026-10-17, before handoff):**

| Fact | Value |
|---|---|
| `credentials.json` in both stores | byte-identical, `sha256 2e228446…` |
| `host_id` in both stores | byte-identical, `sha256 7d66be05…` |
| `resource` | `https://api.openai.com/v1` |
| access token | **already expired** (`expires_at` 3.9 h in the past) |
| refresh token | present, not yet rotated |

Because the access token is expired, the **first** call through either store
performs a refresh and rotates the refresh token. The handoff therefore cannot
be deferred until after the canary starts: it must complete before the first
call on the new path.

**Handoff - one step, in this order:**

1. Confirm both copies are still byte-identical. If a refresh has happened since
   the date above, the copies have diverged: stop, and copy the *writer's* file
   over the runtime store rather than assuming.
2. Archive the old store without deleting it - `mv siwc-creds
   siwc-creds.archived-<date>`. Renaming removes it as a refreshable path while
   preserving the bytes; do not `chmod` in place, because the probe script's
   default still points there.
3. Re-point every consumer at the runtime store
   (`/srv/turnstone/credentials/runtime/openai-siwc`, or the
   `model.openai_oauth_store` setting) in the same step. The probe script
   `scripts/siwc_direct_oauth_probe.py` defaults to the archived path
   (`SIWC_STORE_DIR` overrides it) - set it, or it becomes a second writer.
4. Never revive a rotated token from the archive.

Only then create the canary row.

## 7. Audience allow-list (additive)

`model.auth_audience_allowlist` is a comma/newline-separated list of exact
resources. The direct-OAuth row needs `https://api.openai.com/v1` added; existing
entries must be preserved.

```bash
# Read the current value (deployment-wide state; not in the service file store)
CURRENT=$(curl -sS -H "Authorization: Bearer $TURNSTONE_ADMIN_TOKEN" \
  http://127.0.0.1:8090/v1/api/admin/settings \
  | jq -r '.settings[] | select(.key=="model.auth_audience_allowlist") | .value')

# Union: append, de-duplicate, never reorder or drop.
NEW_VALUE=$(printf '%s\n%s\n' "$CURRENT" "https://api.openai.com/v1" \
  | tr ',' '\n' | sed '/^[[:space:]]*$/d' | awk '!seen[$0]++' | paste -sd, -)

curl -sS -X PUT -H "Authorization: Bearer $TURNSTONE_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc --arg v "$NEW_VALUE" '{value:$v}')" \
  http://127.0.0.1:8090/v1/api/admin/settings/model.auth_audience_allowlist
```

Re-read afterwards and confirm every previous entry is still present before
creating the canary row. This write needs the `admin.settings` scope and is
audited. It belongs to the canary gate, not to code activation.

## 8. Rollback target, measured on the live host

| Unit | Current `PYTHONPATH` | Rollback expectation |
|---|---|---|
| `turnstone-server` | `typed-decision-v187-finish` | same |
| `turnstone-node@2..5` | `typed-decision-v187-finish` | same |
| `turnstone-console` | *(none)* | none |
| `92-*` drop-ins | **none present** | must stay absent |
| `93-*` / `94-*` drop-ins | **none present** | removed by rollback |

Measured with `systemctl show -p Environment` and a directory listing of the
three `.d` paths on 2026-10-17. `rollback_core` asserts exactly this shape and
returns nonzero if it cannot prove it.
