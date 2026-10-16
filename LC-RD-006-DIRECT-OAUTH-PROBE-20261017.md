# LC-RD-006 — Direct ChatGPT OAuth probe (open-source token sharing) (2026-10-17)

Authorized scope: implement and test a **narrow, standalone** proof of concept for
the official Sign in with ChatGPT direct Responses flow. This is not a Turnstone
production provider and not a new authentication subsystem.

Status: **probe implemented, tested offline, awaiting one browser consent.**
No inference has been attempted yet — that requires the consent step below.

---

## 1. Why a probe was needed rather than a modification

The direct flow is **not** the Codex CLI device login. The Codex runtime contains
**zero** occurrences of the direct-flow markers:

| Marker | Occurrences in the bundled `codex` binary |
|---|---|
| `chatgpt.tokens.use.direct` | 0 |
| `dynamic_agent_client` | 0 |
| `resource.invoke` | 0 |
| `ext_agent_host_id` / `agent_name_hint` | 0 |

and the official sign-in page states plainly: *"Check the token response's
granted scopes for `chatgpt.tokens.use.direct` before proceeding to inference. A
valid ID token alone does not authorize ChatGPT plan usage."*

The pre-existing Codex device sign-in is therefore **left completely untouched**,
and this probe uses its own credential store.

## 2. What was built

`scripts/siwc_direct_oauth_probe.py` — public client, authorization-code grant:

- **PKCE S256** (RFC 7636; unit-tested against the RFC's Appendix B vector)
- fresh **`state`** and **`nonce`** per attempt
- **persistent per-host `ext_agent_host_id`**, created once and stored
- **first-time registration** uses `client_id=dynamic_agent_client` +
  `agent_name_hint`, and **requires** the issued `client_id` (`oaiapp_…`) on the
  callback — a callback without it is treated as incomplete registration
- **reauthorization** reuses the retained issued `client_id`, omits
  `agent_name_hint`, and sends `id_token_hint`/`login_hint` only when stored
- scope: `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`
- `resource=https://api.openai.com/v1`
- `redirect_uri` fixed at `http://127.0.0.1:1455/auth/callback` (never
  `localhost`) and sent **byte-identically** in both the authorization request
  and the code exchange

Endpoints resolved from the provider's own OIDC discovery
(`https://auth.openai.com/.well-known/openid-configuration`, HTTP 200 →
`issuer=https://auth.openai.com`, `jwks_uri=…/.well-known/jwks.json`), so the
issuer and JWKS are not hard-coded guesses.

### Validation actually performed

- `state` compared against the pending attempt; mismatch is refused (CSRF)
- authorization denials propagated verbatim (`error` / `error_description`)
- **ID token**: RS256 signature verified against the published JWKS, plus
  `iss`, `aud` (against the **issued** client id), `exp`, and `nonce`; `sub` used
  as the account identity
- **granted scopes** checked for `chatgpt.tokens.use.direct`, preferring the
  token response's `scope` field over the callback's
- if the scope is absent the run **stops with the actual granted list** — no
  workaround, per instruction

## 3. The self-hosted VM problem, and the chosen procedure

A `127.0.0.1` callback reaches the machine running the **browser**, which is the
operator's Windows 11 box — not this VM. The probe therefore supports both
documented shapes:

| Path | How it works | Operator effort |
|---|---|---|
| **Copy-paste** (default) | `authorize` prints the URL; the browser lands on the loopback callback with nothing listening; the operator copies the address bar back into `complete` | one paste |
| **SSH tunnel** (`--listen`) | operator forwards `-L 1455:127.0.0.1:1455` and the VM captures the callback itself | one tunnel + one click |

Both keep the `redirect_uri` identical on both legs. Neither prints a token: the
copy-paste path carries only a **single-use authorization code** (exchanged
server-side on this VM), and the listener prints nothing at all.

## 4. Tests

`integrations/turnstone-self-api/tests/test_siwc_direct_oauth.py` — **36 pass**,
offline, no credentials:

| Area | Covered |
|---|---|
| PKCE | S256 correctness; RFC 7636 vector; uniqueness |
| Authorization URL | all six scopes; `dynamic_agent_client` + hint on registration; issued id + hint omitted on reauthorization; `localhost` never used; fresh state/nonce |
| Callback | happy path; state mismatch refused; denial propagated; missing code refused; missing issued `client_id` refused; reauthorization may omit it; no-pending refused; bare URL refused |
| Scopes | present/absent; `+` and comma separators; token-response preference |
| ID token | valid; wrong nonce; wrong audience; wrong issuer; expired; **forged signature** — all against a locally generated JWKS served over loopback |
| Store | refuses Codex CLI paths and `codex-home` lookalikes; `0600` file / `0700` dir; stable host id; display never contains token values |
| Request shape | `store=false`, `stream=true`, **no** flow-forbidden parameter sent; success requires `response.completed`; `response.failed` surfaced (`subscription_sharing_usage_limit_exceeded`) rather than hidden |

Two deliberate properties worth calling out: the inference test asserts that a
stream ending **without** `response.completed` is **not** reported as success,
and the store test asserts the reporting path cannot emit token values.

## 5. Operator command (the one step that needs a browser)

```bash
# 1) generate the authorization URL
/var/lib/turnstone/github/siwc-venv/bin/python \
  /var/lib/turnstone/github/turnstone/scripts/siwc_direct_oauth_probe.py \
  authorize --agent-name Turnstone

# 2) open the printed URL in your Windows browser, approve, then copy the FULL
#    redirect URL from the address bar (it will fail to load — expected) and:
/var/lib/turnstone/github/siwc-venv/bin/python \
  /var/lib/turnstone/github/turnstone/scripts/siwc_direct_oauth_probe.py \
  complete --callback-url '<paste the whole http://127.0.0.1:1455/auth/callback?... URL>'

# 3) verify
… status      # shows granted scopes; AUTHORIZED / NOT AUTHORIZED
… models      # GET /v1/models — reports whether gpt-6.1-sol is available
… infer --model gpt-6.1-sol     # one streamed POST /v1/responses
```

SSH-tunnel alternative for step 2: `ssh -L 1455:127.0.0.1:1455 <vm>` then
`authorize --listen` and approve in the browser.

Setup (already done on this host): `python3 -m venv /var/lib/turnstone/github/siwc-venv`
and `pip install 'PyJWT[crypto]'`.

Credential store: `/var/lib/turnstone/github/siwc-creds` (`0700`, files `0600`),
**separate from Codex CLI's store**. Refresh tokens are retained and refreshed
automatically with ~60 s of headroom before expiry.

## 6. Expected outcome

Unverified until consent is completed. The Codex binary evidence in §1 predicts
that a Codex-device-login session will **not** carry
`chatgpt.tokens.use.direct` — but that is a prediction about a *different*
credential, not about this flow. This probe's own consent is what is being
tested, and it is expected to be the mechanism that grants the scope.

If the scope comes back absent, or inference returns an eligibility/plan error,
the probe prints the exact returned error and stops — no API key, no Switchyard
route substituted.

## 7. Preserved

The seven verified model additions (four OpenRouter + three Switchyard-backed)
remain operational. `gpt-6.1-sol`'s production definition is **unchanged**.
No OpenRouter rotation, no Codex credential revocation, no permission narrowing,
no Switchyard routing change. The pre-existing Codex device sign-in is untouched,
and `/home/vincent/.codex` was neither read nor modified.
