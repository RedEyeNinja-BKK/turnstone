# S6 production status

> A snapshot of the running system. It is **observed**, not proposed.
> Read it with [s6-index.md](s6-index.md).

## Snapshot

**Observed 2026-10-20T17:25Z.**

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
**2026-10-20T10:08:53Z** and again at **2026-10-20T13:31:47Z**, by
unrelated work on this host. Neither restart was performed by this
campaign.

Two independent counts agree on that, so the claim does not rest on a
single reading. systemd's restart counter for the unit reads
`NRestarts=2`, and the epoch's own telemetry carries node@3's cadence
rows under **three** distinct process identities — the original one plus
one per restart. The gaps in node@3's cadence rows bracket both restart
times.

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
- the sensor was **live** throughout, on **both** sides of both restarts.
  node@3's first cadence row under each new process is a real
  observation, not a heartbeat, so the epoch's own record shows the
  sensor observing work again immediately after each restart.

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
is still short — 66 of 100 attributable events.

## The gate fails closed without a reconciliation

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

## The count the corrected matcher would report

The deployed matcher credits a `delegation_result` — machine work — to an
operator turn. The corrected matcher removes that inflation, so from the
**same population** it reports one fewer attributable event.

**Measured 2026-10-20T17:25Z**, on the live epoch, same events and same
store rows, with only the matcher changed:

| matcher | B | B2 | **B + B2** | C | C2 | C3 |
|---|---|---|---|---|---|---|
| deployed (type-blind, row-reusing) | 65 | 2 | **67** | 13 | 4 | — |
| corrected (eligibility + exact matching) | 64 | 2 | **66** | 0 | 0 | 18 |

The deployed matcher is named for what it is: it applies **no event-type
eligibility rule** and it is an **independent nearest-neighbour match**, so
it can hand the same store row to two events. On this population it claims
**two** rows twice. The corrected matcher is both: it admits only
`operator_instruction` events, and it solves an exact minimum-cost,
maximum-cardinality assignment in which each row is consumed at most once —
**zero** rows are claimed twice.

The difference is exactly one event, and the direction is correct: the gate
must not be satisfied by counting machine work as operator work. The
eligibility rule removes the inflated event; the exact matcher does not
change the count on this population (it guarantees the count, it does not
move it).

The other column is the point of the correction. The deployed matcher
files the system's own transitions — a retry, a delegation result, a
task-agent failure — into **C (unverified)** and **C2**, because it
attributes them to operator rows and then finds the origin does not match.
The corrected matcher **excludes** them from the assignment and reports
them in their own bucket, **C3 = 18**. Nothing disappears: the 18 are still
counted, just counted as what they are. The gate reads 66 under the
corrected matcher and 67 under the deployed one, and the difference is
only visible in how the same events are accounted for.

## Deployment status

**NOT DEPLOYED.** The corrections on this branch are staged and qualified.
Nothing in this repository has been activated.

| | |
|---|---|
| production state | unchanged since the T2 boundary, except for unrelated service restarts (above) |
| successor slot | not installed |
| services restarted by this campaign | none |
| measurement epochs | `s6-t0`, `s6-t1`, `s6-t2` — all preserved, none merged |

## Preserved

Credentials, elevated permissions, the running services, every immutable
overlay, all historical telemetry, the watches, and the rollback anchors are
untouched. The one service restart recorded above changed no configuration
and no epoch marker.

## How to re-read this

```bash
sudo -u turnstone /opt/turnstone-venv/bin/python \
  tools/s6/liveness.py --epoch /var/lib/turnstone/sensor/s6-t2 --json
```
