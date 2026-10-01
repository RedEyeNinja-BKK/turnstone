# The shadow sensor — LocalClaw experimental infrastructure

> **STATUS: LOCAL / EXPERIMENTAL · SHADOW ONLY · NO ROUTING AUTHORITY · NOT AN
> UPSTREAM TURNSTONE FEATURE**
>
> This is not a Turnstone feature, is not proposed upstream, and carries no
> authority over anything. It exists to produce measured evidence about bounded
> engineering work, and it is still collecting that evidence.

- source: `turnstone/core/{bounded_state,exact_tokens,sensor_layer,sensor_lifecycle,sensor_questions,shadow_driver,shadow_observation}.py`
- wired seams: `turnstone/core/session.py`
- commits: `578ccb7d`, `8d6bd4a4`, tests `8b8561c1` (see [PATCHSET.md](PATCHSET.md))
- production slot: `1.8.5-8d6bd4a4-sensor-lane`

---

## Why it exists

Turnstone routes model selection to Switchyard, and Switchyard routes on
declared capability and policy. Neither can observe whether a work is actually
going well. The question the sensor answers is narrow and bounded:

> given the shape of an in-flight piece of engineering work, what does a typed
> decision backend say about its complexity, reasoning demand, tool dependency,
> agentic complexity, local suitability and route sufficiency?

The answer is recorded and read by a human. It never feeds back into routing.

## Observer, not judge

This distinction is the whole safety argument:

| | sensor | Output Guard |
|---|---|---|
| consumes a typed decision | yes | yes |
| blocks anything | **never** | yes, on release |
| selects a model/client/route | **never** | no |
| failure behaviour | fails **open** — records the reason | fails **closed** |
| can influence tools, permissions, escalation | **never** | no |

The sensor holds no handle on any of those surfaces. `_assert_advisory()`
refuses to serialise a snapshot containing any of `model`, `provider`, `route`,
`target`, `llm_client`, `reasoning_effort`, `reasoning`, `temperature`, `agent`,
`tools`, `permissions`, `escalate`, `retry`, `output_guard` — it raises rather
than emit. This is a hard structural check, not a convention.

## The provider-neutral sensor contract

The sensor asks a fixed set of questions and grades the answers. It does not
know or care which backend answered.

Six signals, all confirmed live on the R1 leg:

```text
task_complexity      reasoning_demand     tool_dependency
agentic_complexity   local_suitability    route_sufficiency
```

Each observation records per-signal `value`, `backend`, `primitive`
(`graded`/`boolean`/`enumerated`), `observability`, and an optional `reason`. A
signal the backend could not answer is recorded as **unobservable**, never as a
zero — a missing answer and a zero answer are different claims.

## Provider coverage

One Switchyard lane serves both legs:

```text
switchyard-smart-aux-turnstone    R1  Span / Respan  (OpenRouter)
                             └─R2  Laya            (HTPC)
```

The observation records which provider actually answered (`backend`), so a
fallback leg is visible in the telemetry rather than inferred. To date all
observations have been served by Respan; the Laya leg has not been needed.

The sensor calls this lane **once** per admitted event. No retries, no queue, no
background thread, no consumer.

## Bounded-state serializer and exact-token admission

State handed to the sensor is reduced deterministically before it is sent, under
a hard budget:

```python
STATE_TOKEN_BUDGET = 256      # tokens, exactly
STATE_CHAR_BUDGET  = 921      # derived: 256 * chars-per-token
LOW_TIER_RESERVE   = 128      # lowest-priority tier protected first
```

Overflow is reported honestly as one of two distinct causes, never conflated:

- `evicted_by_tier` — a higher tier was present, or the low-tier reserve had to
  be protected. Policy working as designed.
- `overflowed_budget` — the exact token budget was exhausted.

Exactness requires a real tokenizer. The interpreter is resolved from
`~/.cache/turnstone-sensor-tokenizer`; when it is absent the sensor does not
guess a ratio — it fails open with a recorded reason. That is why the cache is a
documented external dependency rather than a build artifact.

## Cadence and dedupe semantics

An observation is taken when the event is material **and** the state fingerprint
changed. Both gates are whitelists, not heuristics:

```text
not in MATERIAL_EVENTS                 -> rejected_not_material   (no call)
fingerprint == last admitted           -> dedupe_suppressed      (no call)
first substantive state                -> initial_substantive_state
material event + changed fingerprint   -> material_state_transition
```

Two properties worth preserving:

