# JEV SHADOW — local Jev-Style deployment and hosted oracle qualification

> **SHADOW ONLY · NO PRODUCTION AUTHORITY · NOT AN UPSTREAM TURNSTONE FEATURE**

Nothing here participates in production routing, the sensor, the Judge or the
Output Guard. Jev production influence is mechanically **0**.

---

## 1. Authoritative HTPC management identity

```text
alias        htpc
host         100.105.20.36
user         turnstone-mgmt
key          ~/.ssh/id_ed25519_htpc
fingerprint  SHA256:r6sGp3ow8l0xvo7YGcD5WqVUkFMXhz/NezltUd+OUS0
sudo         passwordless
status       PROVEN
```

**`hermes-admin` is not an account on this host and must not be referenced as
one.** It was deleted from this fleet in August 2026. It survived only as a stale
`User` line in `~/.ssh/config`, which caused a false "credential boundary" report
on 2026-10-01. Corrected to `turnstone-mgmt`.

Access lessons that must survive:

1. Test the identity named in durable evidence, not the value in a config file.
2. Probe more than one plausible account before declaring none work.
3. `ssh -v` separates "key offered and refused" from "wrong key never offered".

## 2. Hosted Jev Router — external oracle, disqualified from policy

```text
id                typesafe/jev-router
context           1,000,000
API surface       /v1/chat/completions and /v1/responses both work
```

Measured, from the live catalogue and 12+ live probes:

| property | result |
|---|---|
| selected downstream model | **observable** (`response.model`) |
| selected reasoning effort | **NOT observable** |
| candidate controls | **NOT binding** |
| downstream cost | **real and material** |

Candidate controls that were tested and did **not** bind: `models:`, `exclude:`,
`provider.order`, `route: "fallback"`. An `exclude` naming the very model it had
just chosen still returned that model.

Cost is not academic: on identical-shaped input the router selected
`gpt-6-luna` at ~$0.0000068 and `gpt-6.1-sol` at ~$0.004046 — roughly 600x. The
catalogue lists `prompt=-1, completion=-1`, which is not what the account is
charged. **Budget it as a paid external oracle.**

Verdict: disqualified from authoritative or advisory-in-the-loop routing. Retained
for independent comparison only.

## 3. Local Jev-Style — deployed, CPU-first, isolated

```text
repo          chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF
revision      edf37c26a1098f83cf4264b8adbe0dca2d2ebb0c
license       apache-2.0
quant         Q8_0, sha256 cd87d284c4ee355cb0b3fce7391db57ba3ad108e45b3a41b058d529931a2bd5e
llama.cpp     441df11f65ea0b6d0c72965aaf70c8241070ddcb (built from source on the HTPC)
scorer        jev-score, built from jev_score.cpp sha256 90d848a6...
runtime       Python 3.12, tokenizers 0.23.2, numpy 2.5.3 (upstream's tested versions)
```

### Layout

```text
/opt/jevstyle/src      pinned sources, 0444
/opt/jevstyle/models   GGUF, root-owned read-only
/opt/jevstyle/build    llama.cpp + jev-score
/opt/jevstyle/venv     python env
/opt/jevstyle/logs     stderr
/run/jevstyle/ctl      control FIFO (RuntimeDirectory)
```

Service account `jevstyle` (system, nologin), separate from `laya`. No mutable
runtime state is shared with Laya. `CPUQuota=250%`, `MemoryHigh=3G`,
`MemoryMax=4G`, `ProtectSystem=strict`, `NoNewPrivileges`.

### No port is bound

The control channel is a private FIFO, not a TCP port. `:8012` therefore remains
free and no control surface is exposed to the network. Reachability is over the
SSH management lane only, which is the correct boundary for a shadow service.

### Measured footprint

```text
MemoryCurrent   2.0 GB          MemoryPeak  2.03 GB
TasksCurrent    18
GPU             none (ngl=0); only the pre-existing resident process appears
Laya            PID 295, NRestarts=0, untouched
```

