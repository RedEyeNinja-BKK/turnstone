# LC-RD-006 — Direct ChatGPT OAuth probe (open-source token sharing) (2026-10-17)

**Result: the direct flow is AUTHORIZED and native GPT-6.1 Sol inference through
the OpenAI Responses API SUCCEEDED.** No production model definition was changed.

Probe: `scripts/siwc_direct_oauth_probe.py` (published revision `ec9cff02`).
Run as the `turnstone` service account, isolated venv, credential store separate
from Codex CLI's.

---

## 1. Authorization outcome

Operator completed the browser consent through an SSH-forwarded loopback callback
(`ssh -L 1455:127.0.0.1:1455`). The listener captured the callback directly; no
authorization code ever passed through argv, a paste buffer, or a transcript.

| Item | Value |
|---|---|
| Token exchange | **HTTP 200** |
| Issued client_id | `oaiapp_W64qQn4mx5J6UWhC8NSDXUkW6` (retained; `dynamic_agent_client` is not reused) |
| Granted scopes | `chatgpt.tokens.use.direct email offline_access openid profile resource.invoke` |
| Scope verdict | **AUTHORIZED** — `chatgpt.tokens.use.direct` present |
| ID token | signature + `iss` + `aud` + `exp` + `sub` + `nonce` all verified |
| Host id | `urn:uuid:2ced8f7f-81a1-4dae-839b-5beaa44aa9eb` |
| Store | `/var/lib/turnstone/github/siwc-creds/credentials.json`, mode `0600`, `turnstone:turnstone` |

Every requested scope was granted; none were silently dropped. The account
identity was verified from the ID token (not reported here — see §5).

## 2. Account-specific model discovery

`GET /v1/models` → **HTTP 200**, 9 models reported, 7 with `visibility: "list"`:

| slug | display_name | visibility |
|---|---|---|
| `gpt-5.6-luna` | GPT-5.6-Luna | list |
| `gpt-5.6-sol` | GPT-5.6-Sol | list |
| `gpt-5.6-terra` | GPT-5.6-Terra | list |
| `gpt-6-astra` | GPT-6-Astra | list |
| `gpt-6-luna` | GPT-6-Luna | list |
| `gpt-6-sol` | GPT-6-Sol | list |
| **`gpt-6.1-sol`** | **GPT-6.1-Sol** | **list** |

**`gpt-6.1-sol available: YES`** — exact slug match, `visibility='list'`.

This settles the model-identity question two ways at once:

1. `gpt-6.1-sol` and `gpt-6-sol` are **distinct entries** in the account catalog
   with distinct `display_name`s — they are not the same model.
2. `gpt-6.1-sol` is available **on the direct OAuth path**, whereas Switchyard
   exposes only the non-`.1` family. The alias name `gpt-6.1-sol` is therefore
   *achievable* natively; it is currently backed by `direct-openai-gpt-6-sol`.

## 3. Inference result

One real streamed request, honouring this flow's constraints
(`store=false`, `stream=true`, no unsupported parameters):

| Field | Result |
|---|---|
| Request | `POST /v1/responses`, `model=gpt-6.1-sol` |
| Status | **HTTP 200** |
| SSE events | 13 |
| Text | `Hello, world!` |
| Terminal | **`response.completed`** — SUCCESS |

`response.completed` was required before declaring success; a stream ending
without it is treated as failure by the probe.

### Reasoning-effort support (verified, not assumed)

| `reasoning.effort` | Result |
|---|---|
| `none` | **HTTP 400** `unsupported_value` |
| `low` | HTTP 200, completed |
| `high` | HTTP 200, completed |
| `max` | HTTP 200, completed |

Confirms the documented range: **low through max, and `none` is not accepted.**
This matters for the eventual Turnstone definition — `none` must not be offered
in the effort ladder for this model.

## 4. Minimal native Turnstone integration (proposed, NOT implemented)

The direct flow is a plain `POST /v1/responses`, so this is **not** a JSON-RPC
adapter. Measured against Turnstone's existing Responses provider
(`turnstone/core/providers/_openai_responses.py`):

| Requirement | Turnstone status |
|---|---|
| `store: false` | **already set** (line 437) |
| `stream: true` | by design |
| `instructions` for system content | already used |
| omit `previous_response_id`, `temperature`, `top_p`, `metadata`, `user`, `truncation` | not sent |
| omit `max_output_tokens` | **sends it** — 1 conflict |
| no `role:"system"` items in `input` | non-leading system messages keep that role — conflict |
| no `tool_search` | injected when `defer_loading` is used — conflict |
| OAuth token lifecycle (refresh) | **absent** — needs a credential provider |

So the remaining work is: (a) three request-profile guards, gated on something
like `server_compat.api_surface = "responses_openai_direct"`; (b) an OAuth token
source with refresh; (c) a model definition whose effort ladder is `low…max`.

**Not built.** The probe is standalone and proves eligibility only.

## 5. Sanitizing notes

The probe never prints tokens or codes, and none appear in this record. The
account identity (`sub`, email) is deliberately omitted here even though the
probe verified it.

## 6. Credential lifecycle / rollback

A new credential now exists at
`/var/lib/turnstone/github/siwc-creds/credentials.json` (`0600`, `turnstone`).
It holds `client_id`, `access_token` (~1 h), `refresh_token`, and `id_token`.
Remove the store directory to drop it locally; the registration can be revoked
from the account's connected-applications settings. The probe has no server-side
footprint. Codex CLI's store was never read or written.

## 7. Preserved

12 model definitions unchanged; `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-luna-nt` all
still Switchyard-backed; 5 OpenRouter rows still keyed and **not rotated**; MCP
bridge connected on all six endpoints with zero errors; skills, privileges and
the memory policy untouched. No model-definition audit row was written this
session (last: 2026-10-16T19:49:18).
