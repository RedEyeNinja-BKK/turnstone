# S6 production status

> A snapshot of the running system. It is **observed**, not proposed.
> Read it with [s6-index.md](s6-index.md).

## Snapshot

**Observed 2026-10-20T17:25Z** (the published snapshot). A later
read-only measurement at **`2026-10-20T19:26:03Z`** is reported in
[the gate](#the-gate) with its own timestamp; the two are not merged.

| item | value |
|---|---|
| Turnstone | 1.8.7 |
| services | 6/6 active — `turnstone-console`, `turnstone-node@2` … `@5` |
| overlay | `/opt/turnstone-overlays/sensor-v187-5427ded` (444 files) |
| deployed commit | `5427ded3aa7668975e2cef2621e81b9131498281` |
| epoch | `s6-t2` |
| boundary | `T2` = `2026-10-18T13:21:51Z` |
| elapsed | 52.07 h of the 48 h floor |
| next deadline | **passed** — the 48 h floor was met at `2026-10-20T13:21:51Z` |

### One service was restarted twice, outside this campaign

`turnstone-node@3` is the only service whose start time is not its `T2`
one. It has been restarted **twice** since the boundary, at
**2026-10-20T10:08:53Z** and again at **2026-10-20T13:31:47Z**. Neither
restart was performed by this campaign.

Two independent counts agree on that, so the claim does not rest on a
single reading. systemd's restart counter for the unit reads
`NRestarts=2`, and the epoch's own telemetry carries node@3's cadence
rows under **three** distinct process identities — the original one plus
one per restart. The gaps in node@3's cadence rows bracket both restart
times.

**What the evidence establishes, and what it does not:**

| claim | status |
|---|---|
| node@3 restarted twice after the boundary | **observed** |
| the cause of each restart | **unverified** |
| who or what initiated each restart | **unverified** |
| the sensor observed work before and after both restarts | **observed** |
| the sensor operated without interruption across them | **not proven** |

The cause and the initiator of each restart are **not** established by the
evidence that survives. The unit's journal is readable only with
privileges this account does not have, and the kernel log is likewise
unreadable, so the only surviving indications are the restart counter, the
service start timestamps, and the epoch's own telemetry. Those establish
**that** the restarts happened and **when**; they do not establish **why**.

The last row of that table is the one that matters for the measurement,
and it is a limitation rather than a reassurance. node@3's first cadence
row under each new process is a real observation, so the sensor was
observing work again immediately after each restart. But the epoch records
only what the sensor *wrote*; a turn that arrived in a restart window and
was never observed would leave a silence in the telemetry that is
indistinguishable from a turn that never arrived. No such gap is claimed
here, and none is ruled out by the telemetry alone.

This does not disturb the measurement, and the reason is worth stating
plainly rather than leaving to inference:

- the restarts changed **no** measurement configuration. All five services
  still resolve `PYTHONPATH=/opt/turnstone-overlays/sensor-v187-5427ded`,
  and node@3's `95-sensor.conf` drop-in — its store directory, its
  content mode, its timeouts — is unchanged. The **running** process
  confirms it: `/proc/<pid>/environ` carries the sensor overlay on
  `PYTHONPATH` and all seven `TURNSTONE_SENSOR_*` variables, and the
  store directory is still node@3's own;
- the restarts did **not** move the boundary. `T2` is still
  `2026-10-18T13:21:51Z`, written once by the activation script;
- the sensor was **live** on **both** sides of both restarts. node@3's
  first cadence row under each new process is a real observation, not a
  heartbeat, so the epoch's own record shows the sensor observing work
  again immediately after each restart.

What a restart does cost is *continuity of a single process*, not
continuity of the epoch. The epoch is a directory of observations, not a
process's memory, so the measurement is unaffected. The one thing a
restart does reset is the **dedupe state** and the in-process counters,
which are re-derived on the next event and are not part of the
acceptance arithmetic.

## The gate

| | |
|---|---|
| attributable events (B + B2) | **66** / 100 |
| raw observation rows | 84 |
| unverified (C) | 0 |
| provenance available | true |
| elapsed | 52.07 h / 48 h |
| **gate** | **PENDING** on the count; the 48 h floor is **MET** |

Integrity checks are all met: zero unmatched, zero duplicate event ids,
zero orphans, boundary present.

**The count is live and it moves.** The epoch is receiving genuine operator
work, so the number rises on its own. The figure at any moment is a
measurement of that moment, not a fixed property of the epoch.

**The time floor is now met.** The 48 h floor was reached at
`2026-10-20T13:21:51Z`. The count is the only remaining condition, and it
is still short — 66 of 100 attributable events at the documented
`2026-10-20T17:25Z` snapshot.

The count keeps moving, so the figure is reported with the time it was
measured. A later read-only measurement at **`2026-10-20T19:26:03Z`**
gives **69 of 100** attributable under the current corrected matcher
(`B = 67`, `B2 = 2`), on a raw population of 88 observations and an
elapsed 54.07 h. Under the legacy type-blind matcher, on the *same*
snapshot, that figure is **70** (`B = 68`, `B2 = 2`) — the same
one-event difference, at a larger population. The gate is still short of
100 and the time floor remains met.

### Restart-adjacent gaps, and the one unverified event

Two questions were checked against the read-only evidence, because a
restart is exactly where work can be lost silently.

**Did a restart-adjacent gap lose eligible work?** The epoch's integrity
checks are clean: zero unmatched admitted rows, zero duplicate event ids,
zero orphan observations, and the boundary marker is present. The
matcher's own ledger shows **zero** duplicate event ids and **zero**
suppressed rows that failed to resolve to a store turn beyond the four
distinct-turn suppressions already reported.

**One admitted event is unverified.** It is not restart-related: it was
observed at `2026-10-20T18:17:25Z` by the process that was already
running at that time, and it arrived on the **interjection** seam — an
operator message that reached the model mid-turn. The seam observes the
turn when the queued message is drained, but the conversation row's
`timestamp` is stamped when the row is *persisted*, and that persistence
is deferred to the generation batch. The store row for that turn was
written **52.5 s** after the observation, which is outside the matcher's
5 s window, so the event could not be paired with its row.

Across the whole epoch this is the **only** such case. The measured
distribution of (row timestamp − event timestamp) over the **67**
admitted operator events that carry both timestamps is: minimum −1.00 s,
25th percentile −0.79 s, median −0.41 s, 75th percentile −0.12 s,
maximum +1.39 s, and **zero** events beyond the 5 s window. No row was
written before its event by more than a second, so the deferral is not
systematically shifting the population; this is an isolated instance of
the seam observing before the row lands.

The consequence is bounded and in the conservative direction: an event
that cannot be paired with a row is **not** counted as attributable. The
gate therefore under-counts by at most one event rather than
over-counting. The figure is reported rather than smoothed away, because
the alternative — widening the match window until it matches — would
trade a measured, bounded gap for an unmeasured risk of pairing an event
with the wrong row.

The raw count and the attributable count are **different numbers**, and only
the second one can certify the campaign. `66` attributable events and `84`
raw rows are not interchangeable: the difference is the events that could not
be tied to a stored operator turn.

The corrected collector makes that distinction unarguable. **Without** a
provenance reconciliation:

- the gate count is **zero**, and `met` is **false** — the gate cannot be
  satisfied by raw rows;
- the raw count is still published, explicitly labelled informational;
- **no** attributable rate and **no** ETA are published, because a forecast
  built on events nobody has attributed to an operator turn is a forecast of
  unverified work, and publishing it next to a real forecast invites the
  reader to treat the two alike.

`tools/s6/proofs/provenance_required_proof.py` pins this, with negative
controls in `staleness_control.py` that re-introduce the old fallback and
require the proof to reject it.

## Distinct-turn suppression

**Observed: 4 distinct operator turns are currently suppressed** by the
deployed fingerprint-only dedupe key (`F_distinct_turns_suppressed = 4`).
The same-turn re-fire count is **0** (`E_legitimate_duplicates_suppressed
= 0`), so every suppression in the epoch is a **distinct** turn that was
dropped, not a duplicate firing that was correctly collapsed.

This is the defect the staged correction addresses. It is reported here
rather than hidden, and the correction **does not recover** the lost
observations — it prevents future suppression. See
[s6-dedupe-identity.md](s6-dedupe-identity.md).

## The count under each matcher

Two different things are being compared here, and they are not the same
kind of thing.

- The **legacy type-blind matcher** is the attribution code that ships in
  the measurement artifact and reports the live epoch. It is what produced
  the historical numbers.
- The **current corrected read-only matcher** is the attribution code on
  this branch. It reads the same epoch and the same store; it changes
  nothing on disk and it deploys nothing.
- The **deployed sensor runtime** is the code in
  `sensor-v187-5427ded`. It is unchanged and it is what is running.
- The **staged successor sensor code** is the dedupe-identity fix on this
  branch. It is not deployed, and correcting the *reporting* code does not
  deploy it.

The legacy type-blind matcher credits a `delegation_result` — machine
work — to an operator turn. The current corrected read-only matcher removes
that inflation, so from the **same population** it reports one fewer
attributable event.

**Measured 2026-10-20T17:25Z**, on the live epoch, same events and same
store rows, with only the matcher changed:

| matcher | B | B2 | **B + B2** | C | C2 | C3 |
|---|---|---|---|---|---|---|
| legacy type-blind matcher (row-reusing) | 65 | 2 | **67** | 13 | 4 | — |
| current corrected read-only matcher | 64 | 2 | **66** | 0 | 0 | 18 |

The legacy type-blind matcher is named for what it is: it applies **no
event-type eligibility rule** and it is an **independent nearest-neighbour
match**, so it can hand the same store row to two events. On this
population it claims **two** rows twice. The current corrected read-only
matcher is both: it admits only `operator_instruction` events, and it
solves an exact minimum-cost, maximum-cardinality assignment in which each
row is consumed at most once — **zero** rows are claimed twice.

The difference is exactly one event, and the direction is correct: the gate
must not be satisfied by counting machine work as operator work. The
eligibility rule removes the inflated event; the exact matcher does not
change the count on this population (it guarantees the count, it does not
move it).

The other column is the point of the correction. The legacy type-blind
matcher files the system's own transitions — a retry, a delegation result, a
task-agent failure — into **C (unverified)** and **C2**, because it
attributes them to operator rows and then finds the origin does not match.
The current corrected read-only matcher **excludes** them from the
assignment and reports them in their own bucket, **C3 = 18**. Nothing
disappears: the 18 are still counted, just counted as what they are. The
gate reads 66 under the corrected matcher and 67 under the legacy one, and
the difference is only visible in how the same events are accounted for.

**This comparison does not deploy anything.** It is a read-only
re-attribution of one already-collected population. The deployed sensor
runtime keeps fingerprint-only dedupe, and the four distinct-turn
suppressions reported above continue to happen until the staged successor
sensor code is deployed under separate authorization.

## Deployment status

**NOT DEPLOYED.** The corrections on this branch are staged and qualified.
Nothing in this repository has been activated.

| | |
|---|---|
| production state | unchanged since the T2 boundary, except for two service restarts (above) |
| successor slot | not installed |
| services restarted by this campaign | none |
| measurement epochs | `s6-t0`, `s6-t1`, `s6-t2` — all preserved, none merged |

## Preserved

Credentials, elevated permissions, the running services, every immutable
overlay, all historical telemetry, the watches, and the rollback anchors are
untouched. The one service restart recorded above changed no configuration
and no epoch marker.

## Historical privacy exposure (remediated in this branch's history)

**The current tip of this branch is clean, and that is not the same as the
repository being clean.** Earlier published commits on this branch carried
real operator content. This branch's history has since been **rewritten** to
remove them: the twelve commits above the base were rebuilt with the
sensitive values replaced by synthetic placeholders of the same shape, and
the branch was force-pushed with an expected-old-SHA lease.

| what | where | status |
|---|---|---|
| a real operator interjection published beside its unsalted digest | `tools/s6/provenance.py` and `tests/test_provenance_one_to_one.py` | **remediated: replaced in this branch's history** |
| live conversation-row ids in a commit message | the first commit of this branch | **remediated: replaced in this branch's history** |
| live conversation-row ids and live commit/send/attachment ids in a design document | `docs/design/s6-dedupe-identity.md` | **remediated: replaced in this branch's history** |
| a live workstream id in a test fixture's prose | `tests/test_sensor_interjection_seam.py` | **remediated: replaced in this branch's history** |
| a real operator message used as a proof fixture | `tools/s6/proofs/liveness_proof.py` | **remediated: replaced in this branch's history** |

What was replaced, and why it matters:

- The first pair published a **real operator message** next to the
  unsalted SHA-256 of its projection. That digest is a **confirmation
  oracle**: a reader who guesses the message can hash it and compare, and a
  reader who holds the digest can locate the matching row in the live
  telemetry. The replacements are **synthetic** messages whose digests were
  **recomputed** from the synthetic text, so the pair proves nothing about
  any real message. The property the test needs — two distinct digests over
  two distinct messages — is preserved exactly.
- The live **conversation-row ids** were replaced with **synthetic
  placeholders** with the same shape, and the surrounding prose says so at
  the point of use.
- The live **commit keys, client send ids and attachment ids** in the
  dedupe-identity document are lookup keys into the live store and the live
  telemetry. They were replaced with synthetic placeholders of the same
  shape; the measured sizes, the descriptor and the identical fingerprints
  — the actual evidence — are unchanged.
- The proof fixture in `tools/s6/proofs/liveness_proof.py` used a **real
  operator message** as its "plain message" sample. It is now a synthetic
  fixture.

**What the rewrite does not reach.** A history rewrite changes the branch,
not the platform. The following remain outside the branch's control and
remain the operator's to decide:

- objects that were already fetched by a reader, a fork, or a mirror;
- the pull request's own **timeline** — the commits and comments that
  referenced the pre-rewrite history;
- the remote's **caches** for the old commit and blob endpoints;
- the **base branch**, which predates this branch and carries one of the
  same identifiers in a file this branch does not change.

So the honest statement is: **the branch history is clean, and the
repository is not yet proven clean.** Closing the remainder needs a
sensitive-data removal request to the platform, and it needs a decision
about the pull request's timeline.

## How to re-read this

```bash
sudo -u turnstone /opt/turnstone-venv/bin/python \
  tools/s6/liveness.py --epoch /var/lib/turnstone/sensor/s6-t2 --json
```
