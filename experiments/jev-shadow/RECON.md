# JEV SHADOW INTEGRATION — RECON (2026-10-01)

Read-only recon. No mutation performed. Baseline frozen before any action.

## Baseline freeze (pre-recon)

```text
routes.toml sha256 : c16930a10f10ee39612d1862c7149a8b96542701d18ad99f9a7b3577c0507188
routes.toml mtime  : 2026-10-01 00:52:06
Switchyard MainPID : 3101732   NRestarts=0   health={"status":"ok"}
Switchyard binary  : a1569e0790d564b5
nodes 2-6          : original PIDs, NRestarts=0, slot 1.8.5-8d6bd4a4-sensor-lane
census             : routes=45 targets=25 llm_clients=25 capabilities=6 capability_clients=5
```

## A. Hosted Jev Router — `typesafe/jev-router`

Source: OpenRouter live catalogue (`/api/v1/models`), not the marketing page.

```text
id                 : typesafe/jev-router
name               : TypeSafe: Jev Router
created            : 1790363560  -> 2026-09-25
context_length     : 1000000
pricing            : prompt = -1, completion = -1   (free tier; -1 = uncharged)
architecture       : text+image+file+audio+video -> text, tokenizer "Router"
supported_parameters: reasoning, reasoning_effort, structured_outputs, include_reasoning,
                     logprobs, top_logprobs, seed, temperature, top_p, max_tokens,
                     max_completion_tokens, tools, tool_choice, response_format, ...
top_provider       : context_length=None, max_completion_tokens=None, is_moderated=False
```

Established:

- exact id `typesafe/jev-router` — **confirmed**, not assumed
- **free** — both pricing fields are `-1`
- `reasoning_effort` is an accepted parameter (so effort is settable)
- no `max_completion_tokens` advertised by top_provider — must not assume a ceiling

**Still unproven, requires a live probe** (cannot be read from a catalogue):
selected downstream model visibility, selected effort visibility in the response,
candidate-model restriction support, provider-routing controls, rate limits,
timeout/failure shape. The marketing page answered none of these; this is exactly
the "do not assume marketing-page behaviour is a stable API contract" case.

## B. Local Jev-Style — `chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF`

Source: HF API (authoritative), plus the repo's own `manifest.json`.

```text
repo revision (sha): edf37c26a1098f83cf4264b8adbe0dca2d2ebb0c
lastModified        : 2026-09-26T11:50:27Z
pipeline_tag        : text-classification
license             : apache-2.0
base_model          : Qwen/Qwen3.5-0.8B  (rev 2fc06364715b967f1860aea9cf38778875588b17)
architecture        : Qwen3_5ForCausalLM, text only, 24 layers (18 Gated DeltaNet + 6 full attn),
                      hidden 1024, tied embeddings, 752,393,024 params
readout             : "verdict",  template "macjev-render-v1"
budgets             : max_len 25600, head_max 2048
llama.cpp commit    : 441df11f65ea0b6d0c72965aaf70c8241070ddcb
tested with         : python 3.12, tokenizers 0.23.2, numpy 2.5.3
```

### GGUF variants with exact SHA256 (from the repo manifest)

| file | sha256 | bytes |
|---|---|---|
| `Jev-Style-0.8B-Decision-v3-Q8_0.gguf` | `cd87d284c4ee355cb0b3fce7391db57ba3ad108e45b3a41b058d529931a2bd5e` | 811,843,040 |
| `Jev-Style-0.8B-Decision-v3-Q4_K_M.gguf` | `0a19bc29bacc33e0d871146c8612b24dd14c2ed2e61cedeb7a928b0852628bac` | 529,296,864 |
| `Jev-Style-0.8B-Decision-v3-F16.gguf` | `a33f709e10009c3fe182b51e4945d440288b137a35ad5d481e4ac1f2800b1a27` | 1,516,744,160 |

Supporting files also hashed in the manifest (scorer + config + tokenizer), e.g.
`jev_score.cpp`, `build_jev_score.sh`, `readout_config.json`, `tokenizer/tokenizer.json`.

### Critical mechanism finding — llama.cpp alone is NOT sufficient

Decision probabilities require the **custom scorer**, not ordinary generation:

- scorer source `jev_score.cpp`, built by `build_jev_score.sh` -> executable `jev-score`
- interface: JSON-lines process
- it reads **logits at verdict-slot positions**
- scoring rule, verbatim from `readout_config.json`:

```text
per option k: logit[' yes'] - logit[' no'] at the k-th ' ->' slot,
computed as h_slot . (w_yes - w_no) from the final normed hidden state
and the tied embedding rows (float32)
```

slot token ids: ` yes`=9542, ` no`=874, ` ->`=1411, plus per-letter ids.

Implication for design: a plain llama.cpp `llama-server` chat endpoint **cannot**
produce calibrated per-option decision probabilities. The adapter must speak to
`jev-score` (JSON-lines) or the Python `JevStyleDecisionGGUF` wrapper. This is the
single most important recon fact and it invalidates the cheapest option.

Over-budget inputs **error, they do not truncate** (max whole input 25,600 tokens).

## C. LocalClaw model-free boundary — HELD

```text
no jev weights on localclaw          : confirmed (only unrelated Hermes plugin YAML)
no jev package in any runtime venv   : confirmed
torch / model processes on localclaw : 0
```

## D. Host placement — a real constraint, discovered not assumed

The prompt says "local Jev-Style should live on the HTPC, beside Laya". Laya's
real client is:

```text
capability_clients.htpc-laya-decision
  base_url : http://100.105.20.36:8011
  endpoint : /v1/systemone
  model    : laya-rl-agent
```

Reachability measured:

| lane | result |
|---|---|
| `ssh htpc` (configured key `id_ed25519_htpc`) | **Permission denied (publickey,password)** |
| `ssh reninja` / `ssh comfyninja` | **OK — reachable** |
| `curl http://100.105.20.36:8011/v1/systemone` from localclaw | **HTTP 401** — service alive, auth required |
| same probe from reninja | **HTTP 401** |

So HTPC is **network-reachable** (401 proves the Laya service answers) but
**SSH-denied** from localclaw. Jev-Style on HTPC is therefore *architecturally
right and operationally blocked* until an operator grants an HTPC management lane.
This is an authority boundary, not a capability gap, and it is not something to
work around by creating new credentials.

## E. Decision for the next step

Placement cannot proceed on HTPC today. Two paths, both requiring operator
awareness, not unilateral action:

1. **HTPC** (as instructed) — blocked on an SSH/management lane.
2. **reninja / ComfyNinja** — reachable now, but it is the ComfyNinja host
   (GPU + ComfyUI + Studio), which is a *different* role from "beside Laya".
   Placing a second decision service there needs an explicit operator ruling.

No Jev weights, runtime or service were placed anywhere during recon.
