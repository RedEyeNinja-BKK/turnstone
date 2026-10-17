# Switchyard expertise package

Durable reference for the Switchyard deployed on `switchyard-vm`, written for a
Turnstone session that must act on it without repeating the investigation.

**Read the provenance document first.** The deployed build is a fork, not
upstream `v0.3.0`, and the difference determines what is safe to assume.

| Document | Answers |
|---|---|
| [01-provenance-and-divergence.md](01-provenance-and-divergence.md) | What is deployed, from which commit, and how it differs from upstream |
| [02-api-surface.md](02-api-surface.md) | Every registered endpoint, the CLI, and which operations mutate |
| [03-metrics-semantics.md](03-metrics-semantics.md) | What each metric actually counts, and what is not emitted |
| [04-routing-reasoning-fallback.md](04-routing-reasoning-fallback.md) | Route kinds, reasoning policy precedence, fallback rules, escalation |

---

## The five facts most likely to be got wrong

1. **`0.3.0` does not identify the build.** Deployment is commit
   `2c6e6df5` (2026-10-04), a fork of upstream `16cbe593` plus 28 local
   commits. Upstream `v0.3.0` is a *different* commit and is not contained in
   the deployment.
2. **`composites`, `decision_lanes`, `route_reasoning_policy`,
   `escalation_*`, typed `/v1/decisions` and the capability endpoints are
   fork-only.** Upstream documentation does not describe them.
3. **`/v1/decision` and `/v1/decisions` are different endpoints.** Do not
   conflate them.
4. **`POST /v1/stats/reset` mutates.** It clears the `/v1/stats` snapshot. It
   does not reset Prometheus counters, so the two windows diverge afterwards.
5. **No `provider` label exists**, and `gpt-6-luna` is the upstream id for two
   different clients (`openai`, `openai_nt`). Provider or thinking/non-thinking
   splits derived from metrics are approximations and must be labelled as such.

---

## Operational guardrails

- The service has **no inbound authentication**. Network placement is the
  control; `127.0.0.1:4000` plus the tailnet listener.
- There is **no reload command**. Config changes need a restart, draining up to
  `--shutdown-timeout` (default 30s). `--dry-run` is the native validate.
- **Switchyard owns routing, provider credentials, decision execution,
  embeddings and reranking.** Turnstone must not re-implement any of them.
- A contract, policy, credential or configuration failure stops a composite
  parent on purpose. Never present it as "route exhausted".

---

## Bounded runtime access

`scripts/turnstone_switchyard_runtime.py` is the read-only path a Turnstone
session uses to inspect the deployment. It runs as the service user — the SSH
key is already `turnstone:turnstone` 0600 — so it needs no sudo grant, no
launcher and no daemon. Every remote command is a module constant; the two
verbs that take input apply it only as an in-process filter. Nothing here
mutates the deployment: `validate` runs the binary's `--dry-run`.

| Verb | Returns |
|---|---|
| `status` | unit state, PID, start time, version, `/health`, manifest sha256 |
| `catalog` | routes, composites, targets, capabilities, composition references |
| `route` | one route or composite with resolved target and candidates (stdin JSON) |
| `stats` | `/v1/stats` totals and per-model calls/latency |
| `metrics` | declared metric families with type, help and sample counts |
| `validate` | `--dry-run` under the unit's own environment files |
| `log` | recent routing records, optionally filtered by `route_id` (stdin JSON) |

Exit codes: 0 success, 2 refused request, 3 transport failure, 4 remote
failure.

---

## Provenance of this package

| Fact | Source |
|---|---|
| Divergence, commit list | local clone of upstream `main` + the deployment's source bundle |
| Endpoints, CLI | live probes and `--help` on the deployed binary |
| Metric labels | `/metrics` on the running process |
| Reasoning/fallback semantics | fork source: `reasoning_policy.rs`, `config.rs`, `client.rs`, `composition.rs` |
| Routing log fields | `/var/log/switchyard/routing.jsonl` |

Reviewed: 2026-10-17. Re-verify the provenance table before trusting any of it
after a Switchyard upgrade.
