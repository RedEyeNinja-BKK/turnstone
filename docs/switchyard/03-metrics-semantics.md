# Switchyard — metric semantics

The metrics are the only live truth the dashboard and the MCP may rely on.
Validate every reading against the deployed binary; upstream documentation
describes a different commit.

---

## 1. The four counters that look alike

One client request can produce several candidate calls, several upstream HTTP
attempts, and several model observations. These are separate counters and must
not be treated as interchangeable.

| Metric | Type | What it counts |
|---|---|---|
| `switchyard_requests_total` | counter | routed answer candidates, labelled by `model` |
| `switchyard_total_requests` | gauge | cumulative count exposed as a single scalar |
| `switchyard_client_responses_total` | counter | responses returned to the client, by `outcome` |
| `switchyard_upstream_attempts_total` | counter | upstream HTTP attempts, by `outcome` and `code` |
| `switchyard_llm_calls_total` | counter | model-call observations, by `algorithm`, `outcome`, `selected_model` |

`switchyard_total_requests` is **not** incoming HTTP client requests. Upstream
documents it as a cumulative gauge for routed answer candidates. Observed on
2026-10-17: gauge 3846 while per-model `requests_total` summed to a different
number over a different window.

---

## 2. Label reality on this deployment

Labels present: `algorithm`, `outcome`, `selected_model`, `model`, `code`,
`version`, `otel_scope_name`, `le`.

**No `provider` label exists on any family.** A provider breakdown must be
derived from a `model` regex.

**`model` vs `selected_model` are on different families:**

- `model` — `requests_total`, `prompt_tokens_total`, `completion_tokens_total`,
  `reasoning_tokens_total`, `cached_tokens_total`,
  `cache_creation_tokens_total`, `model_call_latency_ms`, `total_latency_ms`,
  `errors_total`
- `selected_model` — `llm_calls_total`, `decisions_total`,
  `llm_call_duration_ms`

**Collision:** `gpt-6-luna` is the upstream id for both
`direct-openai-gpt-6-luna` and `direct-openai-gpt-6-luna-nt`. Thinking and
non-thinking traffic on that provider cannot be separated by label. Do not
pretend otherwise in a panel.

---

## 3. Families the deployment does not emit

Referenced by the outgoing deployment's dashboard, absent here:

```
capability_requests_total          capability_duration_ms
capability_in_flight               capability_items
capability_max_doc_chars           composition_attempt_total
composition_cross_total            context_pressure_promotion_total
escalations_total
```

Their absence is an instrumentation gap, not a zero. A panel that renders them
would draw an empty graph that reads as "no traffic".

---

## 4. Outcome vocabulary

Observed values: `ok`, `client_disconnected`, `error`, `other_error`,
`retryable_error`.

Do not assume `error` and `other_error` mean the same thing, and do not compute
"error rate" as `outcome != "ok"` without stating that `client_disconnected` is
included. `code` carries the upstream HTTP status, including the literal
`none`, which means no HTTP response was obtained.

---

## 5. Histograms

Latency is exposed as classic histograms (`_bucket` / `_count` / `_sum`).
`histogram_quantile` returns **no value** until at least one bucket is
populated. On an idle deployment every latency panel is legitimately empty;
that is not a query error and must not be "fixed" with a default.

---

## 6. What `/v1/stats` adds

`/v1/stats` exposes a per-model snapshot with values Prometheus does not carry:
`max_observed_context_tokens`, `avg_prompt_tokens`, `cache_hit_rate`,
`theoretical_cache_hit_rate`, `request_pct`, `token_pct`.

It is a **different window** from the Prometheus counters and is cleared by
`POST /v1/stats/reset`. Use it for per-model context and cache insight, never
as a request total.
