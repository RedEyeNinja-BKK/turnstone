# ROOT HANDOFF — S6-t2 (corrected)

**One command.** Paste it into your normal SSH session; it runs as root in a
child process and never alters your shell.

```bash
sudo bash -c 'cd /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6 && echo "cddf81c83acf7931c2f25aacc271c8cc6dc105231212b9fbafd24b75bdb10f09  s6-t2-execute.sh" | sha256sum -c - && bash s6-t2-execute.sh'
```

That is the whole operator interface. No `set -euo pipefail` is typed into your
interactive shell, and nothing exits your session. The driver reports its own
exit code and the six-service status on both success and failure.

> The hash is the literal SHA-256 of `s6-t2-execute.sh` as shipped, not a
> placeholder. `sha256sum -c` recomputes it from the file on disk and refuses
> to run the script on any difference.

---

## 1. Authoritative epoch marker: `T2`

| item | value |
|---|---|
| **marker filename** | **`/var/lib/turnstone/sensor/s6-t2/T2`** |
| marker content | one UTC instant, `YYYY-MM-DDTHH:MM:SSZ` |
| written by | `activate-cluster-5427ded.sh` → `write_boundary_if_absent()` |
| written when | only after all six services verify on `5427ded` |
| read by | `collect.py`, `liveness.py` via `collect.read_boundary()` |
| earlier epochs | `s6-t0/T1`, `s6-t1/T1` — untouched, still read by the same resolver |

The rule now: **the marker's filename follows the epoch number; its meaning does
not.** Writers use the epoch's own name. Readers accept `T2` then `T1`, and if
both exist with *different* instants they refuse and the acceptance slice fails
closed (`gate.boundary_ok = false`, `BOUNDARY FAULT` printed).

Why this matters: the earlier handoff and the activation script agreed only by
coincidence of a hardcoded filename. Once the epoch was renamed, one side read
one name and the other wrote a different one - an operator would have got
`No such file or directory` after a successful six-service activation, with
nothing actually wrong. That is now impossible in either direction:
`evidence/boundary_marker_proof.py` (36 checks) proves the writer, both readers,
and this document's stated path all resolve to the same file.

---

## 2. What the single command does

`s6-t2-execute.sh`, in order, stopping at the first failure:

| step | action | on failure |
|---|---|---|
| 1 | verify SHA-256 of the installer, activation script, rollback script, `collect.py`, `liveness.py`, and the 444-entry manifest | exit 1, nothing executed |
| 2 | `install-slot.sh ensure` — **idempotent** | the installer's own code (1), nothing overwritten |
| 3 | `activate-cluster-5427ded.sh --dry-run` — writes nothing | the activation script's own code (2 refused, 5 busy) |
| 4 | roll six services, one at a time, predecessor-aware | exit 2/3/4/5 per the activation script |
| 5 | read the boundary the activation script wrote; print the capability record | exit 3 if absent |
| 6 | run `collect.py` and `liveness.py` **as `turnstone`**, then prove the epoch tree is byte-unchanged | exit 3 if anything changed |

Every failure path prints the real exit code, the log path, and the current
six-service status, then exits. Your SSH session stays open.

### Step 2 is why a busy-cluster exit 5 is no longer a problem

```
slot absent                 -> verify staging, install, verify installed
                               manifest + root:root/0555/0444
slot present, exact         -> verify the INSTALLED bytes and posture, continue
slot present, different     -> REFUSE, overwrite nothing
```

So the sequence "busy cluster → exit 5 → wait → run the same command again"
needs no manual step. The second run verifies the slot it installed and carries
on; services already upgraded are verified without a restart.

**On errexit:** the driver disables `-e` for itself and captures every step's
code inside an `if`, so it behaves identically whether you invoke it as
`bash s6-t2-execute.sh` or `bash -e s6-t2-execute.sh` (or from a shell that
exported `SHELLOPTS`). It cannot abort at a failing step and lose the report.
Verified in both launch modes by `evidence/script-tests-execute-errexit.sh`.

Steps 2 and 3 pass their own exit code through instead of collapsing it to 1,
so a busy cluster that stops at the dry-run gate still reports **5** and gets the
wait-and-re-run guidance rather than looking like a broken artifact.

Exit codes from step 4: **0** done · **2** refused (drift/preflight) · **3**
failed and rolled back verified · **4** failed, rollback did not verify · **5**
work in flight. `--allow-busy` is refused by design; an advisory sensor is never
worth terminating a live workstream.

---

## 3. Rollback — per service, predecessor-aware

`rollback-cluster-5427ded.sh`. **Sequential, one unit at a time, in reverse
upgrade order.** It never restarts six services together and never re-renders a
rollback file: the bytes it restores are the bytes that were running before the
upgrade, and that equality is asserted after the copy.

