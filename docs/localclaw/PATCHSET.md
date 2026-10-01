# LocalClaw patchset manifest

Mechanically auditable record of every LocalClaw commit carried on
`integration/localclaw-v1.8.5-sensor-20261001` that upstream `dev` does not.

- Base: upstream `dev` merge-base `9bec9aadcee0185e064257c139a9b8a449315f01`
- Upstream `dev` at recording: `e6679137a6404f563936de91de66c11c5563a927`
- Recorded: 2026-10-01
- Commits ahead of upstream: 41, of which **36 are local-only** (patch-id not
  present upstream) and 5 are already upstream (see *Retire candidates*).

Re-derive the classification:

```bash
git fetch upstream dev
for sha in $(git rev-list $(git merge-base HEAD upstream/dev)..HEAD); do
  git show "$sha" > /tmp/p.diff
  echo "$(git patch-id --stable < /tmp/p.diff | cut -d' ' -f1) $sha"
done | sort > /tmp/mine-pids.txt
# compare the patch-id column against the same computed over `git rev-list upstream/dev`
```

`status` values:

| status | meaning |
|---|---|
| `required` | production depends on it; no upstream equivalent exists |
| `retire candidate` | equivalent or superseded upstream; safe to drop on next rebase |
| `bootstrap` | lineage/plumbing graft, kept for reproducibility |
| `superseded local` | kept for history, no longer load-bearing |

---

## 1. Output Guard typed-decision seam

| field | value |
|---|---|
| commits | `9ed545da` `26a799b1` `82838855` `8a2a81c7` `b66c5aeb` `700423b4` |
| purpose | run the Output Guard against a typed, non-chat decision backend instead of a generative chat model |
| files | `core/output_guard_judge.py`, `core/typed_decision.py` |
| upstream overlap | #1201 (provider boundary), #1213 (provider descriptors), #1214 (multiple backends) |
| status | `required` |
| tests | `tests/test_output_guard_typed_decision.py` |
| production dependency | **yes** — `judge.output_guard_model` resolves to the AUX lane |

LocalClaw-specific detail: the guard reads typed aliases through the **real**
registry accessor (`82838855`) and fails closed when the registry cannot be read
(`8a2a81c7`). `700423b4` removes a misleading log line that claimed a
session-model fallback which can never occur on the typed path.

## 2. Shadow sensor (local experimental infrastructure)

| field | value |
|---|---|
| commits | `578ccb7d` `8d6bd4a4` `8b8561c1` |
| purpose | advisory, no-authority observation of workstream state; see [SENSOR.md](SENSOR.md) |
| files | `core/bounded_state.py`, `core/exact_tokens.py`, `core/sensor_layer.py`, `core/sensor_lifecycle.py`, `core/sensor_questions.py`, `core/shadow_driver.py`, `core/shadow_observation.py`, `core/validation_corpus.py`, 7 test modules |
| upstream overlap | none — upstream `dev` has no sensor, shadow or bounded-state module |
| status | `required` (production-active) / **not proposed upstream** |
| tests | 238 passed, 1 skipped from a clean checkout |
| production dependency | **yes** — all five nodes run `1.8.5-8d6bd4a4-sensor-lane` |

`8b8561c1` exists because the qualification suite had only ever been committed on
the 1.8.4-based `fix/output-guard-typed-decision-20260928` branch; the deployed
lineage shipped the sensor with no tests covering it.

## 3. Responses reasoning replay + round repair

| field | value |
|---|---|
| commits | `02db136d` `fc611fb9` `ee794cb6` `83377813` `94116d7b` `cfe1b29c` |
| purpose | replay stored reasoning on the Responses wire and repair in-round assistant turns so strict-thinking backends do not 400 |
| files | `core/history_decoration.py`, `core/model_turn.py`, `core/providers/_openai_responses.py` |
| upstream overlap | #1202 (reasoning replay) |
| status | `required` |
| tests | responses round-repair and golden modules on the integration branch |
| production dependency | **yes** |

## 4. Compaction: non-empty in-round reasoning contract

| field | value |
|---|---|
| commits | `8c35c50c` `34fa4a70` `2dfe2b32` |
| purpose | stamp a non-empty placeholder for in-round assistant turns lacking reasoning; scope the invariant to string-or-absent values |
| files | `core/history_decoration.py`, `core/model_turn.py` |
| upstream overlap | #1202 |
| status | `required` |
| production dependency | **yes** |

`2dfe2b32` is the bootstrap-era transition shim; keep it until every lane is
confirmed off the transitional shape.

## 5. Continuation (C0 / C2)

