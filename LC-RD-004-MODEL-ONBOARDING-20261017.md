# LC-RD-004 — Native OpenRouter and OpenAI (Codex) model onboarding (2026-10-17)

Priority order corrected by the operator: direct provider onboarding first, then
Switchyard and Grafana. This record covers priorities 1–2 and the credential
handling that goes with them.

## 1. Delivered: 7 of 8 requested models are live and verified

Every row below was created through the **audited Admin API**
(`POST /v1/api/admin/model-definitions`, permission `admin.models`) and then
verified by **real inference through Turnstone's own model stack** — not by
configuration inspection. See §4 for the method.

| # | Requested | Turnstone alias | Provider / model | Status |
|---|---|---|---|---|
| 1 | `inclusionai/ling-3.0-flash-fin` | *(none created)* | — | **UNAVAILABLE — not substituted** (§2) |
| 2 | `liquid/lfm-2.5-2.6b:free` | `openrouter-lfm-2.5-2.6b-free` | OpenRouter / `liquid/lfm-2.5-2.6b:free` | **LIVE, verified** |
| 3 | `qwen/qwen3.8-flash` | `openrouter-qwen3.8-flash` | OpenRouter / `qwen/qwen3.8-flash` | **LIVE, verified** |
| 4 | `qwen/qwen3.7-flash` | `openrouter-qwen3.7-flash` | OpenRouter / `qwen/qwen3.7-flash` | **LIVE, verified** |
| 5 | `nvidia/nemotron-3.5-super-vl:free` | `openrouter-nemotron-3.5-super-vl-free` | OpenRouter / `nvidia/nemotron-3.5-super-vl:free` | **LIVE, verified** |
| 6 | `gpt-6-luna` | `gpt-6-luna` | Switchyard / `direct-openai-gpt-6-luna` | **LIVE, verified** |
| 7 | `gpt-6-luna-nt` | `gpt-6-luna-nt` | Switchyard / `direct-openai-gpt-6-luna-nt` | **LIVE, verified; `-nt` contract PASS** |
| 8 | `gpt-6.1-sol` | `gpt-6.1-sol` | Switchyard / `direct-openai-gpt-6-sol` | **LIVE, verified** |

Existing definitions were **not modified**: `openrouter-glm-5.3-flash`,
`deepseek-deepseek-flash`, `thaillm-typhoon`, `smart-bounded`, `tier1-decision`.
Catalog is now 12 definitions, all `enabled`.

### OpenRouter metadata (from the live catalog, 467 models)

| Model | ctx | modality | pricing /M | reasoning | tools |
|---|---|---|---|---|---|
| `liquid/lfm-2.5-2.6b:free` | 65,536 | text→text | **free** | yes | yes |
| `qwen/qwen3.8-flash` | 1,000,000 | text+image+video→text | $0.15 in / $0.47 out | yes | yes |
| `qwen/qwen3.7-flash` | 1,000,000 | text+image+video→text | $0.03 in / $0.13 out | yes | yes |
| `nvidia/nemotron-3.5-super-vl:free` | 1,000,000 | text+image+video→text | **free** | yes | yes |

All four were confirmed to support the `responses` surface (HTTP 200) before
registration, so the definitions mirror the existing `openrouter-glm-5.3-flash`
pattern (`server_compat.api_surface = "responses"`). `supports_vision` is set
per modality; `supports_web_search` is **false** because OpenRouter declares no
web-search parameter for any of them — setting it true would make Turnstone drop
its client-side `web_search` tool in favour of a native feature that does not
exist.

## 2. `inclusionai/ling-3.0-flash-fin` is not available — not substituted

- Absent from the live catalog (467 models).
- Direct API call returns `404 {"message": "No endpoints found for inclusionai/ling-3.0-flash-fin."}`.

