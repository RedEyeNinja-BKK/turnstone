# S6 shadow-sensor measurement — architecture, lineage and acceptance

> **Status: NOT DEPLOYED.** The corrections on this branch are staged and
> qualified. The code currently running in production is the **deployed
> commit** named below, which predates them. Do not read this document as a
> description of the running system; read §2 for what is running.

This document is the engineering record for the S6 shadow-sensor measurement
campaign: what the sensor is, where it hooks, how its output is accounted
for, and how a reading can be checked against the evidence that produced it.

Three things are kept deliberately separate throughout, and every claim below
names which one it is:

| kind | meaning |
|---|---|
| **observed** | a measurement of the running system, with a timestamp |
| **historical** | what was true at an earlier point, kept for comparison |
| **proposed** | a change that is staged but not active |

---

## 1. What the sensor is

The shadow sensor observes what an operator actually did, and asks a local
judge whether that work was well-posed. It is **advisory**: it observes, it
never acts, and it never holds authority over a turn.

Three properties hold by construction, and each is pinned by a test:

- **Fail-open.** Every observation path is wrapped so that a sensor failure
  can never fail a task. A broken sensor degrades to silence, never to an
  error the user sees.
- **No control authority.** The sensor reads exactly one thing from the
  decision row, and its result is discarded at every call site.
  `test_no_sensor_result_is_read_or_assigned` pins that.
- **No control input.** The hook signature accepts no control fields, so
  the sensor cannot influence the turn it observes.
  `test_hook_signature_accepts_no_control_inputs` pins that.

### 1.1 The seams

The sensor observes at six call sites in `turnstone/core/session.py`:

| seam | what it observes |
|---|---|
| `operator_instruction` | a substantive operator turn — the main one |
| `delegation_result` | a task-agent's result returning |
| `retry_transition` | an attempt increment |
| `task_agent_failure` | a failed task-agent call |

Only `operator_instruction` counts toward acceptance. The other three are
**system transitions**: real, observed, and explicitly *not* operator work.
They are reported in their own bucket (`C3_system_transitions`) rather than
matched to an operator row or dropped. See
[the provenance contract](s6-event-provenance.md).

### 1.2 The privacy projection

The sensor never sees document content. For an attachment-delivered turn it
examines the attachment locally, tests the decoded words, and discards them.
What reaches the state is a **bounded structural descriptor**:

- the attachment count
- closed-class kinds (text / image / document / …)
- a coarse size bucket
- two verification counts

The projection carries **no** body, excerpt, filename, path, mime type, or
attachment id (a content hash). The projection key set and the rendered
string are both asserted exactly, so the descriptor cannot grow by accident.
The projection is the same view the grader received before this work, and
the turn identity added by the dedupe correction never enters it.

---

## 2. Production lineage and the deployed commit

### 2.1 The three identities

These are separate and must never be conflated:

| identity | value |
|---|---|
| **deployed commit** | `5427ded3aa7668975e2cef2621e81b9131498281` |
| **deployed overlay** | `/opt/turnstone-overlays/sensor-v187-5427ded` (444 files) |
| **staged candidate** | the tip of `fix/s6-sensor-dedupe-provenance-20261020` |

**Observed:** the deployed overlay's `turnstone/` tree is **byte-identical**
to the `turnstone/` tree of the deployed commit. The correspondence is
exact, not approximate:

```
$ diff -r <deployed-commit>/turnstone /opt/turnstone-overlays/sensor-v187-5427ded/turnstone
(no output)
```

The staged candidate differs from production in exactly three production
files: `turnstone/core/session.py`, `turnstone/core/shadow_driver.py`,
`turnstone/core/sensor_lifecycle.py`.

### 2.2 The epochs

| epoch | boundary marker | meaning |
|---|---|---|
| `s6-t0` | `T1` | first shadow activation; **preserved, incomplete** |
| `s6-t1` | `T1` | the `cb7fece` activation; **preserved** |
| `s6-t2` | `T2` | the `5427ded` activation; **the current epoch** |

