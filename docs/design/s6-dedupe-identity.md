# The S6 dedupe identity contract

> **Status: NOT DEPLOYED.** The contract below describes the staged
> correction. The deployed system still uses fingerprint-only dedupe, which
> is the defect this document specifies the fix for.

## 1. The invariant

```
ONE substantive logical operator turn
  -> ONE logical sensor admission.

Multiple hook firings for the SAME turn
  -> ONE admission.

Separate operator turns with identical content
  -> DISTINCT admissions.
```

The third clause is the one the deployed system violated.

## 2. The defect

The sensor's dedupe key was the **state fingerprint** alone. The fingerprint
hashes the **bounded projection**, and for a turn whose instruction lives in
an attachment, the only attachment-derived field in that projection is the
bounded structural descriptor (count / kinds / size bucket / two
verification counts).

So two *different* documents in the same size bucket produce a
byte-identical projection, an identical fingerprint, and the second turn is
suppressed as `unchanged_state`.

### 2.1 Observed evidence

Measured in epoch `s6-t2` on 2026-10-19. The identifiers below are
**synthetic placeholders** with the same shape as the measured values: the
real conversation ids, commit keys, send ids and attachment ids are lookup
keys into the live store and the live telemetry, so they are not published.
The *structure* is the evidence, and the structure is preserved exactly.

| | turn A | turn B |
|---|---|---|
| conversation id | `1001` | `1002` |
| `commit_key` | `aaa1…` | `bbb2…` |
| `client_send_id` | `ccc3…` | `ddd4…` |
| attachment | `eee5…`, 2 427 B | `fff6…`, 2 654 B |
| descriptor | `count=1 kinds=text=1 bytes=1-4KB inline_text_verified=1` | identical |
| fingerprint | `ffff…` | `ffff…` |

Different document, different submission, same structural descriptor — so
the fingerprint matched and the turn was dropped.

A second pair (the same shape, in a larger size bucket) collapsed the same
way. All four rows carried **distinct `client_send_id`s**, so they were four
different turns.

**Three distinct turns are currently suppressed** this way
(`F_distinct_turns_suppressed = 3`).

### 2.2 Why the fingerprint is not the defect

The fingerprint is **correct for its actual job** — it is the privacy
projection's identity, and it must be identical for two turns that present
the same material. The defect is using it as a **turn identity**. The
projection carries no content, name, id or mime, so it *cannot* distinguish
two documents by design. That is correct for egress and wrong for identity.

## 3. The fix

The dedupe key becomes `(fingerprint, turn identity)`.

The **turn identity** is the admitted conversation row's `commit_key` — a
per-turn idempotency token that **already exists** in the production path.
It is not synthesized, and it is not new state.

### 3.1 Where the identity may go

| location | identity allowed? |
|---|---|
| dedupe key | **yes** |
| the projection (semantic view) | **no** |
| the observation record | **no** |
| the cadence row | **no** |
| the model request | **no** |
| outbound judge request | **no** |

The identity is **hashed** (`sha256(commit_key)[:32]`) before it reaches the
dedupe state, so the raw token is never held, logged or transmitted.

### 3.2 The no-retry-storm contract is untouched

An event with **no** turn identity keeps plain fingerprint dedupe: its key
is `(fingerprint, "")`, which is exactly the historical behaviour. A
failure path that never admitted a turn therefore still suppresses a
repeated identical event, and the no-retry-storm contract still holds.

## 4. The proof

Red-first, against the real `ShadowSensor` and the real `ChatSession` — not
a helper function.

| file | tests | pre-fix |
|---|---|---|
| `tests/test_sensor_turn_identity_dedupe.py` | 14 | **10 failed**, 4 passed |
| `tests/test_sensor_turn_identity_session.py` | 11 | **9 failed**, 2 passed |

### 4.1 What the sensor-level tests pin

- two distinct turns with identical plain text stay **distinct**
- two distinct attachment-only turns with identical structural descriptors
  stay **distinct**
- a repeated submission of the same document is **not** automatically
  suppressed
- duplicate hooks for one logical turn **are** suppressed
- a turn identity that is present but whose state changed is admitted (a
  change is a change)