1. **The caller owns exactly one cadence decision.** `SensorHook.observe_event`
   calls `should_sense()` — which *commits* the fingerprint — and then calls
   `observe(..., decided=True)`. Re-gating inside `observe()` denied the very
   event the caller had just admitted, producing a `sensor_call` cadence row
   with an empty observation store. That was a real production defect
   (2026-09-30 canary); `decided=True` is the fix and it is covered by tests that
   drive the real hook path, not `observe()` directly.

2. **Suppression depends only on the caller's state, never on whether a network
   call succeeded.** The fingerprint is committed on the admit branch. An earlier
   version advanced it only after success, so with the backend down every event
   read as `initial_substantive_state` and the sensor fired on everything.

Telemetry lives outside the repository, under
`operations/switchyard-sensor-layer-20260929/`:

| file | contents |
|---|---|
| `shadow-observations.jsonl` | one `SensorSnapshot.to_dict()` per admitted observation |
| `shadow-cadence.jsonl` | one row per cadence decision: event, outcome, workstream, fingerprint |
| `EPOCH-T0.txt` | the measurement epoch anchor |

## Wired lifecycle seams

Four of the eight material events are wired into `session.py`:

| event | call site | state projected |
|---|---|---|
| `operator_instruction` | `session.py:12839` | objective, phase |
| `retry_transition` | `session.py:15410` | objective, phase, blockers |
| `delegation_result` | `session.py:26968` | objective, phase, history |
| `task_agent_failure` | `session.py:27058` | objective, phase, blockers |

## Intentionally unwired seams

Four material events are declared material but have **no call site**:

```text
escalation_transition   new_evidence   phase_transition   tool_failure
```

This is deliberate, not an oversight: each is a candidate seam whose state
projection has not been agreed, and wiring an unagreed projection would put
unbounded content in front of the 256-token budget. They stay unwired until
their projection is defined. Note this is four, not two.

## Shadow gate

The epoch was reset on 2026-10-01 when the transport lane identity changed, the
runtime slot changed and Switchyard's config changed. Records from the previous
lane describe a different transport and are not comparable, so they are excluded
rather than mixed.

```text
T0  = 2026-10-01T00:59:13+07:00   (epoch 1790791153)
gate = >=100 genuine post-T0 observations  AND  >=48h elapsed
     = >=100 observations                  AND >= 2026-10-03T00:59:13+07:00
```

No synthetic traffic fills the counter. Acceptance for each admitted event is:
one `sensor_call` cadence row, exactly one stored observation, a real trigger, a
real backend, a serialized state within 256 tokens, and zero control fields. A
cadence row with no stored observation is the defect signal.

## Reproducibility requirements learned in deployment

These are the operational constraints, recorded because each one silently breaks
reconstruction if forgotten:

1. **Immutable runtime slot.** Production runs a slot, not the repository
   checkout. The slot name embeds its commit.
2. **Repo-vs-slot import trap.** The slot is a real (non-editable) install into
   its own venv. Editing the checkout does not change what runs, and importing
   from the checkout does not prove what the slot contains. Compare the slot's
   `site-packages/turnstone/` against a checkout directly.
3. **Per-node slot activation.** Each node reads its own slot file; a fleet-wide
   claim requires all five verified, not one.
4. **A slot commit in no branch is unrecoverable.** The deployed commit
   `8d6bd4a4` existed only as a loose object until 2026-10-01. It is now the base
   of `integration/localclaw-v1.8.5-sensor-20261001`.

## Known limitations

Stated plainly so nobody reads more into the telemetry than it supports:

- **No workstream identity on observations.** Cadence rows carry the workstream
  id; `SensorSnapshot` has no such field, so observation rows read
  `workstream: null`. Per-workstream read-out must join cadence to observations
  on `timestamp` + `state_fingerprint`. Closing this is a runtime-slot code
  change, deliberately not done during the epoch.
- **A zero observation count is a property of the sample, not a mechanism.** An
  earlier claim that dedupe suppression had "never fired" was falsified 53
  minutes after it was written. Never write a universal negative into durable
  documentation from a small sample.
- **Exactness is tokenizer-dependent.** No tokenizer, no exact count — the sensor
  reports the reason instead of estimating.
- **Six signals only.** Anything outside them is not measured, and unmeasured is
  not zero.
- **Observability is `unknown` in practice.** The enum has one member; the
  backend does not currently commit to a confidence level, and the sensor does
  not invent one.

## Rollback

Select a different runtime slot. Nothing about the sensor is baked into the
database, and no stored observation is consumed by any production path.
