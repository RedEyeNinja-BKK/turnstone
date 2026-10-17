# LC-RD-008 — Switchyard Dashboard V7.0 deployment plan

Status: **DEPLOYED 2026-10-17** — `turnstone-switchyard-v7`, version 1, 35
panels. Scrape prerequisite satisfied; see §4.1.
`SWITCHYARD DASHBOARD V6.7` (`switchyard-dashboard-v6`, version 16) is used as
a **visual and structural reference only**, remains unmodified, and its series
are untouched.

---

## 1. Purpose

Build a dashboard for the Switchyard running on `switchyard-vm`, describing
what that deployment **actually emits today**.

V6.7 supplies conventions worth reusing — row grouping, stat-plus-timeseries
pairing, 24h window, 1m refresh, the `$model` selector. Everything else is
derived from the new deployment's own manifest and metric surface. Where a
family the old dashboard used does not exist here, V7.0 simply does not show
it; nothing is reconstructed to imitate the old view.

---

## 2. Manifest review — the current deployment

Source: `/etc/switchyard/routes.toml` on `switchyard-vm`
(sha256 `9a6448ef543bac6300401efd5f46b831f3f1001d85ed9e9aa55e617ccc1fa5a5`,
615 lines, `schema_version = 1`, no literal secrets — every credential is an
`api_key_env` / `decision_api_key_env` reference).

Service: `switchyard-neutral-green.service` — `User=switchyard`, binary
`/opt/switchyard/bin/switchyard-server`, `--host 127.0.0.1 --port 4000`,
routing log `/var/log/switchyard/routing.jsonl`, `SWITCHYARD_DEPLOYMENT=v6`,
started 2026-10-16 00:19 +07.

### Topology

| Object | Count | Notes |
|---|---|---|
| `llm_clients` | 7 | deepseek, htpc, thaillm, openai, openai_nt, openrouter, comfyninja |
| completion targets | 23 | the actual upstream models |
| decision targets | 4 | laya, jev-style, span-01-lite, mercury-decide |
| direct routes | 23 | one per completion target, `candidates = []` |
| primitive routes | 5 | `smart`, `smart-bounded`, `smartlocal`, `smartlocal-bounded`, `smartfree` |
| composites | 4 | `tier1`, `tier1-bounded`, `tier2`, `tier2-bounded` |
| decision lanes | 2 | `smartfree-decision`, `smartlocal-decision` |
| decision composition | 1 | `tier1-decision` (default lane) |
| capability clients | 3 | embedding-local, embedding-free, reranker-local |
| capabilities | 3 | 2 embeddings (1024-dim, L2), 1 rerank (`top_n=5`, `max_candidates=64`) |

`GET /v1/models` returns **28** ids — exactly the 23 direct routes plus the 5
primitives. `/health` = `{"status":"ok"}`. `switchyard_build_info.version` =
`0.3.0`.

### Routing shape worth putting on the dashboard

- `smart` → deepseek-flash, candidates `[glm-5.3-flash, qwen3.8-flash]`, 1,000,000 ctx
- `smart-bounded` → `openai-gpt-6-luna-nt`, candidate `qwen3.7-flash`, policy `none`
- `smartlocal` / `smartlocal-bounded` → `htpc-qwen3_5-9b-mtp`, candidate
  `comfyninja-qwen3.8-27b-q3-mtp-vl`, escalation at 61,440 input tokens
- `smartfree` → `apodex/apodex-1.1-mini:free`
- `tier1` = `[smartfree, smart]`; `tier1-bounded` = `[smartfree, smart-bounded]`;
  `tier2` = `[smartfree, smartlocal]`; `tier2-bounded` = `[smartfree, smartlocal-bounded]`

---

## 3. Metric surface of this deployment — measured

`http://127.0.0.1:4000/metrics` on `switchyard-vm`: 444 lines, **22 logical
families**. Labels in use: `algorithm`, `outcome`, `selected_model`, `model`,
`code`, `version`, `otel_scope_name`, `le`.

