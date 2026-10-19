# S6 production operations — upgrade, rollback and the runbook

> **Status: the corrections on this branch are NOT DEPLOYED.** The
> procedures below describe the deployed S6-t2 arrangement and the
> procedure that would be used to roll the staged corrections. Running any
> of it requires explicit operator authorization.

## 1. The current production arrangement

| item | value |
|---|---|
| Turnstone | 1.8.7, six services |
| overlay | `/opt/turnstone-overlays/sensor-v187-5427ded` (444 files, root-owned, read-only) |
| epoch | `s6-t2` |
| boundary | `T2` = `2026-10-18T13:21:51Z` |
| services | `turnstone-console`, `turnstone-node@2` … `@5` |
| gate | 100 attributable events **and** 48 h from the boundary |

**Observed (2026-10-19T19:09Z):** all six services active, none restarted
since the boundary; gate at 44 attributable / 100, 29.8 h elapsed.

## 2. The single operator command

```bash
sudo bash /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6/s6-t2-execute.sh
```

It is the only file an operator invokes. It:

1. verifies the SHA-256 of every artifact it will execute or load, **as
   root, before running anything**;
2. ensures the immutable slot — **idempotent**: absent → install, present
   and exact → verify, present and different → **refuse** without
   overwriting;
3. runs a non-mutating dry run;
4. rolls the six services one at a time, with a per-service verifier;
5. establishes the epoch boundary **only after all six verify** — the
   activation script owns that step; the driver never writes it;
6. records the boundary, runs the read-only reports as the service account,
   and **proves the reports changed nothing** (a before/after snapshot of
   the epoch tree).

On any failure it prints the real exit code and the current six-service
status, and exits with that code. It never closes the operator's shell.

### 2.1 Exit codes

| code | meaning | state |
|---|---|---|
| 0 | complete | measurement active, not complete |
| 1 | artifact hash mismatch, or not root | **nothing was executed** |
| 2 | the activation script refused before changing anything | unchanged |
| 3 | a service failed verification; its automatic rollback verified | earlier services stay on the new slot |
| 4 | the automatic rollback did **not** verify | that unit may be in an unknown state — inspect before re-running |
| 5 | work was in flight (busy cluster) | nothing written; wait and re-run, the slot install is idempotent |

### 2.2 Re-running is safe

A run that stopped at exit 5 may have left the slot installed. The next run
does **not** require the operator to skip a step by hand: the slot install is
idempotent, and services already upgraded are **verified without a
restart**.

## 3. Rollback

Per service, predecessor-aware:

```bash
# read-only: what state is every unit in?
bash activate-cluster-5427ded.sh --status

# show the plan, change nothing
bash activate-cluster-5427ded.sh --dry-run

# roll back everything eligible, one unit at a time
bash rollback-cluster-5427ded.sh

# a single unit
bash rollback-cluster-5427ded.sh turnstone-node@4
```

A rollback takes a **byte-exact, checksummed backup** of the predecessor
drop-in before replacing it, and refuses to proceed if the backup does not
verify. Earlier services keep running: a partial rollout is reported
accurately, not hidden.

**Emergency path — different authority.** A manual rollback of a unit whose
predecessor cannot be proven healthy is a different action with a different
authority, and is stated as such in the runbook rather than folded into the
ordinary path.

## 4. The idle gate

`preflight_idle()` inspects **all six services before any mutation**. If any
service has an in-flight or ambiguous workstream, the script exits 5 and
writes nothing. The gate is re-checked immediately before **each** service's
restart, so a cluster that becomes busy mid-roll is caught at the next
step rather than at the start.

`--allow-busy` is refused by design: it would kill in-flight sessions.

## 5. Reporting

Read-only, as the service account:

```bash
# the current epoch
sudo -u turnstone /opt/turnstone-venv/bin/python \
  s6/collect.py  --epoch /var/lib/turnstone/sensor/s6-t2
sudo -u turnstone /opt/turnstone-venv/bin/python \
  s6/liveness.py --epoch /var/lib/turnstone/sensor/s6-t2
```

The reports never write telemetry, never call the decision backend, and
never touch a service. The driver proves this by snapshotting the epoch
tree before and after and failing the run if either report changed
anything.

Historical epochs are read explicitly and are **never merged** into the
current one. `s6-t0` is preserved as incomplete/undercounted and is never
presented as acceptance data.

## 6. The qualification commands

Everything below is read-only or sandboxed; nothing installs a slot,
restarts a service, writes an epoch, or touches production telemetry.

```bash
# the measurement and driver proofs  -> 439 checks, 0 failed
bash tools/s6/proofs/run-all.sh

# the sensor suite
python -m pytest -q

# the red-first proof: the new tests fail against the pre-fix code
python /var/tmp/sensor-audit/redfirst_check.py
```

## 7. What a future deployment would involve

**Not authorized. This section is a plan, not a procedure that has been
run.** Before any of it happens, the operator must authorize it explicitly.

1. **The deployable commit** — the tip of
   `fix/s6-sensor-dedupe-provenance-20261020`, identified by its SHA.
2. **The immutable overlay artifact** — a staged tree whose manifest
   matches the deployable commit's `turnstone/` tree byte for byte, with
   the same 444-file discipline.
3. **Qualification** — the full sensor suite, the red-first proof, the
   proof suite, and the lint-parity check, all re-run against the staged
   bytes rather than the working tree.
4. **Expected effect on coverage** — the dedupe correction makes distinct
   turns visible, so the attributable count **rises** by the number of
   previously suppressed turns (currently 3) plus the two turns the
   attachment seam was missing. The matcher correction **lowers** it by one.
   Both effects must be stated before the rollout, not discovered after.
5. **A fresh epoch** — the corrections change what is *collected*. The
   `s6-t2` counts are a **historical measurement of the pre-fix system** and
   must be preserved and interpreted as such. A fresh `s6-t3` epoch would be
   needed for a clean acceptance measurement of the corrected system; the
   `s6-t2` figures are kept, not overwritten.
6. **The rollout** — the same idle-gated, predecessor-aware, one-service-at-
   a-time procedure, with per-service verification and automatic rollback.

## 8. Preserved

- all credentials, elevated permissions, running services;
- every immutable overlay;
- all historical telemetry and every epoch directory;
- the watches;
- the rollback anchors (the predecessor drop-ins and their checksummed
  backups);
- `s6-t0` and `s6-t1`, untouched and never merged.
