# LocalClaw Turnstone divergence

This directory documents the LocalClaw customizations carried on top of upstream
Turnstone. It exists so that production state can be reconstructed and understood
**from our fork alone**, without relying on operator memory or on files that
exist only on this server.

It is not an upstream proposal and not a roadmap. It is a factual record of what
we carry, why, and what would retire it.

- Authoritative index: this file
- Per-commit manifest: [PATCHSET.md](PATCHSET.md)
- Sensor (local experimental infrastructure): [SENSOR.md](SENSOR.md)
- Upstream reference issues/PRs: [UPSTREAM-OVERLAP.md](UPSTREAM-OVERLAP.md)

---

## Purpose

Turnstone serves three agents (Turnstone, Hermes, OpenClaw) on `localclaw-vm`
behind Switchyard, which owns model routing. LocalClaw carries a set of
production patches — responses-wire reasoning repair, compaction contract
changes, a typed-decision Output Guard seam, and an experimental shadow sensor —
that upstream does not carry.

The goal is:

```text
production state
    ==
reproducible source in RedEyeNinja-BKK/turnstone
    + documented divergence from upstream
    + a clear alignment path back to mainline
```

## Upstream base

| item | value |
|---|---|
| upstream remote | `https://github.com/turnstonelabs/turnstone.git` |
| upstream branch | `dev` |
| upstream dev SHA (recorded 2026-10-01) | `e6679137a6404f563936de91de66c11c5563a927` |
| merge-base with our integration branch | `9bec9aadcee0185e064257c139a9b8a449315f01` |
| our commits ahead of upstream/dev | 41 (36 local-only, 5 already upstream) |
| upstream commits we do not carry | 91 |

Upstream `dev` moves daily. Re-derive these numbers with:

```bash
git fetch upstream dev
git rev-parse upstream/dev
git merge-base HEAD upstream/dev
git rev-list --count $(git merge-base HEAD upstream/dev)..HEAD
git rev-list --count $(git merge-base HEAD upstream/dev)..upstream/dev
```

**No rebase or merge of upstream into the integration branch is performed during
the sensor shadow epoch.** The numbers above are for visibility only.

## Local integration branch

| item | value |
|---|---|
| branch | `integration/localclaw-v1.8.5-sensor-20261001` |
| base | upstream `v1.8.5` merged into LocalClaw production lineage (`a0b70d4c`) |
| tip | `8b8561c1b7a8c809d3e03dc877d9cf38da72add1` |

This branch is the reproducible LocalClaw Turnstone state. It was created from
the **deployed runtime slot commit** rather than from a development branch, so the
branch tip and production were byte-identical at creation.

## Production runtime lineage

Production runs an immutable runtime slot, not the repository checkout:

| item | value |
|---|---|
| slot name | `1.8.5-8d6bd4a4-sensor-lane` |
| slot commit | `8d6bd4a4609b243cc221dc0d2d5435fa2489d7b2` |
| production anchor tag | `localclaw-prod-20261001-sensor-lane` |
| version | 1.8.5 |
| layout | `/opt/turnstone/runtimes/<slot>/venv` (a real, non-editable install) |
| per-node activation | `/home/vincent/.local/share/turnstone-slots/node<N>.slot`, applied by `/opt/turnstone/bin/tt-node-slot-run` |

`/proc/<pid>/cmdline` is the authority for which slot a process actually runs.
Unit text and the repository checkout are not deployment truth.

The slot commit was, until 2026-10-01, **reachable from no branch** — it existed
only as an object in the local object database. It is now the base of the
integration branch and is pushed to the fork.

### Two anchors, two different questions

| anchor | answers |
|---|---|
| branch `integration/localclaw-v1.8.5-sensor-20261001` | where development continues |
| tag `localclaw-prod-20261001-sensor-lane` | exactly what production was built from |

They are deliberately different commits. The branch tip carries the sensor
qualification suite and these documents, none of which are in the deployed tree;
the tag carries only the deployed source. Roll back to the tag, develop on the
branch.

## Reproducibility

Verified 2026-10-01 from a neutral clean checkout of the fork with an isolated
`HOME` and no reference to `/opt/turnstone`:

```bash
git clone --branch integration/localclaw-v1.8.5-sensor-20261001 \
    --single-branch git@github-turnstone:RedEyeNinja-BKK/turnstone.git
```

The committed tree is byte-identical to the deployed slot's `turnstone/` package,
with one addition: `turnstone/core/validation_corpus.py`, a test-only helper
imported by `tests/test_exact_tokens.py`.

Qualification from that clean checkout: **238 passed, 1 skipped**.

Two environmental prerequisites are **not** reproducible from the repository and
must be documented wherever the sensor is rebuilt:

