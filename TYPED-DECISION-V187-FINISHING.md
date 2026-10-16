# Turnstone 1.8.7 typed-decision finishing candidate — 2026-10-16

Status: **STAGED ON GITHUB ONLY. NOT DEPLOYED.**

Base: `staging/typed-decision-v187-20261016`, whose first-pass overlay is already running on `turnstone-vm`. Do not modify or replace that running overlay while this candidate is under test.

## Exactly what changed

1. `turnstone/core/output_guard_judge.py`: remove false `typed_alias_unresolved` warning for intentionally non-generative typed backends; use a DEBUG message, retain INFO typed alias/result events, add actual Switchyard backend/child-lane/model/score fields to event.
2. `turnstone/core/tool_advisory.py`: render semantic-only non-none risk even with no flags, using **only framework-owned prose and structured metadata**, never free-form model reasoning. Label it explicitly as a Turnstone framework advisory rather than raw command stdout; preserve all existing heuristic flags and annotations.
3. `turnstone/core/typed_decision.py`: narrow the `noul` question to assistant-directed prompt-injection attempts, not any generic ERROR/WARNING/FAILED text. The `noul` score and existing risk thresholds remain unchanged.
4. Add regression tests to `tests/test_output_guard_typed_decision.py` and `tests/test_tool_advisory.py`.
5. Add isolated build/test script and 12-call comparative live calibration (six synthetic fixtures under old and new instructions).

## On `turnstone-vm` (as vincent)

```bash
git clone --depth 1 --branch fix/typed-decision-v187-finishing-20261016 \
  https://github.com/RedEyeNinja-BKK/turnstone.git \
  "$HOME/typed-decision-v187-finishing-source"

bash -n "$HOME/typed-decision-v187-finishing-source/scripts/stage_typed_decision_v187_finishing.sh"

bash "$HOME/typed-decision-v187-finishing-source/scripts/stage_typed_decision_v187_finishing.sh"
```

This builds `/home/vincent/typed-decision-v187-finish-candidate` from the installed **upstream 1.8.7** package and overlays only the three changed Python modules.

The staging runner runs a **34-test standalone regression suite**, 10-case mutation battery and, if `pytest` exists in the already installed virtualenv, the additional advisory/typed pytest files. It refuses to overwrite an existing candidate directory.

Only if these pass, compare old versus new questions on the real Switchyard endpoint:

```bash
set -o pipefail
/opt/turnstone-venv/bin/python \
  "$HOME/typed-decision-v187-finishing-source/scripts/compare_typed_decision_calibration_v187.py" \
  | tee "$HOME/typed-decision-v187-finish-candidate/calibration.log"
```

This performs 12 synthetic requests on `tier1-decision`; no server, DB, model definition or permissions change. It compares the old and proposed risk classification for clean output, harmless literal ERROR, actual command failure, quoted example documentation, forged role instructions and an exfiltration directive.

**Acceptance criteria:** 34/34 regression green; 10/10 mutant kills; no failing advisory pytest tests (if available); revised clean grade below 0.05; benign error/traceback/quoted example below 0.20; forged authority and exfiltration at or above 0.50. These calibration thresholds are **operator-chosen gates**, not a provider-guaranteed probability interpretation. If a fixture is REVIEW, do not deploy: inspect and refine the question.

## Deployment and rollback

**Do NOT use** `deploy_typed_decision_v187.sh activate` for this finishing candidate. That script belongs to the already deployed first-pass overlay and intentionally refuses overwrite.

Once the candidate and comparison pass, a separate second-pass deployment must install a fresh root-owned overlay under a new path, preserve the currently active `90-typed-decision-v187.conf` drop-in as the immediate rollback target, and use its own reversible service-specific override. The upstream virtualenv, existing workstreams, configured models, privileges and Switchyard routing must remain unchanged. Final acceptance requires one new Turnstone workstream call and journal evidence of successful typed-decision routing with the new log event and meaningful advisory text.
