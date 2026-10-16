# LC-RD-005 — Codex OAuth determination, OpenAI model identity, key rotation readiness (2026-10-17)

Closes the outstanding Codex question from LC-RD-004, corrects the OpenAI model
identity, and prepares (but does not perform) the OpenRouter key rotation.

Headline: **Codex login DOES work on turnstone-vm** — I was wrong to imply it
needed Node. But Codex OAuth **cannot back native Turnstone model inference**
without building a new adapter subsystem, which is a scope decision for the
operator, not something to invent.

---

## 1. Codex on turnstone-vm: the operator was right, and it works

My earlier "no Codex tooling" finding was true but not decisive. The official
Python SDK ships its own runtime, so Node was never required.

| Check | Result |
|---|---|
| PyPI package | `openai-codex` **0.163.0**, author OpenAI, `requires_python >=3.10` |
| Dependencies | `pydantic>=2.12`, `packaging>=26.2`, **`openai-codex-cli-bin==0.163.0`** |
| Installed on this host | cleanly, in an isolated venv, Python **3.13.5** |
| Runtime bundled | `codex_cli_bin/bin/codex` — **static-pie ELF**, `codex-cli 0.163.0` |
| Node/npm needed | **no** |
| `codex doctor` before login | `✗ auth  no Codex credentials were found`; `⚠ websocket  Responses WebSocket failed; HTTPS fallback may still work` |

**Device-code login was proven end to end (initiation).** Running
`codex.login_chatgpt_device_code()` on this host returned:

```
verification_url  https://auth.openai.com/codex/device
user_code         0NC2-406BA
handle attrs      ['cancel','login_id','user_code','verification_url','wait']
```

Completion needs the operator's browser; I did **not** wait and **no credentials
were stored**. The specific codes above have expired — the runnable instructions
are in §5.

### The three questions answered

**Q1 — Does official Codex login work on turnstone-vm with operator interaction?**
**Yes.** Install and login initiation both succeed locally; completion is a
browser approval at the URL above. Nothing else is needed on the host — no Node,
no pre-existing `~/.codex`, no API key.

**Q2 — Can the authenticated Codex client be used for agent execution?**
**Yes.** The SDK exposes `Codex.thread_start(...)` / `thread.run(...)` →
`TurnResult`, `Sandbox.read_only|workspace_write|full_access`, turn-level sandbox
overrides, `thread_resume`, `AsyncCodex`; the CLI adds `codex exec` for
non-interactive runs and `app-server` / `exec-server` services. That is a real
agent runtime. (Not exercised end-to-end here, because that requires credentials
I deliberately did not obtain.)

**Q3 — Can those credentials legitimately and reliably back native Turnstone
model inference?**
**No — not without a new adapter.** Evidence:

| Observation | Consequence |
|---|---|
| No `serve`/`proxy`/`openai`/`http` **ingress** subcommand exists. Full command set: `agents, exec, review, login, logout, mcp, plugin, app-server, remote-control, completion, update, doctor, sandbox, debug, apply, resume, queue, archive, delete, migrate-rollouts, unarchive, fork, cloud, exec-server, features` | Codex serves no OpenAI-compatible endpoint |
| `app-server proxy` is documented as *"Proxy stdio bytes to the running app-server control socket"* | Its protocol is Codex's own JSON-RPC, not OpenAI's HTTP surfaces |
| Binary string scan: **0** occurrences of `openai-compatible` or `chat.completions` | No OpenAI-compatible surface is implemented |
| `/v1/responses`, `api.openai.com`, `auth.openai.com` strings | These are **egress** — Codex calls upstream; they are not an ingress |
| Turnstone `MODEL_AUTH_MODES = {static, entra_obo, entra_app, rfc8693_obo}` | No Codex/ChatGPT auth mode exists |
| Turnstone providers speak OpenAI-compatible `chat/completions` or `responses`, chosen by `server_compat.api_surface` | Nothing to point at a Codex control socket |

So the two are different shapes: Turnstone wants an **OpenAI-compatible HTTP
model endpoint**; Codex offers an **agent runtime over JSON-RPC with ChatGPT-OAuth
auth**. Successful CLI login is therefore *not* proof of provider integration —
exactly the distinction the operator drew.

Closing that gap means a new subsystem (a local adapter presenting an
OpenAI-compatible surface and translating to Codex threads/turns, plus
credential lifecycle and streaming/tool-call fidelity). Per instruction I have
not built it. **Scope is a decision for the operator.** Meanwhile the three
Switchyard-backed aliases remain operational and are the correct integration —
Switchyard is the model-routing authority, and it is where the OAuth already is.

---

## 2. OpenAI model identity: the alias is mis-named (as suspected)

`gpt-6.1-sol` maps to Switchyard's `direct-openai-gpt-6-sol`. Verified against
the live OpenRouter catalog, these are **distinct** model ids:

| OpenRouter id | ctx | $/M in | $/M out |
|---|---|---|---|
| `openai/gpt-6.1-sol` | 1,050,000 | 2.00 | 10.00 |
| `openai/gpt-6-sol` | 1,050,000 | 2.00 | 10.00 |
| `openai/gpt-6.1-sol-pro` | 1,050,000 | 2.00 | 10.00 |
| `openai/gpt-6-sol-pro` | 1,050,000 | 2.00 | 10.00 |
| `openai/gpt-6-luna` | 1,050,000 | 0.10 | 0.50 |

