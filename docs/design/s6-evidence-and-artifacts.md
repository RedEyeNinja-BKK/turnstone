# S6 evidence, artifacts and source correspondence

> **Status: the corrections on this branch are NOT DEPLOYED.** This
> document records what the evidence is, where it lives, and how to check
> it.

## 1. Three identities, kept separate

| identity | value | where it lives |
|---|---|---|
| **deployed commit** | `5427ded3aa7668975e2cef2621e81b9131498281` | git history |
| **deployed overlay** | `/opt/turnstone-overlays/sensor-v187-5427ded` (444 files) | the running system |
| **staged candidate** | tip of `fix/s6-sensor-dedupe-provenance-20261020` | this branch |

A pushed commit is **not** a deployed commit. Nothing in this repository
implies deployment.

## 2. Source ↔ artifact correspondence

**The correspondence is exact, not approximate.** The `turnstone/` tree of
the deployed commit is byte-identical to the deployed overlay:

```bash
$ diff -r <deployed-commit>/turnstone \
         /opt/turnstone-overlays/sensor-v187-5427ded/turnstone
(no output)
```

Both are 444 files with identical file lists. The deployed commit's tree
also carries the test suite (13 test files) and `ruff.toml`, which a slot
does not install — a slot installs the runtime package only.

The staged candidate differs from production in exactly three production
files:

```
turnstone/core/session.py
turnstone/core/shadow_driver.py
turnstone/core/sensor_lifecycle.py
```

## 3. The manifest

`tools/s6/staging-5427ded/MANIFEST.sha256` is the 444-entry digest list of
the deployed runtime package. It is committed byte-identical to the deployed
artifact, and the deployment driver pins it by SHA-256 before running
anything.

The driver also pins six more artifacts — the slot installer, the activation
script, the rollback script, and the three measurement modules. See
[s6-hash-chain.md](s6-hash-chain.md) for the transitive closure and for
exactly what the chain does and does not attest.

## 4. Where the operational evidence lives

The operational evidence for this campaign is **not** published in this
repository. It stays on the VM, under
`/var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/`, because
it contains material that must not be published:

- the telemetry databases (`*.db`, `*.db-wal`, `*.db-shm`);
- quarantined pre-`T0` test writes (raw JSONL observation and cadence
  rows);
- two large git bundles of the candidate tree;
- the live reports, which quote per-service counts.

What is published here is the **code that produced the evidence** and the
**proofs that check the code**, so a reviewer can re-derive the numbers
from a checkout rather than take them on trust.

## 5. Reproducing the counts

```bash
# the measurement and driver proofs
bash tools/s6/proofs/run-all.sh

# the live reports, read-only, as the service account
sudo -u turnstone /opt/turnstone-venv/bin/python \
  tools/s6/collect.py  --epoch /var/lib/turnstone/sensor/s6-t2
sudo -u turnstone /opt/turnstone-venv/bin/python \
  tools/s6/liveness.py --epoch /var/lib/turnstone/sensor/s6-t2
```

## 6. The evidence set, and what each piece proves

| artifact | checks | what it establishes |
|---|---|---|
| `tools/s6/proofs/liveness_proof.py` | 71 | the liveness reporter's coverage arithmetic |
| `tools/s6/proofs/collector_proof.py` | 56 | the collector's epoch window and grading split |
| `tools/s6/proofs/t1_exclusion_proof.py` | 27 | pre-boundary rows are excluded, epoch-name independent |
| `tools/s6/proofs/epoch_selection_proof.py` | 57 | each epoch closes at the next epoch's boundary |
| `tools/s6/proofs/watch_condition_proof.py` | 29 | the watch's stop condition |
| `tools/s6/proofs/watch_lifetime_proof.py` | 60 | the watch's budget and lifetime |
| `tools/s6/proofs/provenance_proof.py` | 35 | the provenance classification |
| `tools/s6/proofs/gate_provenance_proof.py` | 22 | the gate cannot silently fall back to raw rows |
| `tools/s6/proofs/boundary_marker_proof.py` | 36 | the boundary is written once, never overwritten |
| `tools/s6/proofs/rate_basis_proof.py` | 28 | the rate, the ETA and the labels key on the gate's count |
| `tools/s6/proofs/provenance_required_proof.py` | 24 | acceptance **cannot** be certified without a reconciliation |
| `tools/s6/proofs/matching_cardinality_proof.py` | 23 | the attribution is an **exact** maximum-cardinality assignment |
| `tools/s6/proofs/handoff_digest_proof.py` | 33 | every digest the handoff states describes the bytes on disk |
| `tools/s6/proofs/staleness_control.py` | 38 | the proofs above **fail** against a mutated tree |
| `tools/s6/driver/script-tests-execute.sh` | 40 | the driver's step order, exit codes, read-only proof |
| `tools/s6/driver/script-tests-execute-chain.sh` | 13 | the hash chain's **negative control** |
| **total** | **592** | |

Plus the sensor suite (`374 passed, 2 skipped`) and the red-first proof,
which shows the new tests **fail** against the pre-fix code.

## 7. The red-first evidence

| test file | pre-fix result |
|---|---|
| `tests/test_sensor_turn_identity_dedupe.py` | 9 failed, 4 passed |
| `tests/test_sensor_turn_identity_session.py` | 8 failed, 2 passed |
| `tests/test_provenance_one_to_one.py` | 14 failed |
| `tools/s6/driver/script-tests-execute-chain.sh` | 7 passed, 6 failed |

Each of these is a proof that **fails against the defective implementation**
and passes after the fix. A test that only passes afterwards proves
nothing.
