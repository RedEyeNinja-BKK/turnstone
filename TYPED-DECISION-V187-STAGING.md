# Turnstone 1.8.7 — typed-decision Output Guard candidate (2026-10-16)

**Status: STAGED / NOT DEPLOYED.** Branch is based on fork `production/current` solely because its `model_registry.py` and `output_guard_judge.py` blob hashes match upstream `v1.8.7`. **Do not install this entire fork branch as a replacement for upstream 1.8.7.** Overlay only the two changed Python modules onto an isolated copy of the installed upstream package.

## Files in scope
- `turnstone/core/typed_decision.py`: typed Switchyard `/v1/decisions` client, schema validation, `noul` risk normalization.
- `turnstone/core/output_guard_judge.py`: classify typed capabilities before generative-provider construction; dispatch typed requests; labeled failure => heuristic-only, never session-model fallthrough.
- `tests/test_output_guard_typed_decision.py`: recovered fork tests plus live-response regression for `backend`, `child_lane`, `model`, `requested`, and separate `risk_score` versus certainty.
- `mutation_battery_typed_decision.py`: 10 inherited mutation checks.
- `scripts/stage_typed_decision_v187.sh`: test-only overlay of installed `turnstone==1.8.7`, **no system service or database changes**.

## Staging on turnstone-vm (as vincent, no GitHub PAT required)

```bash
git clone --depth 1 --branch staging/typed-decision-v187-20261016 \
  https://github.com/RedEyeNinja-BKK/turnstone.git \
  "$HOME/typed-decision-v187-source"
bash "$HOME/typed-decision-v187-source/scripts/stage_typed_decision_v187.sh"
```

The runner checks the installed `1.8.7` package version, copies its package tree into `$HOME/typed-decision-v187-candidate`, overlays the two patched modules, then executes the standalone regression suite and mutation battery **against the copy**. If the candidate directory exists, it refuses to overwrite it. Logs: `regression.log` and `mutation.log` in candidate root.

## Live candidate-only backend verification (AFTER isolated unit/mutation green)

Run from `vincent`'s original source clone. This uses the candidate package
copy, not the running server, and makes **two synthetic decision calls** to
Switchyard through the real `OutputGuardJudge._evaluate_active` dispatcher.
A `model_turn` trap ensures no chat/Responses fallback is invoked.

```bash
git -C "$HOME/typed-decision-v187-source" pull --ff-only
PYTHONPATH="$HOME/typed-decision-v187-candidate" \
  /opt/turnstone-venv/bin/python \
  "$HOME/typed-decision-v187-source/scripts/live_verify_typed_decision_v187.py"
```

Expected final line: `LIVE CANDIDATE OUTPUT GUARD: PASS; services/config/DB unchanged`.
Risk level from the second synthetic error-containing output is **not asserted**,
since model classification is nondeterministic; both must succeed and retain the
actual backend and requested-route metadata.

## Results supplied by operator (2026-10-16)

- Installed upstream Turnstone: `1.8.7`, isolated candidate under
  `/home/vincent/typed-decision-v187-candidate`.
- Standalone regression suite: **31 passed / 0 failed**.
- Mutation battery: **10/10 KILLED**, 0 survived, 0 untested, 0 errors.
- Expected warnings in negative tests: simulated unresolved alias and HTTP 503.
- Live `OutputGuardJudge` dispatch with Switchyard: **pending**.

## Activation prerequisites (DO NOT change yet)

- Actual model alias and wire model id: `tier1-decision`.
- `judge.output_guard = true`, `judge.output_guard_llm = true`, `judge.output_guard_model = tier1-decision`. A Models → Roles assignment alone may not set those independently.
- Model capabilities merged with any existing values:
  ```json
  {"supports_typed_decision":true,"decision_contract":"switchyard-decision:v1","decision_types":["noul"],"decision_endpoint":"/v1/decisions"}
  ```
- Typed model base URL: `https://switchyard-vm.tailc8ef7b.ts.net` (origin, **no trailing /v1**); adapter appends `/v1/decisions`.
- Confirm model registry reload to each node. Keep the normal session and pre-tool IntentJudge models unchanged.

## Acceptance still pending
- Standalone 1.8.7 overlay test suite and 10/10 mutation kills.
- Full-turn live semantic Output Guard call and audit row with real `/v1/decisions` response; no generative provider attempt.
- A synthetic risk-positive tool result, semantic error/timeout preserving the heuristic floor, and multistep session regression.
- Candidate preflight and backup of deployed package/config before any controlled service swap. Retain a verified immediate rollback.
- Separately assess confidence semantics and provenance downstream in audit UI before calling it complete.

Switchyard known-good live response: `tier1-decision` returns `noul=0.028540645`, `backend=direct-openrouter-span-01-lite`, `child_lane=smartfree-decision`, `model=respan/span-01-lite-20260925`, `requested=tier1-decision`; 200 HTTP. The `noul` number is a **risk grade**, not statistical certainty. When the upstream verdict schema requires a numeric `confidence`, `0.0` remains the existing *unreported* sentinel, not a prediction of certainty.