**Finding: the Turnstone alias `gpt-6.1-sol` is named `6.1` but is backed by the
`6` (non-`.1`) model.** Switchyard's manifest contains **only** the non-`.1`
family (`direct-openai-gpt-6-luna`, `…-luna-nt`, `…-sol`) — there is no `gpt-6.1`
route on Switchyard at all.

Two further observations:

- I verified the identifiers are **distinct catalog entries**; I did **not**
  establish that they behave differently. Same price and context on both.
- `gpt-6-luna-nt` has **no OpenRouter counterpart** — no OpenAI model id of that
  form exists. The non-thinking behaviour is therefore a **Switchyard route
  property**, not an upstream model id. That matters: it cannot be reproduced by
  pointing Turnstone at OpenRouter.

**Unchanged, as instructed.** The alias stays operational pending an explicit
mapping decision. Options, for approval only:

| Option | Effect |
|---|---|
| (a) Rename the alias to `gpt-6-sol` | Name matches reality; breaks any existing selection of `gpt-6.1-sol` |
| (b) Repoint the model to a real 6.1 (needs OpenRouter `openai/gpt-6.1-sol` + the OpenRouter key, i.e. leaving Switchyard) | Alias name becomes accurate; changes the routing authority for this alias |
| (c) Leave as-is and document | Operational now; name stays misleading |

`gpt-6-luna-nt`'s verified no-reasoning behaviour is preserved and untouched.

---

## 3. OpenRouter key rotation: prepared, not performed

**Nothing was rotated, revoked or replaced.** The five definitions were confirmed
tagged for a single reviewed change (by `base_url = https://openrouter.ai/api/v1`):

| definition_id | alias |
|---|---|
| `04f82c83df4f4820a2fd7ae36e3de942` | `openrouter-glm-5.3-flash` |
| `6172c3ac3ed94a2fb4432fe8666fc6fb` | `openrouter-lfm-2.5-2.6b-free` |
| `06cfb0f1bdeb4674a77df9616cce8ccb` | `openrouter-nemotron-3.5-super-vl-free` |
| `5ca8112f37214238ae3aaa9508be6ff6` | `openrouter-qwen3.7-flash` |
| `f6b2079b1acc4aad8d76e26f4cec9fd5` | `openrouter-qwen3.8-flash` |

`scripts/rotate_openrouter_key.py` implements the procedure and is **dry-run by
default**; it changes nothing without `--apply`.

Sequence: (1) list OpenRouter definitions → (2) prove the NEW key authenticates
against OpenRouter → (3) `PUT /v1/api/admin/model-definitions/{id}` with
`{"api_key": …}` → (4) **real inference through Turnstone** for every rotated
alias, so a rotation that 401s mid-flight is caught rather than discovered later.

Safety properties, all verified:
- A key **cannot** be passed on argv (`--new-key` is explicitly refused — argv is
  world-readable and lands in shell history).
- Key-file must be `0600`; group/other-accessible files are refused.
- Length and last 4 chars are printed; the value never is.
- The replacement is validated **before** the Turnstone token is read or any
  definition is touched, so a bad key has zero side effects.
- Turnstone's audit row records `api_key: "(updated)"`, never the secret.

Guard behaviour observed:

```
--new-key on argv      -> REFUSING: keys must not be passed on the command line
no key source          -> REFUSING: supply the new key via --key-file/--key-stdin/--new-key-env
0644 key file          -> REFUSING: mode 0644; it must not be group/other accessible
bad key, 0600, validated first -> "validated: False (HTTP 401)" then REFUSING ... Nothing changed.
```

The acceptance-harness redaction fix from LC-RD-004 is preserved and still
prevents any secret from reaching the console.

---

## 4. Committed operational helpers

| File | Purpose |
|---|---|
| `scripts/codex_device_login.py` | Operator-run Codex device-code login. Prints only the URL and user code; never a token. `--status`, `--logout`, `--no-wait` supported. |
| `scripts/rotate_openrouter_key.py` | Prepared OpenRouter rotation; dry-run by default; refuses argv keys. |

---

## 5. Operator action: complete Codex login (optional, one command)

Only needed if a direct Codex path is wanted. Nothing else on the host is
required.

```bash
# one-time install (already done on this host)
python3 -m venv /var/lib/turnstone/github/codex-venv
/var/lib/turnstone/github/codex-venv/bin/pip install openai-codex

# start the login; prints a URL + code, then waits for your approval
/var/lib/turnstone/github/codex-venv/bin/python \
  /var/lib/turnstone/github/turnstone/scripts/codex_device_login.py
```

Then open the printed URL, enter the printed code, and approve. Verify afterwards
with `--status`; remove with `--logout`.

> **Set expectations:** completing this authenticates **Codex agent execution**.
> It does **not** by itself give Turnstone the three aliases as native
> inference — see Q3 above.

---

## 6. Not started (correctly sequenced, not blocked by Codex)

SSH to `switchyard-vm`, Grafana service-account integration, Switchyard/Turnstone
catalog reconciliation, the broken `tier1-decision` alias, and the
Hermes/OpenClaw/DSH worker VM. None of these was made a prerequisite of the Codex
determination, and none was touched by this record.

## 7. Preserved

No rotation, revocation or replacement of any credential. The five OpenRouter
definitions and all three Switchyard-backed aliases remain operational and
unmodified. `gpt-6-luna-nt`'s verified no-reasoning behaviour is intact.
Credentials, privileges, the memory policy, the MCP installation and Switchyard
routes are unchanged. No credential value appears in this document or in any
committed file.