## 4. Scorer contract — plain llama-server is not used

Decision probabilities come from the pinned scorer reading verdict-slot logits.
Per `readout_config.json`, the score is
`logit[' yes'] - logit[' no']` at the k-th ` ->` slot.

The slot ids (`9542`, `874`, `1411`) are **read from the pinned config at
runtime and verified against the tokenizer before the engine starts**. Nothing is
duplicated in our adapter. A mismatch raises.

The upstream `_result()` contract returns `answer`, `probabilities`, `scores`,
`temperature`, `top_probability`, `entropy_concentration`, `input_tokens`,
`head_tokens`. It has **no `score` and no `confidence` field**, and none is
invented. An earlier draft of the adapter guessed those names and returned
`None`; the mapping was corrected to the real contract rather than the contract
being bent to fit the guess.

Observed temperature `0.8800546821789332` from the pinned config.

## 5. Deployment defects found and fixed during qualification

Recorded because each would have shipped as a silent failure:

1. **stdin EOF terminated the service.** The control channel is a FIFO, so every
   writer that closes delivers EOF. The read loop exited and systemd restarted
   the service after each single request. Fixed by reopening and continuing.
2. **`ExecStartPre` `rm -f` unlinked the FIFO**, causing restart churn even when
   the service was healthy.
3. **`mkfifo` under `ProtectSystem=strict` failed** with status=1, because /opt
   is read-only to the service. Moved to `RuntimeDirectory` under /run.

Final qualification: 4 requests on one process, `NRestarts=0`, malformed input
returns a typed error rather than dying, and repeated identical requests are
bit-identical.

## 6. Comparison methodology

The three providers do **not** return the same kind of measurement. That is the
central methodological fact, and it was established by measurement:

| provider | native shape | normalisation used |
|---|---|---|
| Span | continuous `noul` 0..1, no distribution | none |
| Laya | graded `score` 0..4 + distribution + confidence | `/4` |
| Jev-Style | expected index 0..4 from scorer probabilities | `/4` |

Span also accepts only `noul` questions with plain-string criteria; a graded
`score` question returns HTTP 400 *"Respan only accepts noul questions whose
instructions and criteria are plain strings."*

Every retained result carries both the native value and the normalised value with
its method. Normalised numbers are never compared without their originating
scale.

## 7. Results (n=3 stored states — a lead, not a verdict)

```text
latency        laya p50 2079ms   span p50 538ms   jev scorer 6.0-6.8s (CPU)
MAD            jev-span 0.123    jev-laya 0.247   laya-span 0.368
rank concordance (of 10 pairs)
               jev-span 1/10     jev-laya 5/10    laya-span 4/10
top-ranked signal
               laya tool_dependency
               jev  route_sufficiency
               span task_complexity
```

Two things worth stating plainly:

- **Jev-Style tracks Span numerically** (lowest MAD of any pair) yet is **rank
  inverted against it** (1/10 concordance). Closest in magnitude, furthest in
  ordering.
- **All three disagree on which signal ranks highest.** No provider is shown to
  be correct. These are different measurements of the same bounded states, and
  n=3 cannot adjudicate between them.

The corpus is small because the sensor epoch is young. Expansion is automatic as
observations accumulate; no synthetic volume will be manufactured.

## 8. What remains unproven

- Whether any provider's ranking is *right*.
- Whether the Span/Jev rank inversion is a real property or an artefact of the
  different question types each provider requires.
- Sensitivity to question order, field order, and larger states.
- Q4_K_M parity against Q8_0 (deferred; reference first).
- Behaviour under concurrency and under scorer failure.
- Generalisation beyond the current epoch's task mix.

## 9. Promotion criteria — none met

Jev-Style has **no** live sensor hook, no AUX fallback, no judge use, no Output
Guard use and no routing authority. It stays that way until evidence supports a
change and the operator decides. A benchmark passing is not a promotion.