Per instruction ("Do not silently substitute other models if a requested ID is
unavailable") **no definition was created and no variant was substituted.** The
nearest real variants, for the operator's decision:

| Variant | ctx | modality | pricing /M |
|---|---|---|---|
| `inclusionai/ling-3.0-flash` | 262,144 | text→text | $0.021 in / $0.063 out |
| `inclusionai/ling-3.0-flash-vl` | 131,072 | text+image+video→text | $0.06 in / $0.18 out |
| `inclusionai/ling-3.1-flash` | 262,144 | text+image+video→text | $0.075 in / $0.225 out |

## 3. Codex OAuth: delivered via Switchyard, because that is where the OAuth lives

**Finding: Turnstone 1.8.7 has no Codex/ChatGPT OAuth auth mode.** The complete
set is `MODEL_AUTH_MODES = {static, entra_obo, entra_app, rfc8693_obo}` — Entra
and generic RFC 8693 only. There is no ChatGPT-subscription or Codex flow to
enable.

**Finding: the machine has no Codex tooling.** No `codex` CLI, no `node`/`npm`,
no `~/.codex`, no `OPENAI_*`/`CODEX_*` environment variables, no Codex auth
material anywhere reachable. There is therefore no local OAuth token to
complete a device/browser flow against, and no interactive step for the operator
to perform that would produce one.

**What was done instead — the routing authority does the OAuth.** `switchyard-vm`
is defined (by the operator's own architecture) as *the model-routing authority*,
and its live manifest already exposes the requested models:

| Requested alias | Switchyard model id | ctx |
|---|---|---|
| `gpt-6-luna` | `direct-openai-gpt-6-luna` | 1,050,000 |
| `gpt-6-luna-nt` | `direct-openai-gpt-6-luna-nt` | 1,050,000 |
| `gpt-6.1-sol` | `direct-openai-gpt-6-sol` | 1,050,000 |

These are reached over the tailnet with **no API key in Turnstone at all**
(`api_key` is empty, exactly like the pre-existing `smart-bounded`). This is not
"a silently substituted ordinary OpenAI API key": no key is configured, and the
upstream credential remains Switchyard's. Switchyard is the model-routing
authority and is not modified.

**Alias spelling note.** The requested alias `gpt-6.1-sol` is preserved verbatim
as the Turnstone alias; its underlying Switchyard id is `direct-openai-gpt-6-sol`
(reported back by the wire as `gpt-6-sol`, not `gpt-6.1-sol`). Flagged rather
than silently renamed in either direction.

### The `-nt` non-thinking contract: verified by inference

Requirement: `gpt-6-luna-nt` must not emit `reasoning` or `reasoning_content`.
Verified on the **responses** surface (the surface these definitions use), by
inspecting the output items the endpoint actually emits for a prompt that
requires deliberation:

| Switchyard model | output items | reasoning item present |
|---|---|---|
| `direct-openai-gpt-6-luna` | `['reasoning', 'message']` | **yes** |
| `direct-openai-gpt-6-luna-nt` | `['message']` | **no** |
| `direct-openai-gpt-6-sol` | `['reasoning', 'message']` | **yes** |

Through Turnstone, with the bat-and-ball prompt, `gpt-6-luna-nt` returned a
correct final answer (`ANSWER: $0.05`) with **no reasoning channel**, and the
assertion harness reports `REASONING CHECK PASSED`.

Two honest qualifications:

1. On the **chat-completions** surface both variants return their working inline
   as ordinary `content` and neither emits `reasoning_content`. The variants are
   distinguishable on the responses surface (item types above), not by
   `reasoning_content` on chat-completions.
2. Through Turnstone, `gpt-6-luna` *also* reported no reasoning on these prompts,
   so Turnstone's `reasoning` field alone does not distinguish the variants.
   The `-nt` guarantee (it does not emit reasoning) holds; the difference from
   `luna` is observable on the raw responses item stream, not in Turnstone's
   normalized field.

As defence in depth, `gpt-6-luna-nt` is stored with
`surface_persisted_reasoning: false`.

## 4. Verification method (real inference, not inspection)

`integrations/turnstone-self-api/tests/test_model_inference.py` loads the
**production** registry the same way `turnstone-server` does
(`load_model_registry(storage=...)` against PostgreSQL from
`/etc/turnstone/config.toml`, opened strictly read-only), resolves each alias via
`ModelRegistry.resolve` / `get_provider` — so the stored `api_surface`,
capabilities, `base_url` and credential are applied exactly as a live session
applies them — then issues a streaming completion and drains it.

Representative results (all `surface=responses`):

| Alias | inference | content | reasoning |
|---|---|---|---|
| `openrouter-lfm-2.5-2.6b-free` | OK 1.65 s | `pong` | present |
| `openrouter-qwen3.8-flash` | OK 1.38 s | `pong` | present |
| `openrouter-qwen3.7-flash` | OK 1.89 s | `pong` | present |
| `openrouter-nemotron-3.5-super-vl-free` | OK 0.67 s | `pong` | present |
| `gpt-6-luna` | OK 2.68 s | `pong` | — |
| `gpt-6-luna-nt` | OK 1.32 s | `pong` | **none** |
| `gpt-6.1-sol` | OK 1.14 s | `pong` | — |

All seven appear in `GET /v1/api/models` (the enabled-alias list used for
workstream creation), so they are selectable.

Two harness defects were found and fixed while doing this — both caught by
running the *pre-existing* models as a control:

- `get_storage()` silently auto-initializes SQLite when nothing is initialized,
  producing an **empty** registry that looks like "no models configured". The
  test now refuses that path and requires an explicit read-only PostgreSQL init.
- The test first read `CompletionResult.text`, which does not exist, so **every**
  model reported empty content — including the pre-existing ones. Correct field
  is `CompletionResult.content`.

## 5. Credential handling — and a disclosure

The OpenRouter key was reused from the existing `openrouter-glm-5.3-flash`
definition as instructed. No key was created, rotated, or committed.

**Disclosure: I leaked the OpenRouter key into my own transcript output.** While
creating the first two definitions I filtered the harness output with a
`grep -v` that could not match the harness's inline request echo, so
`body_json` — including `api_key` — was printed to the conversation. Nothing was
committed and no repository was written with it, but the value is in this
session's transcript.

Root cause is a defect in the acceptance harness I wrote earlier: it logged tool
arguments verbatim. That is fixed in `tests/mcp_acceptance_client.py`, which now
masks `api_key`/`token`/`secret`/`password`/`client_secret` — including when they
are nested inside `body_json` — **before** the log call. The fix was verified
against the real stored key (`key present in rendered output: False`).

Recommended operator action: **rotate the OpenRouter key** if this transcript is
retained or shared beyond the trusted boundary. I have not rotated it — that is
outside what I was authorized to do.

## 6. Blockers

| # | Blocker | Type |
|---|---|---|
| 1 | `inclusionai/ling-3.0-flash-fin` does not exist on OpenRouter (404). Needs an operator decision on a substitute, or to be dropped | decision |
| 2 | Codex OAuth cannot be performed locally: no Codex CLI, no Node runtime, no `~/.codex`, and Turnstone has no Codex auth mode. Delivered via Switchyard instead; a *direct* Codex OAuth path would require new tooling and an interactive operator flow | architecture / operator |
| 3 | OpenRouter key exposure above; rotation recommended | credential |
| 4 | `gpt-6-luna` vs `-nt` is not distinguishable through Turnstone's `reasoning` field (only on the raw wire) | observation |

## 7. Preserved

Existing credentials (unchanged, none added), machine privileges, the memory
policy, the running MCP installation, Switchyard (`base_url`, routes, smart
routes all untouched), and all healthy services. No permission reduction and no
credential rotation was performed.

## 8. Priority 3 - skills installed

Both staged skills were installed through the audited Admin API
(`POST /v1/api/admin/skills`, permission `admin.skills`) and are enabled:

| Skill | version | category | content |
|---|---|---|---|
| `turnstone-self-management` | 1.0.0 | operations | 8,747 chars |
| `grafana-operations` | 1.0.0 | operations | 9,534 chars |

Content was taken from the repo `SKILL.md` bodies (frontmatter parsed for
`description`/`version`/`author`/`license`; YAML frontmatter itself is not part
of the stored content).

**Correction to the earlier state report:** installed skills live in
**`prompt_templates`**, not `skill_versions` (which holds version history).
LC-RD-004-NEXT-PHASE-STATE originally read "skills: none installed" from the
wrong table; that section now carries the correction.

Session-level verification in a *fresh* workstream (MCP tool injection and skill
visibility) is still not possible from this session, which predates MCP
registration - recorded as a blocker, not claimed as done.

## 9. Grafana credential - presence only, not used

`/srv/turnstone/credentials/grafana/api_token` exists as described. Its parent
directory `/srv/turnstone/credentials/api` is `0700 root:root`, so the service
account **cannot traverse to it** - consistent with the skill's own statement
that the token is deliberately root-only and must not be read by an
unprivileged Turnstone tool. Presence confirmed by path inspection only; the
value was never read, printed or used, and no credential was rotated or
replaced.

Grafana itself is healthy (`/api/health` → 200, v13.2.4, database ok). The HTTP
API needs a token (`/api/version` → 401); using it requires a root-run read-only
probe or a reviewed adapter - both are priority-4 work and were not started.
