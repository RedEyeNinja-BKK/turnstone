# S6 production status

> A snapshot of the running system. It is **observed**, not proposed.
> Read it with [s6-index.md](s6-index.md).

## Snapshot

**Observed 2026-10-20T11:10Z.**

| item | value |
|---|---|
| Turnstone | 1.8.7 |
| services | 6/6 active — `turnstone-console`, `turnstone-node@2` … `@5` |
| overlay | `/opt/turnstone-overlays/sensor-v187-5427ded` (444 files) |
| deployed commit | `5427ded3aa7668975e2cef2621e81b9131498281` |
| epoch | `s6-t2` |
| boundary | `T2` = `2026-10-18T13:21:51Z` |
| elapsed | 45.81 h of the 48 h floor |
| next deadline | `2026-10-20T13:21:51Z` (the 48 h floor) |

### One service was restarted, outside this campaign

`turnstone-node@3` was restarted at **2026-10-20T10:08:53Z** by unrelated
work on this host. It is the only service whose start time is not its
`T2` one.

This does not disturb the measurement, and the reason is worth stating
plainly rather than leaving to inference:

- the restart changed **no** measurement configuration. All five services
  still resolve `PYTHONPATH=/opt/turnstone-overlays/sensor-v187-5427ded`,
  and node@3's `95-sensor.conf` drop-in — its store directory, its
  content mode, its timeouts — is unchanged. The **running** process
  confirms it: `/proc/<pid>/environ` carries the sensor overlay on
  `PYTHONPATH` and all seven `TURNSTONE_SENSOR_*` variables, and the
  store directory is still node@3's own;
- the restart did **not** move the boundary. `T2` is still
  `2026-10-18T13:21:51Z`, written once by the activation script;
- the sensor was **live** throughout. node@3 wrote no cadence row between
  the restart and this snapshot, and the reason is that **no substantive
  operator turn reached it**: node@3's own workstreams received exactly
  **two** operator turns in that window, and both carried the literal text
  `resume`, which `is_substantive_instruction` rejects — it is in the
  acknowledgement whitelist. No other seam could have fired either: the
  only wired non-operator seams are `retry_transition`,
  `delegation_result` and `task_agent_failure`, and node@3 had no model
  retry and no task-agent call in the window.

  The one substantive turn in that window — `.` carrying an attachment —
  was on a workstream resident on **node@5**, and node@5 observed it. The
  sensor's liveness is not an inference: node@3's **next** cadence row
  follows at `2026-10-20T12:21:37Z`, after this snapshot.

What a restart does cost is *continuity of a single process*, not
continuity of the epoch. The epoch is a directory of observations, not a
process's memory, so the measurement is unaffected. The one thing a
restart does reset is the **dedupe state** and the in-process counters,
which are re-derived on the next event and are not part of the
acceptance arithmetic.

## The gate

| | |
|---|---|
| attributable events (B + B2) | **62** / 100 |
| raw observation rows | 75 |
| unverified (C) | 0 |
| provenance available | true |
| elapsed | 45.81 h / 48 h |
| **gate** | **PENDING** on both the count and the time |

Integrity checks are all met: zero unmatched, zero duplicate event ids,
zero orphans, boundary present.

**The count is live and it moves.** The epoch is receiving genuine operator
work, so the number rises on its own. The figure at any moment is a
measurement of that moment, not a fixed property of the epoch.

## The gate fails closed without a reconciliation

The raw count and the attributable count are **different numbers**, and only
the second one can certify the campaign. `62` attributable events and `75`
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
deployed fingerprint-only dedupe key.

This is the defect the staged correction addresses. It is reported here
rather than hidden, and the correction **does not recover** the lost
observations — it prevents future suppression. See
[s6-dedupe-identity.md](s6-dedupe-identity.md).

## The count the corrected matcher would report

The deployed matcher credits a `delegation_result` — machine work — to an
operator turn. The corrected matcher removes that inflation, so from the
**same population** it reports one fewer attributable event.

**Measured 2026-10-20T11:10Z**, on the live epoch, same events and same
store rows, with only the matcher changed:

| matcher | B | B2 | **B + B2** | C | C2 | C3 |
|---|---|---|---|---|---|---|
| deployed (type-blind, row-reusing) | 61 | 2 | **63** | 11 | 1 | — |
| corrected (eligibility + exact matching) | 60 | 2 | **62** | 0 | 0 | 13 |

The deployed matcher is named for what it is: it applies **no event-type
eligibility rule** and it is an **independent nearest-neighbour match**, so
it can hand the same store row to two events. On this population it claims
**two** rows twice. The corrected matcher is both: it admits only
`operator_instruction` events, and it solves an exact minimum-cost,
maximum-cardinality assignment in which each row is consumed at most once.

The difference is exactly one event, and the direction is correct: the gate
must not be satisfied by counting machine work as operator work. The
eligibility rule removes the inflated event; the exact matcher does not
change the count on this population (it guarantees the count, it does not
move it).

The other column is the point of the correction. The deployed matcher
files the system's own transitions — a retry, a delegation result, a
task-agent failure — into **C (unverified)**, because it attributes them
to operator rows and then finds the origin does not match. The corrected
matcher **excludes** them from the assignment and reports them in their own
bucket, **C3 = 13**. Nothing disappears: the 13 are still counted, just
counted as what they are. The gate reads the same 62 either way, and the
difference is only visible in how the same 62 is accounted for.

## Deployment status

**NOT DEPLOYED.** The corrections on this branch are staged and qualified.
Nothing in this repository has been activated.

| | |
|---|---|
| production state | unchanged since the T2 boundary, except for one unrelated service restart (above) |
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
