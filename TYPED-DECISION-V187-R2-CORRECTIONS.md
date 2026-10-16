# Typed-decision 1.8.7 — revision 2 (r2) corrections and deployment plan

Status: **code + tests complete and green in the repository. NOT staged, NOT activated.**
The live overlay `/opt/turnstone-overlays/typed-decision-v187-finish` is untouched.

## 1. Why r2 exists

Two defects in the typed-decision lane. Both were **measured**, not inferred
(`feedback_measure_before_accepting_a_finding`). The earlier claim that
`tier1-decision` was "live-broken" was retracted: the decision lane works, and its
`base_url` (no `/v1`) / `model` / `context_window` / routing are **unchanged by this revision**.

### (a) Audit provenance on the typed-decision failure path

`OutputGuardJudge._error_verdict()` recorded `self._judge_model_alias or self._model`. On the typed
path provider construction is skipped by design, so `_judge_model_alias` is `""` and `_model` is the
**session** lane's raw model. Measured live row:

| field | value |
|---|---|
| `assessment_id` | `daab2985964148ff930abd46fcebc074` |
| `tier` | `llm_error` |
| `reasoning` | `typed_decision: decision transport failure: [SSL: RECORD_LAYER_FAILURE] …` |
| `judge_model` | `deepseek-flash` ← the session model, not the judge |

Impact was **audit provenance only**: the verdict was already `risk_level="none"` under
`tier="llm_error"`, no fallback-to-session-model judging occurred, and `history_decoration.py` drops
`llm_error` rows so it was never presented as clean.

### (b) Typed-decision rows advertised as generative choices

Measured on the live production registry (probe output):

```
list_aliases() -> 12 aliases
rows declaring supports_typed_decision -> ['tier1-decision']
TYPED ROWS ADVERTISED AS GENERATIVE CHOICES -> ['tier1-decision']
```

`tier1-decision` was offered in every session's task-agent "Available aliases" list and in the CLI
banner, yet cannot generate: `POST /responses` → 404 `endpoint_not_found`,
`POST /v1/responses` → 404 `model_not_found`. The row is what made it look usable — it carries
`server_compat.api_surface="responses"`, while `typed_decision.py`'s own docstring says such a row
"deliberately carries **no** `api_surface`". The guard itself was already defensively written and
remained safe; the exposure was purely in alias enumeration.

## 2. The change

| File | Change | Overlay treatment |
|---|---|---|
| `turnstone/core/model_registry.py` | new `list_generative_aliases()` — excludes rows whose capabilities declare `supports_typed_decision` | **copy** (repo base byte-identical to pristine 1.8.7) |
| `turnstone/core/output_guard_judge.py` | `_error_verdict()` prefers `_typed_alias` over the session model | **copy** (repo base byte-identical to the active overlay) |
| `turnstone/core/session.py` | 5 choice surfaces → `list_generative_aliases()` | **patch pristine 1.8.7** — repo base is 1.8.4; must not be copied |
| `turnstone/cli.py` | interactive banner "Models:" list → `list_generative_aliases()` | **copy** (repo base byte-identical to pristine 1.8.7) |
| `tests/test_generative_alias_exclusion.py` | new 12-case standalone suite | test artifact |

`list_aliases()` itself is **unchanged**: the typed row stays in the authoritative registry, stays
visible/manageable in the Models tab, and is still resolved and executed by the Output Guard through
`turnstone.core.typed_decision`. Only the *choice* surfaces were filtered.

**Judgment call (flagged for review):** `turnstone/cli.py:1524` is the interactive banner
`Models: …` — the CLI analogue of the session choice list. It is not one of the five `session.py`
sites named in the authorization, but it is the same defect class: a user reading it can select an
alias that cannot generate. Fixed for consistency; revert is a one-line change if the operator
prefers the literal scope.

### Non-regression evidence (repository tree)