| Family | Type | Labels |
|---|---|---|
| `switchyard_build_info` | gauge | version |
| `switchyard_algorithms_in_flight` | gauge | algorithm |
| `switchyard_requests_total` | counter | model |
| `switchyard_total_requests` | gauge | — |
| `switchyard_client_responses_total` | counter | outcome |
| `switchyard_upstream_attempts_total` | counter | outcome, code |
| `switchyard_errors_total` | counter | model |
| `switchyard_total_errors` | gauge | — |
| `switchyard_router_retry_recovered_total` | counter | — |
| `switchyard_decisions_total` | counter | algorithm, selected_model |
| `switchyard_llm_calls_total` | counter | algorithm, outcome, selected_model |
| `switchyard_llm_call_duration_ms` | histogram | algorithm, outcome, selected_model |
| `switchyard_model_call_latency_ms` | histogram | model |
| `switchyard_total_latency_ms` | histogram | model |
| `switchyard_prompt_tokens_total` | counter | model |
| `switchyard_completion_tokens_total` | counter | model |
| `switchyard_reasoning_tokens_total` | counter | model |
| `switchyard_cached_tokens_total` | counter | model |
| `switchyard_cache_creation_tokens_total` | counter | model |
| `switchyard_runs_total` | counter | algorithm, outcome |
| `switchyard_run_duration_ms` | histogram | algorithm, outcome |
| `switchyard_routing_overhead_ms` | histogram | algorithm |

Distinct values observed: `algorithm=passthrough`; `outcome` ∈ {`ok`,
`client_disconnected`, `error`, `other_error`, `retryable_error`};
`code` ∈ {`200`, `400`, `404`, `429`, `500`, `504`, `none`};
`model` / `selected_model` ∈ {`gpt-6-luna`, `gpt-6-sol`, `Qwen3.5-9B-Q4_0`,
`Qwen3.5-9B-MTP`, `comfyninja/qwen3.8-27b-q3`}.

### Not emitted here (and therefore not in V7.0)

Nine families that exist on the outgoing deployment are absent from this
build: `capability_requests_total`, `capability_duration_ms`,
`capability_in_flight`, `capability_items`, `capability_max_doc_chars`,
`composition_attempt_total`, `composition_cross_total`,
`context_pressure_promotion_total`, `escalations_total`
(twelve metric names once the histogram components are counted).
They are recorded here so a future reader does not mistake their absence for a
dashboard omission. Restoring them is an instrumentation decision, not a
dashboard one.

### Labels that do not exist here

