# S6 engineering record — index

The S6 shadow-sensor measurement campaign: what the sensor is, what it
measures, how its output is accounted for, and how a reading can be checked
against the evidence that produced it.

> **Deployment status: NOT DEPLOYED.** The corrections on this branch are
> staged and qualified. The running system is the deployed commit named in
> §2 of the main document. See
> [s6-production-status.md](s6-production-status.md) for the current
> production snapshot.

## Documents

| document | subject |
|---|---|
| [s6-shadow-sensor.md](s6-shadow-sensor.md) | architecture, the seams, the privacy projection, the two acceptance dimensions, the production lineage |
| [s6-dedupe-identity.md](s6-dedupe-identity.md) | the dedupe identity contract — the invariant, the defect, the proof |
| [s6-event-provenance.md](s6-event-provenance.md) | the classification ledger — A / B / B2 / C / C3 / E / F, and the matcher corrections |
| [s6-hash-chain.md](s6-hash-chain.md) | the transitive trust chain — what it covers and what it does not |
| [s6-production-operations.md](s6-production-operations.md) | upgrade, rollback, the runbook, and what a future deployment would involve |
| [s6-evidence-and-artifacts.md](s6-evidence-and-artifacts.md) | source ↔ artifact correspondence, the manifest, the evidence set |
| [s6-production-status.md](s6-production-status.md) | the current production snapshot |

## Code

| path | subject |
|---|---|
| `turnstone/core/session.py` | the six observation seams; the admitted turn identity is threaded out of the append functions |
| `turnstone/core/shadow_driver.py` | the dedupe key: `(fingerprint, turn identity)` |
| `turnstone/core/sensor_lifecycle.py` | the hook signature; the fail-open boundary |
| `tools/s6/collect.py` | the epoch collector: coverage, grading split, the gate |
| `tools/s6/liveness.py` | the liveness reporter: operator-work coverage |
| `tools/s6/provenance.py` | the event-provenance reconciliation |
| `tools/s6/s6-t2-execute.sh` | the single operator command |
| `tools/s6/proofs/` | the pure-logic proofs (522 checks) |
| `tools/s6/driver/` | the driver's own proofs, including the hash-chain negative control |

## Quick start

```bash
bash tools/s6/proofs/run-all.sh     # 522 checks, 0 failed
python -m pytest -q                 # the sensor suite
```

## The three identities

| identity | value |
|---|---|
| deployed commit | `5427ded3aa7668975e2cef2621e81b9131498281` |
| deployed overlay | `/opt/turnstone-overlays/sensor-v187-5427ded` |
| staged candidate | the tip of this branch |

These are separate. A commit on this branch is **not** deployed.
