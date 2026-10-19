# S6 event provenance — the classification ledger

> **Status: NOT DEPLOYED.** The ledger below is the staged correction. The
> deployed matcher is type-blind and may reuse a store row.

## 1. Why a raw row count is not an answer

`collect.py` answers *"how many observation rows exist?"*. `liveness.py`
answers *"did the sensor see the work?"*. Neither answers the question the
acceptance gate needs:

> **How many of those rows are independently attributable to genuine
> operator work?**

That gap produced three findings on 2026-10-18, all invisible to a raw row
count:

1. an observation whose workstream was **deleted** — the conversation rows
   were cascaded away, so the row cannot be matched to a stored message even
   though authoritative audit records prove an operator created the
   workstream and drove model calls in it;
2. a genuine operator message persisted as `role='system'`
   (`_source='user_interjection'`) — real work the store-side denominator
   did not count;
3. a distinct operator turn **discarded** by dedupe because its
   privacy-preserving structural descriptor matched an earlier turn's.

The module classifies, and never guesses.

## 2. The buckets

Every raw event lands in **exactly one** bucket.

| bucket | meaning | counts toward acceptance? |
|---|---|---|
| **A** | raw admitted **and** observed sensor events | the denominator |
| **B** | independently store-matched operator events | **yes** |
| **B2** | operator events attributable through independent audit/usage evidence (history erased) | **yes** |
| **C** | unresolved or unattributable events | **no** |
| **C3** | system transitions — retry / delegation / task-agent | **no** |
| **E** | legitimate duplicate hook firings | **no** |
| **F** | incorrectly suppressed distinct operator turns | **no** (reported) |

The gate keys on **B + B2**. `C` is reported beside it, never inside it.

**Observed (2026-10-19T20:44Z, epoch `s6-t2`):**

```
A raw admitted + observed    : 61
B verified operator (in store): 52
B2 attributed outside store  :  1   (history erased; audit+usage evidence)
C  unverified                :  0
C3 system transitions        :  8   (retry / delegation / task-agent)
E  duplicate firings         :  0
F  distinct turns suppressed  :  4

A (61) = B (52) + B2 (1) + C3 (8) + C (0)     ← disjoint and exhaustive
gate: B + B2 = 53 attributable / 100 required
```

## 3. The two matcher corrections

### 3.1 Type awareness

The deployed matcher matched **any** material event to **any** operator turn.
`retry_transition`, `delegation_result` and `task_agent_failure` are the
system's own transitions: real, observed, and **not** operator work.

They are now reported in their own bucket (`C3_system_transitions`) rather
than matched to an operator row or vanishing.

Eligibility is decided by the event's recorded **type**, not by its
materiality, so an event with no recorded type is still attributed exactly
as before.

### 3.2 One row, one event

The deployed assignment could match the **same store row** to more than one
event. Each row is now consumed at most once.

Ties are ordered deterministically by `(distance, operator-origin first, row
id)`, so the same population always produces the same assignment.

### 3.3 The effect on the count

| matcher | attributable |
|---|---|
| deployed (type-blind, row-reusing) | **44** |
| corrected (type-aware, one-to-one) | **43** |

**The count goes DOWN by one, and that is correct.** The deployed matcher was
crediting a `delegation_result` — machine work — to an operator turn, so it
was counting machine work as operator work. The correction removes that
inflation.

## 4. The independent cross-check

The 43 store-matched attributions were checked against an **independent
oracle** that recomputes each event's fingerprint from the store row
directly, rather than reusing the matcher's own comparison.

| matcher | agreements | disagreements | rows claimed twice |
|---|---|---|---|
| deployed | 41 | 2 | 2 |
| corrected | **42** | **0** | **0** |

The corrected matcher's attributions agree with the independent oracle on
every one of the frozen event set, and no row is claimed twice.

> The cross-check validates the *matcher*, against a frozen event set. The
> live count has moved on since (43 → the current 43 + new operator work)
> because the epoch keeps receiving genuine work. Those are different
> quantities: one measures correctness, the other feeds the gate.

## 5. Distinct-turn suppression is accounted, not invented

`F_distinct_turns_suppressed` counts turns that *should* have produced an
observation and did not, because the dedupe key could not tell them apart.
The count is derived from the same store rows the rest of the ledger uses.

**No historical sensor observations are invented to compensate.** The
suppressed turns are reported as a loss; they are not back-filled.

Historical epochs are **not** re-measured. The collision happened inside the
live epoch, and the correction changes what is *collected* from the moment it
is deployed. Pre-fix rows stay exactly as they are.

## 6. The `role` trap

`role` is not an origin signal, in either direction:

- a queued operator message is stored `role='system'` with
  `_source='user_interjection'` — real work;
- a machine nudge is stored `role='user'` with `_source='system_nudge'` — not
  operator work.

A `role='user'` filter therefore *excluded* the genuine interjection and
would have *included* any nudge. Both directions are decided by `_source`
(`distinguish_origin`), and an interjection's substance is judged on the
operator's own words with the machine-written preamble stripped.

## 7. The proof

`tests/test_provenance_one_to_one.py` (14 tests) and
`tools/s6/proofs/provenance_proof.py` (35 checks) and
`tools/s6/proofs/gate_provenance_proof.py` (20 checks).

The test file **fails against the pre-fix module** (14 failed) and passes
after (14 passed). It pins:

- one row, two events → the row is claimed once, and the second event
  matches its own row;
- a delegation result is **not** attributed to an operator turn;
- a retry transition is **not** attributed to an operator turn;
- a task-agent failure is **not** attributed to an operator turn;
- a system transition is reported in its own bucket;
- an event with no recorded type is still attributed;
- the tie break is deterministic.

## 8. Read-only in every direction

The module issues **SELECT-only** SQL. It performs no writes, calls no
service, and opens no network connection beyond the local database socket.