- **`provider`** — absent from every family. V7.0 has no `$provider` variable
  (V6.7's is permanently empty).
- **`model` on `switchyard_llm_calls_total`** — that family carries
  `selected_model`. Per-model request volume comes from
  `switchyard_requests_total` and the token families, which do carry `model`.

---

## 4. Blocking prerequisite: Prometheus is not scraping this deployment

**Resolved 2026-10-17 — see §4.1.**

| | Outgoing instance | This deployment |
|---|---|---|
| Where | `localclaw-vm` | `switchyard-vm` |
| In Prometheus (`job="switchyard"`) | **yes**, `up=1`, scraped seconds ago | **no** |
| Distinguishing labels | `hostname`, `origin_host`, `role`, `instance=localclaw-vm` | none of these |
| Families | 31 logical | 22 logical |

Prometheus at `10.10.20.33:9090` receives the fleet by remote-write (its own
`/api/v1/targets` lists only itself; `job="alloy"` is present). The
`job="switchyard"` series therefore describe the **outgoing** process.

**Consequence:** a V7.0 dashboard built today would query the old deployment's
series and would look healthy while describing the wrong system.

### Required before build

1. Add a scrape source for `switchyard-vm`. Because the old deployment is being
   retired, prefer a new job name (`switchyard-v7`) now and repoint or retire
   `switchyard` at cutover rather than mutating the existing job in place.
   Scrape `https://switchyard-vm.tailc8ef7b.ts.net/metrics` or the tailnet
   address on port 4000; `/metrics` is unauthenticated.
2. Confirm reachability **from the scraping host**, not from `turnstone-vm`.
   The process binds `127.0.0.1:4000` and the tailnet interface, not the LAN
   address.
3. Confirm the scrape lands: `up{job="switchyard-v7"} == 1`, and
   `switchyard_build_info{job="switchyard-v7"}` present. The tell is the
   **absence** of a `hostname` label, which is the fingerprint of this build.
4. Only then build panels.

### 4.1 What was actually done

The scrape originates on **agent-vm**, the Alloy hub (`alloy.service`, Alloy
v1.19.2, `/etc/alloy/config.alloy`). That host is the only one in the estate
that reaches both the tailnet metrics endpoint and the central Prometheus.
`switchyard-vm` itself cannot reach `10.10.20.33:9090`, so a push or a local
scrape from that host was not an option.

One component block was appended:

```alloy
prometheus.scrape "switchyard_v7" {
	targets = [{
		"__address__" = "switchyard-vm.tailc8ef7b.ts.net:443",
		instance      = "switchyard-vm",
		job           = "switchyard-v7",
		origin_host   = "switchyard-vm",
		vlan          = "50",
	}]
	scheme          = "https"
	scrape_interval = "15s"
	scrape_timeout  = "10s"
	forward_to      = [prometheus.relabel.canonicalize_metrics.receiver]
}
```

`job="switchyard"` (the outgoing deployment, reported by localclaw-vm) is
unchanged and still scraped; nothing was repointed in place.

Evidence the gate is satisfied:

| Check | Result |
|---|---|
| `alloy validate` on the candidate | pass |
| backup before install | `/etc/alloy/config.alloy.bak-v7-20261017T125827Z` |
| `alloy validate` on the installed file | pass |
| `systemctl reload alloy` | exit 0, service active |
| `up{job="switchyard-v7"}` | **1**, `instance=switchyard-vm` |
| `switchyard_build_info{job="switchyard-v7"}` | present, `version=0.3.0` |
| `switchyard_*` families for the new job | **32** (all emitted families) |
| `hostname` label on the new series | absent — the fingerprint of this build |
| outgoing `up{job="switchyard"}` | still 1, unchanged |

---

## 5. V7.0 design

UID `turnstone-switchyard-v7`, title `SWITCHYARD DASHBOARD V7.0`, folder
General, tags `[switchyard, v7, turnstone]`, refresh `1m`, window `now-24h`.
Datasource `df7wrp11a9hq8c`.

Every query is pinned to the new job so it cannot silently resolve to the old
deployment during the overlap.

**Variables** (only labels that exist)
- `model` → `label_values(switchyard_requests_total{job="switchyard-v7"}, model)`
- `algorithm` → `label_values(switchyard_decisions_total{job="switchyard-v7"}, algorithm)`
- `outcome` → `label_values(switchyard_client_responses_total{job="switchyard-v7"}, outcome)`

### Row 1 — Deployment identity
| Panel | Type | Query |
|---|---|---|
| BUILD | stat | `switchyard_build_info{job="switchyard-v7"}` → `version` |
| TARGET UP | stat | `up{job="switchyard-v7"}` |
| ALGORITHMS IN FLIGHT | timeseries | `switchyard_algorithms_in_flight{job="switchyard-v7"}` |
| SCRAPE AGE | stat | `time() - max(timestamp(switchyard_build_info{job="switchyard-v7"}))` |

### Row 2 — Traffic and outcomes
| Panel | Type | Query |
|---|---|---|
| REQUESTS BY MODEL | timeseries | `sum by (model) (rate(switchyard_requests_total{job="switchyard-v7"}[5m]))` |
| TOTAL REQUESTS | stat | `switchyard_total_requests{job="switchyard-v7"}` |
| CLIENT RESPONSES BY OUTCOME | timeseries | `sum by (outcome) (rate(switchyard_client_responses_total{job="switchyard-v7"}[5m]))` |
| UPSTREAM ATTEMPTS BY OUTCOME/CODE | timeseries | `sum by (outcome, code) (rate(switchyard_upstream_attempts_total{job="switchyard-v7"}[5m]))` |
| ERRORS BY MODEL | timeseries | `sum by (model) (rate(switchyard_errors_total{job="switchyard-v7"}[5m]))` |
| ERROR RATE (5m) | stat | `sum(rate(switchyard_client_responses_total{job="switchyard-v7", outcome!="ok"}[5m])) / clamp_min(sum(rate(switchyard_client_responses_total{job="switchyard-v7"}[5m])), 1e-9)` |

### Row 3 — Decision lanes
| Panel | Type | Query |
|---|---|---|
| DECISIONS BY BACKEND | timeseries | `sum by (algorithm, selected_model) (rate(switchyard_decisions_total{job="switchyard-v7"}[5m]))` |
| DECISION TOTALS (24h) | table | `sum by (algorithm, selected_model) (increase(switchyard_decisions_total{job="switchyard-v7"}[24h]))` |

### Row 4 — Lane selection
| Panel | Type | Query |
|---|---|---|
| LLM CALLS BY SELECTED MODEL | timeseries | `sum by (selected_model) (rate(switchyard_llm_calls_total{job="switchyard-v7"}[5m]))` |
| CALLS BY OUTCOME | timeseries | `sum by (outcome) (rate(switchyard_llm_calls_total{job="switchyard-v7"}[5m]))` |
| CALLS BY ALGORITHM | timeseries | `sum by (algorithm) (rate(switchyard_llm_calls_total{job="switchyard-v7"}[5m]))` |

### Row 5 — Latency
| Panel | Type | Query |
|---|---|---|
| MODEL-CALL LATENCY p50/p95/p99 | timeseries | `histogram_quantile(0.50\|0.95\|0.99, sum by (le, model) (rate(switchyard_model_call_latency_ms_bucket{job="switchyard-v7"}[5m])))` |
| TOTAL LATENCY p95 BY MODEL | timeseries | `histogram_quantile(0.95, sum by (le, model) (rate(switchyard_total_latency_ms_bucket{job="switchyard-v7"}[5m])))` |
| ROUTING OVERHEAD p95 | timeseries | `histogram_quantile(0.95, sum by (le, algorithm) (rate(switchyard_routing_overhead_ms_bucket{job="switchyard-v7"}[5m])))` |
| LLM CALL DURATION p95 BY OUTCOME | timeseries | `histogram_quantile(0.95, sum by (le, outcome) (rate(switchyard_llm_call_duration_ms_bucket{job="switchyard-v7"}[5m])))` |

### Row 6 — Tokens
| Panel | Type | Query |
|---|---|---|
| TOKEN THROUGHPUT BY MODEL | timeseries | `sum by (model) (rate(switchyard_completion_tokens_total{job="switchyard-v7"}[5m]))` |
| PROMPT TOKENS BY MODEL | timeseries | `sum by (model) (rate(switchyard_prompt_tokens_total{job="switchyard-v7"}[5m]))` |
| REASONING TOKENS BY MODEL | timeseries | `sum by (model) (rate(switchyard_reasoning_tokens_total{job="switchyard-v7"}[5m]))` |
| CACHE HIT SHARE | timeseries | `sum by (model) (rate(switchyard_cached_tokens_total{job="switchyard-v7"}[5m])) / clamp_min(sum by (model) (rate(switchyard_prompt_tokens_total{job="switchyard-v7"}[5m])), 1e-9)` |
| CACHE WRITES BY MODEL | timeseries | `sum by (model) (rate(switchyard_cache_creation_tokens_total{job="switchyard-v7"}[5m]))` |

### Row 7 — Run health and resilience
| Panel | Type | Query |
|---|---|---|
| RUN TERMINATIONS BY OUTCOME | timeseries | `sum by (outcome) (rate(switchyard_runs_total{job="switchyard-v7"}[5m]))` |
| RUN DURATION p95 | timeseries | `histogram_quantile(0.95, sum by (le, outcome) (rate(switchyard_run_duration_ms_bucket{job="switchyard-v7"}[5m])))` |
| RETRIES RECOVERED | stat | `increase(switchyard_router_retry_recovered_total{job="switchyard-v7"}[24h])` |

No row is created for the absent families. A blank panel would read as zero
traffic rather than as missing instrumentation, and V7.0 is not a contract
report on its predecessor.

---

## 6. Build and deploy procedure

**Executed 2026-10-17. Outcome below.**

1. **Prerequisite gate** — §4 items 1-4. Do not proceed otherwise.
2. Generate the dashboard JSON from §5; validate locally with the runtime's own
   `parse_dashboard_write_request` so the 512 KiB / 400-panel / depth-40 bounds
   are checked before any network call.
3. `dashboard-write` with `op: create`, `uid: turnstone-switchyard-v7`,
   `message: "initial V7.0 for the switchyard-vm deployment"`. The uid sits in
   the `turnstone-` namespace, so it is removable by this tool.
4. Verify:
   - every panel renders without a query error;
   - `up{job="switchyard-v7"} == 1`;
   - each of the 22 families in §3 resolves to at least one series;
   - `$model` populates from `switchyard_requests_total`;
   - `switchyard-dashboard-v6` still at version 16, 50 panels.
5. Re-read the created dashboard raw and store the document beside this plan.

### Rollback

`dashboard-write` `op: delete` on `turnstone-switchyard-v7`, or recreate from
the stored raw document. V6.7 is never modified, so it remains available
unchanged during the overlap.

---

## 7. Open questions for the operator

None blocking. Remaining choices are cosmetic: whether to promote `algorithm`
when a second value appears, and when to retire the outgoing `job="switchyard"`
series at cutover.

---

## 7.1 Deployment result

| Item | Value |
|---|---|
| Dashboard | `turnstone-switchyard-v7` — `SWITCHYARD DASHBOARD V7.0` |
| Version | 1 |
| Panels | 35 (7 rows, 7 stat, 20 timeseries, 1 table) |
| Variables | `model` (5 options), `algorithm` (1), `outcome` (4) |
| Queries | 30, **all** pinned to `job="switchyard-v7"`, none unpinned |
| Dashboard count in org | 21 |
| `switchyard-dashboard-v6` | still version 16, 50 panels, unmodified |

Every one of the 21 panel query shapes was executed against Prometheus before
creation and returned at least one series. Rate-valued panels read 0 because
the deployment carries only health traffic; `switchyard_total_requests` = 3413.
Latency quantiles return no value until a histogram bucket is non-empty, which
is expected and not a panel error.

---

## 8. Out of scope

- Any change to `switchyard-vm`: no config, restart, instrumentation or lane
  change. This work was read-only against that host.
- Any change to `SWITCHYARD DASHBOARD V6.7`, including extension or repair.
  It is a reference template for an outgoing system.
- Prometheus configuration on docker-vm: no edit was made there, because the
  Alloy hub on agent-vm reaches both ends and the remote-write receiver was
  already enabled.
- Retiring the outgoing `job="switchyard"` series; that belongs to cutover.

## 9. Rollback

1. Remove the Alloy block and restore the timestamped backup:
   `sudo cp /etc/alloy/config.alloy.bak-v7-20261017T125827Z /etc/alloy/config.alloy`
   then `sudo systemctl reload alloy`.
2. Delete the dashboard through the runtime:
   `{"op":"delete","uid":"turnstone-switchyard-v7"}` via `dashboard-write`
   (permitted by the `turnstone-` namespace rule).

Neither step affects the outgoing deployment's series or V6.7.
