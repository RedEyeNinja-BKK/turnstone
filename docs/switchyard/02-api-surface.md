# Switchyard — deployed API surface

Measured against the running process on `switchyard-vm.example.invalid` (`127.0.0.1:4000`),
2026-10-17. Method/path behaviour is from live probes; classification is from
what each route does.

> Status codes below are what an **unauthenticated, empty-body** probe returns.
> A `4xx` proves the route is registered; it does not prove the contract.
> `404` means no route is registered at that path.

---

## 1. Registered routes

| Method | Path | Probe | Class |
|---|---|---|---|
| GET | `/health` | 200 | pure read |
| GET | `/metrics` | 200 | pure read |
| GET | `/v1/models` | 200 | pure read |
| GET | `/v1/stats` | 200 | pure read |
| GET | `/v1/routing/session-stats` | 400 | read, requires params |
| POST | `/v1/chat/completions` | 415 | inference — costs tokens |
| POST | `/v1/responses` | 415 | inference — costs tokens |
| POST | `/v1/responses/compact` | 415 | inference — costs tokens |
| POST | `/v1/messages` | 415 | inference — costs tokens |
| POST | `/v1/messages/count_tokens` | 415 | token counting |
| POST | `/v1/decision` | 415 | routing decision (upstream shape) |
| POST | `/v1/decisions` | 400 | typed decision — **local extension** |
| POST | `/v1/embeddings` | 400 | capability, local extension |
| POST | `/v1/rerank` | 400 | capability, local extension |
| POST | `/v1/stats/reset` | 200 | **state mutation** — see §4 |

Not registered: `/v1/capabilities`, `/v1/rerankings` (both 404).

`415` on the inference routes means the body was empty and the content type was
rejected before deserialization. `400` on the local extension routes comes from
serde: `missing field \`model\``. Neither is an authentication failure — the
service has no inbound auth of its own.

---

## 2. Two decision surfaces, deliberately distinct

`/v1/decision` and `/v1/decisions` are **not** the same endpoint.

| | `/v1/decision` | `/v1/decisions` |
|---|---|---|
| Origin | upstream | local fork (`3c58600f`) |
| Shape | upstream decision request | typed decision, requires `model` |
| Backends | configured targets | decision lanes (`smartfree-decision`, `smartlocal-decision`) |

Do not conflate them in tooling or documentation. The local typed surface is
implemented "over the shared lane" per its commit message, but its request
schema differs.

---

## 3. Capability surfaces

`/v1/embeddings` and `/v1/rerank` are served by the fork (`24457cb5`). Both
reject an empty body with `missing field \`model\``.

Declared capabilities, from `/etc/switchyard/routes.toml`:

| Capability id | Target | Contract |
|---|---|---|
| `switchyard-embedding-local` | `embedding-local` | 1024-dim, L2, max batch 64, contract `localclaw-embedding-space:v1` |
| `switchyard-embedding-free` | `embedding-free` | 1024-dim, L2, max batch 64, contract `localclaw-embedding-space:lfm2.5-350m-v1` |
| `switchyard-rerank-local` | `reranker-local` | `top_n=5`, `max_candidates=64`, `max_doc_chars=1024`, `max_query_chars=512` |

`top_n` is enforced as a ceiling (`47598a3f`), not merely a default.

---

## 4. `/v1/stats/reset` is a mutation

A `POST` with no body returns **200** and clears the in-process statistics
snapshot. Observed effect on 2026-10-17:

| Surface | Effect |
|---|---|
| `/v1/stats` JSON | **reset** (`total_requests` fell from 3413 to 2) |
| Prometheus `switchyard_*_total` | **not reset** — continued to increase |

So the reset affects the stats snapshot, not the metric counters. It is still a
mutation and must never be exposed as an unguarded MCP control. It also means
`/v1/stats` and the Prometheus counters measure different windows and will
legitimately disagree after a reset.

---

## 5. CLI surface (verified from `--help` on the deployed binary)

```
switchyard-server [OPTIONS] --config <PATH>
  --config <PATH>              TOML: LLM clients, targets, algorithm routes
  --host <HOST>                default 0.0.0.0
  -p, --port <PORT>            default 4000
  --backlog <BACKLOG>          default 65535
  --shutdown-timeout <DUR>     default 30s
  --dry-run                    validate config without binding a socket
  --routing-log-file <PATH>    append per-request routing JSONL
  --tls-cert / --tls-key       PEM paths
  -h, --help / -V, --version
```

**There is no reload flag and no config-watch behaviour.** A configuration
change requires a restart. `--dry-run` is the native validation mechanism and
is the correct pre-deployment gate.

`--shutdown-timeout` defaults to 30s, so a restart drains for up to 30 seconds.
Plan restarts accordingly; do not assume an instant cutoff.

---

## 6. Authentication model

The service itself has no inbound authentication: `/v1/models`, `/metrics` and
`/health` are open, and inference routes are gated only by request validity.
Network exposure is therefore the control surface — the process binds
`127.0.0.1:4000` and is published through the tailnet listener only.

Provider credentials belong to Switchyard (`EnvironmentFile` entries
`/etc/switchyard/secrets/providers.env` and
`/var/lib/switchyard/oauth/switchyard-openai.env`). They are referenced by
`api_key_env` / `decision_api_key_env` in the manifest; no literal secret
appears in `routes.toml`.