The boundary marker is written **only after all six services verify**. Rows
written during the rolling upgrade are *rollout-validation* data and are
excluded from acceptance by timestamp — they are never merged in.

**Observed (2026-10-19T19:09Z):** the `s6-t2` boundary is
`2026-10-18T13:21:51Z`. The 48-hour floor is `2026-10-20T13:21:51Z`.

### 2.3 Historical epochs are not re-measured

The corrections on this branch change what is **collected from the moment
they are deployed**. They do not retroactively re-interpret earlier epochs.
`s6-t0` and `s6-t1` keep the counts they had. Where a correction changes a
number, this document says so explicitly rather than replacing the earlier
figure silently.

---

## 3. Acceptance: two dimensions, one gate

The campaign has an **operational gate** and a **quality measurement**. They
answer different questions and are reported separately.

### 3.1 Dimension A — event coverage (this is the gate)

Every admitted sensor event counts toward the 100-event gate, including
**structural-only** events where the operator's instruction lived in the
attachment. These are genuine operator-work events: the operator did the
work; the grader simply never saw the document.

### 3.2 Dimension B — qualified semantic grading (not a gate)

The denominator is the events whose content was actually available to the
grader: `content_basis` of `text` or `text_plus_attachments`.
`attachments_only` events are **excluded from B's denominator**, because a
structural descriptor cannot be graded semantically — the grader never
received the document.

The 100-event / 48-hour operational gate is Dimension A and is kept strictly
distinct from Dimension B.

### 3.3 The gate cannot fall back to raw rows

`liveness.py` publishes the collector's gate dict. If the collector is
handed no reconciliation, it falls back to the **raw** observation count.
That fallback is visible (`provenance_available: false`) and is never
presented as verified, but it would still be published as the gate the
scheduled watch reads.

`tools/s6/proofs/gate_provenance_proof.py` (20 checks) pins both halves of
this: the reconciled gate reads the attributable count, and the fallback
path is distinguishable from the reconciled one.

---

## 4. Reproducible qualification

Everything below runs without root, without a service, and without writing
outside `/var/tmp`.

```bash
# the measurement and driver proofs  -> 639 checks, 0 failed
bash tools/s6/proofs/run-all.sh

# the sensor suite
python -m pytest -q
```

The proofs locate the modules relative to themselves, so they run from a
checkout with no host paths.

---

## 5. Known limitations and open questions

- **The turn identity is per-workstream.** Dedupe state is keyed by
  workstream, so an identical projection in a *different* workstream was
  never at risk. The identity does not change that; it only separates
  distinct turns *within* a workstream.
- **The three suppressed turns are not recovered.** The correction prevents
  future suppression; it does not recreate the observations that were
  dropped. `F_distinct_turns_suppressed` keeps reporting the loss.
- **The corrected matcher lowers the count.** The deployed matcher credits
  a `delegation_result` to an operator turn, so correcting it *reduces* the
  attributable count by one. That is the correct direction: it stops
  counting machine work as operator work.
- **The hash chain covers the deployment's own files only.** It does not
  attest any external library, interpreter, or system package. See
  [the hash-chain note](s6-hash-chain.md) for exactly what is and is not
  covered.
- **`s6-t0` remains incomplete** and is never presented as acceptance data.

---

## 6. Related documents

| document | subject |
|---|---|
| [s6-dedupe-identity.md](s6-dedupe-identity.md) | the dedupe identity contract |
| [s6-event-provenance.md](s6-event-provenance.md) | the classification ledger |
| [s6-hash-chain.md](s6-hash-chain.md) | the transitive trust chain |
| [s6-production-operations.md](s6-production-operations.md) | upgrade, rollback, runbook |
| `tools/s6/ROOT-HANDOFF-5427ded.md` | the operator handoff for the S6-t2 rollover |