- **suppression is consecutive-only** — a repeated identity that returns
  after an intervening event is admitted again
- **the same `client_send_id` sent twice is two distinct turns** with two
  distinct identities, because the identity is minted per admitted turn
- the identity **never reaches the projection** — the key set is unchanged
  and no identity appears in the serialized state
- the identity **never reaches the wire** — no identity in the observation
  record or the cadence row
- with no identity, fingerprint dedupe is unchanged (no retry storm)

### 4.2 What the session-level tests pin

- every operator seam passes an identity: `send`, interjection, and queued
  flush, on a real `ChatSession`
- the identity **is** the stored row's `commit_key` — it joins to the
  durable row, not a synthesized token
- a turn that was never admitted has no identity and keeps fingerprint dedupe
- the identity is correct **across workstreams and retries**
- a **session restart cannot reuse the identity**: a new turn gets a new
  commit key

## 5. Known limitations

- **Dedupe state is per workstream.** The identity only separates turns
  *within* a workstream. Two identical turns in different workstreams were
  never at risk from this defect.
- **Suppression is CONSECUTIVE-ONLY.** The dedupe state holds the workstream's
  *last admitted key*, so a turn is suppressed only if it arrives with the
  same key as the immediately preceding admitted event. The same turn
  identity returning after an intervening event is admitted again:

  | sequence | admitted? |
  |---|---|
  | A, A | second **suppressed** (`unchanged_state`) |
  | A, B, A | the second A is **admitted** (`material_state_transition`) |

  This is the pre-existing contract — the state is a single slot, not a set —
  and the correction did not change it. It is a **known limitation** rather
  than a defect, but the reason it does not corrupt the gate's count is
  worth stating exactly, because the obvious argument for it is wrong.

  The gate counts *attributable events*, and the attribution is a one-to-one
  assignment, so **one store row can be credited to at most one event**. That
  alone does **not** bound a repeated identity to one attributable event: the
  identity's second admission is matched against the rows still unclaimed, so
  it can be credited to a *different* row — a different admitted turn — and
  the event count can then exceed the count of distinct turns.

  What does bound it is upstream, in the sensor: every operator seam observes
  the row it has *just* admitted, exactly once, and every admission mints a
  fresh `commit_key`. So one admitted row never reaches the seam twice, and
  the A-B-A re-admission is a re-admission of the same *projection* by a
  genuinely different turn, not a second observation of one turn. The
  provenance ledger measures this directly: `E_legitimate_duplicates_suppressed`
  counts re-fires of the *same* turn, and it is **0** on the live epoch.

  The one-row-one-event property is a property of the *rows*, and it is
  pinned as such — it is not a claim about turns. A future change to a
  set-valued dedupe state would be a separate, deliberate decision.
- **The identity is the commit key, not the send id — and the commit key is
  fresh per turn.** `commit_key` is a `uuid4()` generated at admission, and
  the API's own schema states that `client_send_id` "is not an idempotency
  key; repeated sends with the same value remain distinct turns". So a
  browser re-send of the same message produces a **new** commit key, and the
  re-sent turn is **admitted** rather than suppressed.

  What *is* suppressed is a re-fire of the *same* turn identity — the same
  admitted row re-observed, which the pinned test covers.
- **The three suppressed turns are not recovered.** The correction prevents
  future suppression; it does not recreate dropped observations.

## 6. The wiring pin

The historical pin asserted that every diff hunk mentioned a sensor marker
string. That was a *text* proxy for locality: it broke on legitimate
changes (the `turn_id` parameter, and the return-type change that carries
the admitted identity out of the append functions) while still being unable
to say what had changed.

It is replaced by a **structural** invariant: no control-flow *decision*
(`if` / `while` / `for` / `except` / `with` / `raise` / `assert` / `match`,
and the presence and kind of every `return`) may differ from the committed
file, in any lexical scope.

A mutation check proves the pin bites — 9/9 adversarial mutations are
caught: an added `if`, a removed guard, a new early return, a reordered
append/observe, a changed loop test, a duplicated decision, a swapped pair
of decisions, a changed `try/except` shape, and a changed exception type.