```bash
# read-only: what state is every unit in?
sudo bash /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6/rollback-cluster-5427ded.sh --status

# show the plan, change nothing
sudo bash .../rollback-cluster-5427ded.sh --dry-run

# roll back everything eligible, one unit at a time
sudo bash .../rollback-cluster-5427ded.sh

# a single unit
sudo bash .../rollback-cluster-5427ded.sh --unit turnstone-node@3
```

Per unit it will:

1. classify `95-sensor.conf` — `candidate` / `predecessor` (byte-identical to
   the preserved backup) / `predecessor-other` / `drift` / `absent`;
2. **refuse** on `drift`, `predecessor-other` or `absent` and change nothing —
   those are configurations this run did not create;
3. resolve the preserved backup from `INDEX.tsv` (last entry per unit) and prove
   it **byte-exactly**: the file exists, its SHA-256 matches the checksum
   recorded when it was taken, its name carries **this** unit, and its bytes
   select `cb7fece`. A missing, modified or misfiled backup is refused;
4. global idle preflight (whole cluster) → per-unit idle recheck;
5. atomic restore (write sibling, fsync, rename over), then `cmp` against the
   backup;
6. `daemon-reload`, then restart **that unit only**, then verify: exec'd PID in
   the unit's cgroup with the right binary and port, `PYTHONPATH` = `cb7fece`,
   sensor still enabled/sanitized, store dir = `s6-t1/<service>`, tokenizer dir,
   node/head identity, health;
7. on failure: stop, report exactly which units are rolled back and which are
   not, and exit 3. Nothing later is touched.

Resumable: a unit already on the preserved predecessor is **verified, not
restarted**. Re-running continues where it stopped.

Telemetry is never deleted. `s6-t1` and `s6-t2` remain as evidence. Immutable
overlays, credentials, permissions, OAuth, Output Guard and routing are not
touched by any path in this script.

### Emergency path — different authority, stated

```bash
sudo bash .../rollback-cluster-5427ded.sh --emergency turnstone-node@3
```

This exists for a **genuinely unhealthy** service. It:

- **refuses** if the unit is healthy and active;
- bypasses the idle gate **for that unit only**, so it **MAY TERMINATE
  IN-FLIGHT WORK**;
- is recorded in the rollback log with an explicit line saying so.

It is not a faster version of the normal rollback. Normal rollback waits; the
emergency path trades someone's work for service availability, and only the
operator can make that trade.

---

## 4. Reporting

`collect.py` and `liveness.py` both take `--epoch` and both resolve the marker
through the same function. The driver runs them **as `turnstone`** (via
`runuser`) and proves read-only behaviour by snapshotting every file name, size
and mtime under the epoch before and after; a difference aborts the run with
exit 3. Neither script writes telemetry or the marker — verified by that
snapshot and by `boundary_marker_proof.py §7`.

Two dimensions, never merged:

**A — operator-work observation coverage.** Denominator from the conversation
store (attachment-delivered turns included when the running code can see them),
numerator the admitted observations. Structural-only events count here. Gate:
≥100 genuine unique admitted events **and** ≥48 h from the boundary, with zero
unmatched, zero duplicates, zero orphans, and a healthy boundary.

**B — qualified semantic grading.** Denominator: events whose content was
available to the grader (`text`, `text_plus_attachments`). `attachments_only` is
excluded from the denominator — the grader received a descriptor, not a
document. Reported alongside: `graded_full`,
`graded_text_with_attachment_context`, `graded_structural_only`. No aggregate
threshold is proposed at n≈2; derive one at n ≥ 20 gradeable events.

> Dimension A passing does **not** validate semantic grading of
> attachment-only work, and cannot.

### After the rollover: which epoch, and how to read it

Neither script needs `--epoch` any more. The default is the epoch with the
latest **authoritative boundary** under `/var/lib/turnstone/sensor`, and both
reports print the choice explicitly (`selected by: newest boundary T2=…`). The
scheduled watch passes no `--epoch`, so it follows the live campaign instead of
the campaign it was created in.

```bash
# current epoch (s6-t2), read-only, as the service account
sudo -u turnstone env \
  PYTHONPATH=/opt/turnstone-overlays/sensor-v187-5427ded \
  HOME=/var/lib/turnstone \
  /opt/turnstone-venv/bin/python \
  /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6/liveness.py

sudo -u turnstone env \
  PYTHONPATH=/opt/turnstone-overlays/sensor-v187-5427ded \
  HOME=/var/lib/turnstone \
  /opt/turnstone-venv/bin/python \
  /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6/collect.py

# historical epoch, explicitly
... liveness.py --epoch /var/lib/turnstone/sensor/s6-t1
```