| Suite | Result |
|---|---|
| `tests/test_generative_alias_exclusion.py` (new) | **12 passed, 0 failed, 12 total** — red→green proven (5 failures before the fix, each for the intended reason) |
| `tests/test_output_guard_typed_decision.py` | **34 passed, 0 failed, 34 total** (unchanged) |
| `tests/test_tool_advisory.py` + `tests/test_model_registry.py` | **272 passed** |
| `tests/test_output_guard*.py`, `*available_models*`, models/reload suites | **356 passed** |
| `tests/test_session*.py`, `tests/sessions*` | **742 passed** |
| `ruff check` on all changed files | clean (2 pre-existing `session.py` import-order findings, proven identical on the untouched baseline) |
| `mypy` | zero new errors: `session.py` and `cli.py` error sets proven **identical** baseline vs edited; new test file contributes 0 |

## 3. Deployment plan (staged, reviewed, reversible)

The existing second-pass stage script **correctly refuses** — its slot
(`/opt/turnstone-overlays/typed-decision-v187-finish`) is occupied and active. r2 therefore uses a
new slot and a new, later drop-in, leaving the first-pass and second-pass anchors intact.

**Slot / override / backup**

```
NEW     /opt/turnstone-overlays/typed-decision-v187-r2
DROPIN  /etc/systemd/system/turnstone-server.service.d/92-typed-decision-v187-r2.conf
        /etc/systemd/system/turnstone-node@.service.d/92-typed-decision-v187-r2.conf
BACKUP  /var/backups/turnstone/typed-decision-v187-r2-<date>   (root-only)
```

**Build (root, no service change)** — copy the **pristine 1.8.7** package, then:

1. copy `model_registry.py`, `output_guard_judge.py`, `tool_advisory.py`, `typed_decision.py`,
   `cli.py` from the repository;
2. patch the pristine `session.py` by replacing `self._registry.list_aliases()` with
   `self._registry.list_generative_aliases()`, **asserting exactly 5 replacements and 0 remaining** —
   the repo's 1.8.4 `session.py` is never copied (operator instruction);
3. `cmp` each copied file against the repository copy; import-check all six modules resolve inside
   the r2 slot as the `turnstone` service user;
4. snapshot `config.toml` and dump PostgreSQL to the root-only backup.

**Affected service units (measured, this is why a server-only switch would be wrong):**

| Unit | `PYTHONPATH` today | r2 action |
|---|---|---|
| `turnstone-server.service` | drop-ins `90-` + `91-` → `-finish` | add `92-` drop-in (later wins) |
| `turnstone-node@.service` lines 12 | set **in the unit template** → `-finish` | add `@.service.d/92-` drop-in overriding it |
| `turnstone-console.service` | **none** → pristine 1.8.7 | leave as-is (see caveat) |

> **Caveat to record:** the Console does not load the typed-decision overlay. It is therefore
> pristine 1.8.7 and does not carry this fix. Switching the server and nodes alone leaves that
> asymmetry; decide explicitly whether the Console should also take the r2 overlay.

**Activate (operator-gated)** — verify first-pass `-finish` still active and `/health` OK, require a
typed confirmation, write both drop-ins, `daemon-reload`, restart only the affected units, then
verify the running processes' `PYTHONPATH` really is r2 and `/health` is OK. On any failure:
remove the two drop-ins, `daemon-reload`, restart, confirm `-finish` restored.

**Rollback** — remove ONLY the two `92-` drop-ins and restart; the `90-`/`91-` anchors restore
`-finish`. No model setting, credential, DB row or admin right is touched by activation or rollback.

**Post-activation verification** — live probe of `execute_decision` against `tier1-decision`
(unchanged routing), plus the live alias-exposure probe showing `tier1-decision` absent from
generative choices while still present in `list_aliases()` and still resolvable by the guard.

## 4. Return route

The r2 workstream must not block the approved native OpenAI OAuth implementation. r2 is staged and
awaiting an operator activation decision; LC-RD-007 continues in parallel.