| field | value |
|---|---|
| commits | `0956fd2f` `eb56256c` `2c690895` `0c21f503` `68e2a4d0` `2180fd7b` `bbd6c836` |
| purpose | propagate `incomplete_details` and continue truncated output under a bounded budget |
| files | `core/continuation.py`, `core/session.py`, `core/model_turn.py`, `core/settings_registry.py`, `core/providers/_openai_responses.py`, `core/providers/_protocol.py` |
| upstream overlap | #1214 (future selection policy) — adjacent, not equivalent |
| status | `required` |
| production dependency | **yes** |

`0956fd2f` is the shared C0 reason-propagation primitive; the C2 commits depend on it.

## 6. Rerank instrumentation

| field | value |
|---|---|
| commits | `26bb5b32` `cefa7d5b` `2a1f04ac` |
| purpose | instrument dispatched rerank failures and read Switchyard's nested error code |
| files | `core/rerank.py`, `core/bm25.py`, `core/memory_relevance.py`, `core/session.py` |
| status | `required` |
| production dependency | yes — capability lanes report these counters |

## 7. Per-lane request headers

| field | value |
|---|---|
| commits | `4d115d38` |
| purpose | `server_compat.extra_headers` per-lane request headers |
| files | `core/model_turn.py`, `core/server_compat.py` |
| upstream overlap | #1201 |
| status | `required` |
| production dependency | **yes** — Switchyard lanes depend on lane-scoped headers |

## 8. Notify delivery diagnostics

| field | value |
|---|---|
| commits | `fa4f77f6` |
| purpose | load config JWT secret for notify; improve delivery diagnostics |
| files | `channels/_http.py`, `core/session.py`, `server.py` |
| upstream overlap | upstream PR #1137 — **the patch-id IS present upstream** |
| status | `retire candidate` (already upstream, listed here because it sits in our range) |

## 9. Scheduler migration lineage

| field | value |
|---|---|
| commits | `3984a549` |
| purpose | graft LocalClaw scheduler migrations onto the upstream storage chain |
| files | `core/storage/migrations/versions/*_lclaw*.py` and the merge migrations |
| status | `bootstrap` |
| production dependency | **yes** — these migrations have run in production |

Never drop these. They are applied to real databases.

## 10. Version bumps, changelogs, dependency and web-UI housekeeping

| commits | purpose | status |
|---|---|---|
| `eaacf3df` `3954328c` | version bumps to 1.8.4 / 1.8.5 | `superseded local` |
| `24265a6f` `e35edfab` | changelog preparation | `superseded local` |
| `1aeebaea` `7ea38517` | dependency floors (HTTPX2, Vitest, OpenAI SDK < 3.14) | `required` |
| `a0b70d4c` | merge upstream v1.8.5 into LocalClaw lineage | `bootstrap` |
| `ad62b777` `b8f1a999` | web-UI transcript measurement | `superseded local` — patch-ids present upstream |

---

## Retire candidates (patch-id already upstream)

These five commits are still in our range but their patches exist upstream. They
are the cheapest source of conflict on the next rebase and can be dropped:

| commit | subject |
|---|---|
| `b91477de` | fix: preserve reading position during conversation updates |
| `fa4f77f6` | fix(notify): load config JWT secret and improve delivery diagnostics (#1137) |
| `1aeebaea` | fix(deps): update HTTPX2 and Vitest for security advisories |
| `ad62b777` | test(harness): measure the perf page inside the shell layout chain |
| `b8f1a999` | perf(webui): stop re-measuring the transcript on every layout pass |

Dropping a *commit* is not the same as dropping its behaviour: re-verify each
against the merged upstream tree before removing anything.

## Upstream work we deliberately do NOT duplicate

| area | upstream | our posture |
|---|---|---|
| Switchyard provider boundary | #1201 | filed and tracked; resolved but not submitted upstream |
| reasoning replay | #1202 | reference only |
| provider descriptors | #1213 | reference only |
| multiple backends / selection policy | #1214 | reference only |
| PAIR / Switchyard backend | #1215 | reference only |
| the shadow sensor | none | **local experimental infrastructure; not proposed upstream** |

See [UPSTREAM-OVERLAP.md](UPSTREAM-OVERLAP.md).

## Uncommitted work owned by another workstream

Two files are modified in the shared checkout and are **not** part of this
branch's history:

```text
turnstone/core/history_decoration.py
turnstone/core/model_turn.py
```

They are reasoning-replay docstring/scope corrections owned by a different
workstream. They are identical in committed form on both lineages, so they do not
affect reproducibility. Do not commit them here. A byte-exact backup is kept at
`operations/turnstone-source-sync-20261001/foreign-work-preserved/`.

## Answering "which local commits do we still need?"

Groups 1–7 and 9 in the tables above are required by production today. Group 10
is mixed. The five retire candidates are not required. Answering this question
after an upstream update means re-running the patch-id derivation at the top of
this file and re-reading the `status` column.
