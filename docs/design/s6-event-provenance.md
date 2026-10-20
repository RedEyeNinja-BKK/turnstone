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

### 3.2 One row, one event, and as many as the data allows

The deployed assignment could match the **same store row** to more than one
event. Each row is now consumed at most once.

That alone was not enough. The one-to-one rule was implemented as a **greedy**
assignment — each event took its best still-unclaimed candidate, in event
order — and a greedy assignment does not find a *complete* matching when
events compete for the same rows. It can take a row that a later event needed,
leaving that event unmatched while another row sat idle. That **under-reports**
attributable operator work, which is the same class of defect as the
double-claim: reporting less work than was done.

The counterexample (a 5-second window):

```
store rows at  -4 s  and  +1 s
events        at  +0.5 s and +5.5 s

event @ +0.5 s  -> candidates: row @ -4 s (d = 4.5), row @ +1 s (d = 0.5)
event @ +5.5 s  -> candidates: row @ +1 s (d = 4.5)
```

The greedy matcher gives row `+1 s` to the event at `+0.5 s` (it is the
closest) and leaves the event at `+5.5 s` unmatched: **one** match. But event
`+0.5 s` → row `-4 s` and event `+5.5 s` → row `+1 s` is a valid **two**-match
assignment.

The attribution is now an **exact minimum-cost, maximum-cardinality**
bipartite matching, solved by successive shortest paths over the residual
graph:

1. **MAXIMISE** the number of matched (event, row) pairs. A campaign is
   measured in operator turns, so a matching that credits more turns is
   better even if some pairings are less close in time.
2. Among maximum-cardinality matchings, **MINIMISE** the total attribution
   distance, so the pairing is as tight as the data allows.
3. Break remaining ties deterministically by
   `(distance, operator-origin first, row id, event id)`, decided exactly, so
   the result never depends on the order the events arrive in.

Distances are carried as integer **microseconds**, computed from the
timestamps' integer fields, so the cost of a matching never depends on
floating-point rounding or on the order the summands happen to be added in.

The problem sizes are small: on the live epoch the largest workstream had 20
operator events against at most 8 candidate rows per event, so the exact
solver is affordable.

`tools/s6/proofs/matching_cardinality_proof.py` pins the property, and its
last check cross-checks the solver against a **brute-force** optimum over 60
randomised instances. The proof is red-first: against the greedy matcher it
fails 5 checks, including 16 of the 60 brute-force comparisons.

### 3.3 The effect on the count

| matcher | attributable |
|---|---|
| deployed (type-blind, row-reusing) | **44** |
| corrected (type-aware, one-to-one) | **43** |
| corrected (type-aware, exact maximum-cardinality) | **43** |

**The count goes DOWN by one, and that is correct.** The deployed matcher was
crediting a `delegation_result` — machine work — to an operator turn, so it
was counting machine work as operator work. The correction removes that
inflation.

The exact matching does not change the live count: on the live epoch the
contested region is small enough that the greedy and exact assignments agree
on every event. It changes the *guarantee* — the count is now the maximum
the data supports, and it stays that way as the population grows, rather
than depending on the order the events happen to arrive in.

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