1. the bounded-state tokenizer cache at `~/.cache/turnstone-sensor-tokenizer`
   (the sensor's exact-token admission needs a real tokenizer interpreter), and
2. the Switchyard AUX lane (below), which is configuration, not source.

## External dependencies

| dependency | why |
|---|---|
| Switchyard (`localclaw-switchyard.service`, `127.0.0.1:4000`) | owns all model routing; Turnstone names lanes, Switchyard resolves them |
| the AUX decision lane `switchyard-smart-aux-turnstone` | the sensor's single provider call target |
| the bounded-state tokenizer cache | exact-token admission; absent → the sensor fails open with a recorded reason |
| `lacme >= 1.2` in the test environment | the shared `/opt/turnstone/.venv` carries 1.0.5 and produces failures that look like regressions |

## Switchyard integration boundary

Turnstone does not route. It names a model alias; Switchyard resolves it to a
target and client. The sensor follows the same boundary and is deliberately
weaker than the guard:

```text
switchyard-smart-aux-turnstone    R1 Span / Respan  (OpenRouter)
                          --fallback-->  R2 Laya     (HTPC)
```

Both legs return a typed decision. The sensor calls this lane exactly once per
admitted event and reads the response; it never selects, retries or reorders.

## Judge / Output Guard relationship

The Output Guard and the sensor are **different consumers of the same lane**, and
must not be conflated:

| | Output Guard | Sensor |
|---|---|---|
| role | decides whether output is released | records an advisory observation |
| authority | **blocking** | **none** |
| path | typed-decision seam in `output_guard_judge.py` | `shadow_driver.ShadowSensor` |
| on failure | fails closed | fails open, records the reason |

Both resolve `judge.output_guard_model` / `DECISION_CAPABILITY` to
`switchyard-smart-aux-turnstone`. The sensor can never influence the guard, model
selection, tools, permissions or routing.

## AUX lane metadata

The three AUX model-definition rows carry documentation fields describing the
lane's provider legs. These were stale until 2026-10-01, when they were
converged on the operator ruling:

| alias | `decision_primary_model` | `decision_fallback_model` | `decision_role` |
|---|---|---|---|
| `switchyard-smart-aux-turnstone` | `respan/span-01-lite` | `laya-rl-agent` | `composite_aux_resilient` |
| `switchyard-smartfree-aux-turnstone` | `respan/span-01-lite` | *(none)* | `direct_span_only` |
| `switchyard-smartlocal-aux-turnstone` | `laya-rl-agent` | *(none)* | *(unset)* |

These four `decision_*` fields have **zero readers** in the Turnstone source.
`routes.toml` is the topology authority; `supports_typed_decision` is the only
load-bearing capability key on these rows. The metadata is a record for humans,
not a control input.

## Known non-upstream behavior

- `server_compat.extra_headers` per-lane request headers (upstream issue open).
- responses-wire reasoning replay and the non-empty in-round reasoning contract.
- compaction continuation behaviour (C0/C2), including lane-neutral scoping.
- rerank failure instrumentation reading nested Switchyard error codes.
- the typed-decision Output Guard seam and the shadow sensor.

## Tests and qualification evidence

| area | evidence |
|---|---|
| sensor | `tests/test_sensor_*.py`, `tests/test_shadow_observation.py`, `tests/test_bounded_state.py`, `tests/test_exact_tokens.py` — 238 passed, 1 skipped from a clean checkout |
| typed-decision guard | `tests/test_output_guard_typed_decision.py` |
| compaction / replay | the C0/C2 and responses round-repair test modules on the integration branch |
| shadow gate | post-T0 observation count and elapsed time, tracked outside the repository (see [SENSOR.md](SENSOR.md)) |

## Rollback expectations

The integration branch is a source-of-truth record, not a deployment mechanism.
Rolling back production means selecting a different runtime slot, not reverting
this branch. Every slot name embeds its commit, so the rollback target is always
nameable.

The previous accepted baseline before the sensor work was
`1.8.5-700423b4-sensor` lineage tip `700423b4`.

## Future upstream-alignment notes

Likely conflict hotspots when upstream `dev` is next taken:

- `turnstone/core/session.py` — four sensor lifecycle seams are wired into
  upstream-owned control flow.
- `turnstone/core/output_guard_judge.py` — the typed-decision seam.
- model registry accessor — the guard reads typed aliases through it.
- runtime-slot packaging — LocalClaw builds immutable slots outside upstream's
  installer.
- settings/config surfaces — the AUX lane names and the guard model setting.

Patches likely to become removable if upstream adopts equivalents are marked
`retire candidate` in [PATCHSET.md](PATCHSET.md).
