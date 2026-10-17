# LC-RD-007 — native OpenAI OAuth: deployment package

Status: **candidate staged and verified in sandbox. Not activated.** The
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

## 4. Activation sequence (operator-gated — NOT run)

```bash
# 0. Prerequisites
#    - SIWC runtime store live at /srv/turnstone/credentials/runtime/openai-siwc
#    - R2 staged and verified
#    - audience allow-list includes https://api.openai.com/v1
#      (setting: model.auth_audience_allowlist)

sudo bash scripts/stage_typed_decision_v187_r2.sh stage
sudo bash scripts/stage_typed_decision_v187_r2.sh verify
sudo bash scripts/stage_openai_oauth_v187.sh stage
sudo bash scripts/stage_openai_oauth_v187.sh verify

# --- approval boundary ---

sudo install -d -o root -g root -m 0755 /etc/systemd/system/turnstone-node@.service.d
for f in /etc/systemd/system/turnstone-server.service.d/93-openai-oauth-v187.conf \
         "/etc/systemd/system/turnstone-node@.service.d/93-openai-oauth-v187.conf" \
         /etc/systemd/system/turnstone-console.service.d/93-openai-oauth-v187.conf; do
  printf '[Service]\nEnvironment="PYTHONPATH=/opt/turnstone-overlays/openai-oauth-v187"\n' \
    | sudo tee "$f" >/dev/null
  sudo chmod 0644 "$f"
done
sudo systemctl daemon-reload
sudo systemctl restart turnstone-server.service turnstone-node@2 turnstone-node@3 \
     turnstone-node@4 turnstone-node@5 turnstone-console.service
```

Post-activation checks: each unit's `MainPID` environ shows the OAuth slot;
`/health` returns 200; the alias probe still excludes `tier1-decision`; a fresh
workstream selects the canary alias and performs real inference with
`served_model=gpt-6.1-sol`.

**Rollback** — remove the three `93-` drop-ins, `daemon-reload`, restart the same
units. The `90-`/`91-` (and R2 `92-`) anchors restore the previous overlay. Model
rows are untouched by activation and rollback.

## 5. Alias cutover (a separate, later gate)

The canary is a **new** alias; the existing `gpt-6.1-sol` row keeps pointing at
Switchyard until the canary passes.

1. Add a canary row, e.g. `gpt-6.1-sol-native`:
   `provider=openai-responses`, `model=gpt-6.1-sol`,
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

## 6. Credential cutover

The working store `/var/lib/turnstone/github/siwc-creds` stays authoritative until
this package is live. At cutover:

- point every consumer at the runtime store in one step — never two independent
  writers refreshing one grant;
- the runtime copy was byte-identical at staging time; if a refresh has happened
  since, re-copy rather than assuming;
- retire the old store as an **inactive** historical reference, and never revive a
  rotated token from it.