`PYTHONPATH` must name the slot the services are actually running: the coverage
denominator classifies operator turns with that slot's own
`is_substantive_instruction`, so a different tree would measure a different
sensor. The reports state which slot answered (the `CAPABILITY` record for the
epoch, or `running code carries the attachment seam: …` for the current one).

A historical epoch is never re-interpreted through today's code: `s6-t1` keeps
its text-only eligibility and reports `100%` coverage of the 3 turns it could
see, with the 12 attachment-delivered turns named as a known blind spot rather
than counted against it.

`--json` mode writes nothing to stderr: the scheduled watch appends stderr to
stdout before parsing, so a diagnostic there would corrupt the payload and fire
the watch. Skip notes ride inside the JSON as `epochs_skipped`.

Coverage below the floor is now a **FAILURE** (exit 1), not a caveat: if
independently verified operator work is going unobserved, the watch
(`data["verdict"] == "FAIL"`) alerts instead of recording a healthy poll.

---

## 5. Artifact identity

```
candidate commit  5427ded3aa7668975e2cef2621e81b9131498281
staging tree      /var/lib/turnstone/staging/sensor-v187-5427ded   (444 files)
slot target       /opt/turnstone-overlays/sensor-v187-5427ded      (absent)
epoch             /var/lib/turnstone/sensor/s6-t2                  (absent)
predecessor       /opt/turnstone-overlays/sensor-v187-cb7fece + s6-t1
```

Hashes of everything the driver executes or trusts:

```
f785179fad1963d541c67bf22a22dd1916a58960bace195c5666c8ef1fbb267c  staging-5427ded/MANIFEST.sha256      (444 entries)
823a094e6b59db8dff74964d5849f49fb2b63fd6a3b152a182e9e6f79e683f68  staging-5427ded/install-slot.sh
5843cc8ce518cfcdc16b5401ba49dbe9c493d282f5c21d0114a436f95f3f7a83  activate-cluster-5427ded.sh
9f8d7815708e6442d5274c80f46609a29020706e5c1a8749798e0cdd7e938055  rollback-cluster-5427ded.sh
089cb1044781cbe6299a967c759563f3df19dacb9170963fef7674849a34b892  collect.py
13988c65607ca3dd5f12a863d1908950eb3ee8bb9add0bf514ceec643edf2f6a  liveness.py
fb2397b03abed2250a706f5487bb192424806083606310288cea3d8f256321cc  provenance.py
```

`cddf81c83acf7931c2f25aacc271c8cc6dc105231212b9fbafd24b75bdb10f09  s6-t2-execute.sh` — the driver itself, checked by the inline
`sha256sum -c` in the command at the top of this document.

Byte manifests, regenerated after the last write:

```
s6/SHA256SUMS  129 entries   sha256sum -c clean
SHA256SUMS (artifact root)  158 entries   sha256sum -c clean
```

Rule: every regular file under the artifact root, excluding `SHA256SUMS`
itself, `__pycache__`, `.ruff_cache`, and the transient harness sidecars
`s6/.turnstone.db*`. If you want a second, independent check before running the
command, verify the tree from the artifact root:

```
cd /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017 && sha256sum -c SHA256SUMS
```

The staged sensor tree is **not rebuilt** for this correction: its manifest is
unchanged, and `install-slot.sh verify` proves it against both anchors (R2 + 8
files, cb7fece + 4 files) before anything is installed.

---

## 6. What is unchanged

- No sensor code, no staging bytes, no manifest entry changed in this correction.
- `cb7fece`, `a07bf6e`, `f76c813`, both R2 overlays and `typed-decision-v187*`
  are untouched.
- `s6-t0` and `s6-t1` are read-only history; nothing merges them.
- Credentials, sudoers, OAuth stores, registry, Output Guard, routing,
  Switchyard, users/groups, restart policy: not referenced by any script here.
- Production right now: **6/6 on `sensor-v187-cb7fece`, `SENSING`, `s6-t1`
  collecting.** This correction did not touch it.

---

## 7. After it runs

Confirm the boundary by hand if you want to:

```bash
cat "$EPOCH/T2"     # EPOCH=/var/lib/turnstone/sensor/s6-t2
```

Expected tail of the output:

```
  on 5427ded: 6/6   on the cb7fece predecessor: 0/6
  T2 = 2026-10-18T…Z  (authoritative acceptance boundary for s6-t2)
  capability.attachments_observed=1
  READ-ONLY PROOF: the epoch tree is unchanged by the reports
```

`liveness.py` must say:

```
capability anchored on : running code carries the attachment seam: /opt/turnstone-overlays/sensor-v187-5427ded
```

`MIXED cluster` there means one service did not take the upgrade — run
`--status`, find it, re-run the same command.

The gate will read `PENDING` on both counts. That is correct: ≥100 events and
≥48 h from the boundary cannot be met before `2026-10-20`.
