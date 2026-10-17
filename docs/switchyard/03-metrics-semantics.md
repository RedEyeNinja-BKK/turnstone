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

## 3. Families genuinely absent from this build

Referenced by the outgoing deployment's dashboard but not implemented here:

```
capability_requests_total          capability_duration_ms
capability_in_flight               capability_items
capability_max_doc_chars           context_pressure_promotion_total
escalations_total
```

The capability families matter: **embedding and rerank calls emit no Prometheus
metrics on this build.** Their traffic is invisible here. The runtime script's
`metrics` verb only re-reads `/metrics`, so it cannot expose them either; the
capability endpoints have to be exercised directly (`POST /v1/embeddings`,
`POST /v1/rerank`) to observe them.

Their absence is an instrumentation gap, not a zero. A panel that renders them
would draw an empty graph that reads as "no traffic".

### Absent series is not absent family

A Prometheus family appears in `/api/v1/label/__name__/values` only once some
series exists. `composition_attempt_total` and `composition_cross_total` are
implemented, but a deployment that has never served a composite request has no
series for them. Check "implemented" against the binary, never against series
presence on an idle deployment — the two are routinely confused, and this
document itself previously recorded them as missing for that reason.

---

## 3a. Composition telemetry

Whole-route composites emit two bounded counters. Both are populated on the
current deployment and cover requested tier, selected child, physical model,
failure class and promotion reason without any high-cardinality label.

**Provenance.** These are emitted by the deployed Switchyard binary at commit
`2c6e6df5`, not by anything in this repository. Verified two ways on
2026-10-17: the live `/metrics` endpoint on `switchyard-vm.example.invalid`, and the fork source
`crates/switchyard-server/src/metrics.rs` (`record_composition_attempt`,
`record_composition_cross`) plus its only call site in `lib.rs`. Re-verify
against the binary after any Switchyard upgrade.

```
switchyard_composition_attempt_total{parent_route, child_route, target, outcome, capability}
switchyard_composition_cross_total{parent_route, from_child, to_child, reason}
```

`outcome` is one of `selected`, `skipped_ineligible`, `attempt_failed`.
For `selected`, `target` is the model that actually served. For a skip or a
failure, `target` is empty and `capability` carries the reason:
`vision` for a modality skip, or the failure class (`rate_limited`,
`temporarily_unavailable`, `transport`, `timeout`, `server`, `context_window`,
`provider_policy`, `auth`, `request`, `other`) for a failure.

`composition_cross_total.reason` is that same failure class, so a promotion is
attributable without the routing log.

What these counters still cannot say:

* **Per-request sequence.** The counters are aggregate. "Child A failed, then
  B failed, then C served" is only recoverable from the routing log, and only
  as first-failure attribution (`cross_child_reason` names one child, not a
  chain).
* **Which request.** No request, session or prompt identifier appears in any
  label — by design.
* **Bounded-reasoning effect.** `route_reasoning_policy` is manifest config,
  not telemetry. `reasoning_tokens_total{model}` shows the effect, not the
  policy that produced it.

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
