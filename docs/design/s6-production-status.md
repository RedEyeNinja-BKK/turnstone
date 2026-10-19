# S6 production status

> A snapshot of the running system. It is **observed**, not proposed.
> Read it with [s6-index.md](s6-index.md).

## Snapshot

**Observed 2026-10-19T19:30Z.**

| item | value |
|---|---|
| Turnstone | 1.8.7 |
| services | 6/6 active — `turnstone-console`, `turnstone-node@2` … `@5` |
| overlay | `/opt/turnstone-overlays/sensor-v187-5427ded` (444 files) |
| deployed commit | `5427ded3aa7668975e2cef2621e81b9131498281` |
| epoch | `s6-t2` |
| boundary | `T2` = `2026-10-18T13:21:51Z` |
| elapsed | 30.1 h of the 48 h floor |
| next deadline | `2026-10-20T13:21:51Z` (the 48 h floor) |

No service has been restarted since the boundary: all six still carry their
`T2` start times.

## The gate

| | |
|---|---|
| attributable events (B + B2) | **47** / 100 |
| raw observation rows | 54 |
| unverified (C) | 0 |
| provenance available | true |
| elapsed | 30.1 h / 48 h |
| **gate** | **PENDING** on both the count and the time |

Integrity checks are all met: zero unmatched, zero duplicate event ids,
zero orphans, boundary present.

**The count is live and it moves.** The epoch is receiving genuine operator
work, so the number rises on its own. The figure at any moment is a
measurement of that moment, not a fixed property of the epoch.

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

This is why the expected corrected figure is *lower*, and it is the correct
direction: the gate must not be satisfied by counting machine work as
operator work.

## Deployment status

**NOT DEPLOYED.** The corrections on this branch are staged and qualified.
Nothing in this repository has been activated.

| | |
|---|---|
| production state | unchanged since the T2 boundary |
| successor slot | not installed |
| services restarted | none |
| measurement epochs | `s6-t0`, `s6-t1`, `s6-t2` — all preserved, none merged |

## Preserved

Credentials, elevated permissions, the running services, every immutable
overlay, all historical telemetry, the watches, and the rollback anchors are
untouched.

## How to re-read this

```bash
sudo -u turnstone /opt/turnstone-venv/bin/python \
  tools/s6/liveness.py --epoch /var/lib/turnstone/sensor/s6-t2 --json
```
