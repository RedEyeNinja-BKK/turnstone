# Switchyard — routing, reasoning policy and fallback

Every claim here is from the fork's source at
`2c6e6df53e9dbac9f6121d9780e4c09930d51b40` or from live observation.

---

## 1. Route kinds

| Kind | How it is expressed | Example |
|---|---|---|
| direct | `[routes.direct-*]`, `candidates = []` | `direct-openai-gpt-6-luna` |
| primitive | `[routes.*]` with `candidates = [...]` | `smart`, `smartfree` |
| composite / tier | `[composites.*]` with `children = [...]` | `tier1` |

A primitive route is a passthrough whose target is the first leg and whose
`candidates` are ordered fallbacks.

> `smartfree` declares `candidates = []`, so "has candidates" is **not** a
> reliable way to tell primitives from direct routes. Classify by the
> `direct-` name prefix instead.

---

## 2. Reasoning policy — two separate controls

### Target level (`reasoning_policy` / `reasoning_effort`)

From `crates/protocol/src/reasoning_policy.rs`:

```
ReasoningPolicy = Unspecified | Disabled | Enabled
```

`Disabled` encodes the dialect's real off-knob:

| Dialect | Wire body for `Disabled` |
|---|---|
| `openai_effort` | `{"reasoning": {"effort": "none"}}` |
| `openrouter_enabled` | `{"reasoning": {"enabled": false}}` |
| `llama_cpp_enable_thinking` | `{"chat_template_kwargs": {"enable_thinking": false}}` |

This is a genuine provider command, not a rejection of reasoning controls.
A target's policy is applied **after** the caller's fields and after any
`extra_body` default, so the target's choice wins.

Configuration loading **fails closed**: a specified policy with no declared
dialect is rejected, because it would otherwise emit a knob nobody declared.

### Route level (`route_reasoning_policy`)

```
RouteReasoningPolicy = Inherit | Medium | High | None
```

`None` maps to `ReasoningPolicy::Disabled`. The client applies it **last**:

> "A route's explicit policy outranks the target's default and the caller's
> effort. Applied last so the route is genuinely authoritative."

Validation walks every callable target of the route (skipping targets that
declare `reasoning_effort`, which already pin a level) and refuses to load if
the dialect cannot honour the policy.

**Consequence for `-bounded`:** on a route that declares `none`, the control is
real. Live evidence: `smart-bounded` calls record `reasoning_tokens = 0` with
`model = gpt-6-luna`, i.e. the same physical model that records
non-zero reasoning on the unbounded route.

---

## 3. Composition does **not** propagate policy to children

From `crates/switchyard-runner/src/composition.rs`:

> "Each child receives the pristine request. Whatever the previous child
> prepared for its provider is deliberately dropped, so a child cannot inherit
> another child's model, body or reasoning fields."

Therefore a tier's own `route_reasoning_policy` — even if one were declared —
would not reach its children. Each child applies its own.

### The resulting gap

```
tier1-bounded       = [smartfree, smart-bounded]
tier1               = [smartfree, smart]
tier2-bounded       = [smartfree, smartlocal-bounded]
tier2               = [smartfree, smartlocal]
```

`smartfree` declares `route_reasoning_policy = "inherit"`. So in every
`-bounded` tier the free leg is **not** forced non-thinking; only the second
child is. The bounded tiers are therefore not genuinely bounded across the
whole execution path.

Fixing this needs either:

1. a `smartfree-bounded` primitive over the same physical models with
   `route_reasoning_policy = "none"`, referenced from the bounded tiers
   (narrow, no fork change), or
2. composition propagating a parent policy (fork change, wider blast radius).

Option 1 is the smaller change and is consistent with "do not recreate separate
physical `-nt` pins" — the physical models stay shared; only the policy differs.

---

## 4. Fallback and retry

Both trees implement these; the fork did not change them:

| Control | Where | Meaning |
|---|---|---|
| `fallback_on_rate_limit` | `llm_clients.*` (default `true`) | after HTTP 429 exhausts retries, try the next completion candidate |
| `max_retries` | `llm_clients.*` | per-target retry budget |
| `failure_cooldown_ms` | `llm_clients.*` | target unavailable window after a failure |

With `fallback_on_rate_limit = false`, a 429 does **not** start cooldown and the
429 is returned.

Three outcomes are kept distinct in composition and must not be conflated:

| Outcome | Meaning |
|---|---|
| never eligible | capability/context mismatch; the child received **no** provider call |
| eligible then transient failure | child exhausts its own candidates first, then yields to the next child |
| eligible then contract/policy/credential failure | stops the parent — retrying elsewhere would hide the defect |

That third rule is why an authentication failure must never be presented as
"route exhausted".

---

## 5. Context-pressure escalation

`escalation_max_input_tokens` and `escalation_target` must be declared
**together**; a zero threshold is rejected. The escalation leg must appear
among the route's candidates, and only real promotions are reported.

Present on `smartlocal` and `smartlocal-bounded`:

```
escalation_max_input_tokens = 61440
escalation_target = comfyninja-qwen3_8-27b-q3-mtp-vl
```

This is enforced by reordering before dispatch, not by letting the request fail.

---

## 6. Capability filtering

Ordered candidates are filtered by target capability **before** dispatch
(`694e43ec`). An ineligible candidate is skipped rather than called, so:

- a text-only candidate is never handed an image request;
- a route's `vision = true` must still be honest, because `/v1/models` derives
  capability from the route declaration, not from the candidate set.

Verify heterogeneous-candidate behaviour with a live probe before relying on
it; the config warning that "`vision = true` only when every candidate supports
image input" is an upstream schema note, and this fork filters instead.

---

## 7. Routing log fields (fork extension)

`--routing-log-file` writes one JSON object per request. Fields observed live:

```
ts, route_id, algorithm, model, tier, child_route, cross_child_reason,
final_target, prompt_tokens, completion_tokens, reasoning_tokens,
cached_tokens, cache_creation_tokens, total_tokens,
session_id, trial_id, task, origin
```

`child_route`, `tier` and `cross_child_reason` are the composition telemetry.
**They exist only in this file — they are not Prometheus labels.** Any
candidate-level or tier-level panel must either read the log or accept that the
metric surface cannot show it.
