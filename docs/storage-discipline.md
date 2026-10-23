# Storage discipline — build, test and cleanup contract

This document is the operating contract for anything on this host that compiles
code, runs tests, or writes temporary files. It exists because 57 GiB of
debug build output accumulated in five days across five separate directories,
taking the root filesystem to 85% and blocking work.

## 1. The rule

There is exactly **one** Cargo target directory:

```
/var/lib/turnstone/sytarget
```

Every build goes through the wrapper in this repository:

```bash
sy-build campaign "<reason>" -- cargo build --release
sy-build campaign "<reason>" -- cargo test --workspace
```

| Item | Path |
|---|---|
| source | `tools/storage/sy-build` |
| installed | `/var/lib/turnstone/bin/sy-build` |
| policy | `tools/storage/storage-policy.sh` |
| monitor (read-only) | `tools/storage/storage-monitor.sh` |
| pruner (gated) | `tools/storage/storage-prune.sh` |
| bypass detection (read-only) | `tools/storage/storage-detect.sh` |
| daily summary (read-only) | `tools/storage/storage-daily.sh` |
| alerting (transition-based) | `tools/storage/storage-alert.sh` |
| tests | `tools/storage/tests/run-storage-tests.sh`, `tools/storage/tests/run-detection-tests.sh` |

Not `cargo build`. Not `CARGO_TARGET_DIR=/var/tmp/something cargo build`.

## 2. Why a wrapper and not a convention

The previous five directories were not created maliciously; each was a
reasonable local decision by a session that did not know a convention existed.
`sy-build` makes the correct thing the path of least resistance:

| Guarantee | How |
|---|---|
| One target dir | fixed in the wrapper, not overridable by accident |
| No incremental bloat | `CARGO_INCREMENTAL=0` (incremental state measured 2.1–5.7 GiB per tree) |
| One campaign at a time | `flock` on `/var/lib/turnstone/.build.lock`, held for the whole cargo lifetime **and inherited by its children** |
| Enough headroom | refuses below 35 GiB free, and refuses if the build would leave under 5 GiB spare |
| No orphaned output | dev profile removed on exit, on failure, and on SIGTERM/SIGINT/SIGHUP |
| Survives SIGKILL | a campaign state file records the PID; the next invocation reports an orphan (it does not auto-delete) |

## 3. What is never removed

The wrapper only ever removes `<target>/debug`. It never touches:

- source trees, `.git`, worktrees, branches, stashes
- `target/release`, or any release executable
- `/var/lib/turnstone/{backups,artifacts,staging,canary,sensor-runtime,credentials,github}`
- `/opt/turnstone-overlays`, `/etc/turnstone`, `/srv/turnstone`
- PostgreSQL data or WAL

## 4. Monitoring vs deletion are separate

| Tool | Can delete? | Runs unattended? |
|---|---|---|
| `storage-monitor.sh` | **No** | Yes, every 4 hours |
| `storage-prune.sh` | Yes, but needs `--apply --confirm` | **No — not scheduled** |

A monitor that can also delete is a monitor nobody dares put on a timer.

Thresholds: **WARN 70%**, **PRUNE 85%**, **HARD 92%**.

`storage-monitor.sh` exit codes: `0` healthy, `10` warn, `11` prune, `12` hard,
`20` could not measure. It also reports *unexpected* build roots — an ad-hoc
`/var/tmp/sytarget-*` is exactly how the last incident started, so it is
detected and alerted even though it is never deleted automatically.

## 5. Temporary files

- pytest: use an explicit `--basetemp` under the canonical area, with a trap
  that removes it. Do not let pytest allocate unbounded into `$TMPDIR`.
- Evidence corpora: **reference** `/var/lib/turnstone/artifacts/...` in place.
  Do not copy it into a fresh `mktemp -d` per run; that produced 263 identical
  copies of the same corpus (3.4 GiB).
- Never place a SQLite database (or its `-wal`/`-shm`) in a temporary
  directory. The duplicate evidence copies each carried a 4 MB WAL.
- `/var/tmp` is scratch. Nothing durable may live there.

## 6. Known limitation: this is a convention, not a boundary

`sy-build` **cannot stop** a session that runs `cargo build` directly. That has
been tested, not assumed:

```bash
CARGO_TARGET_DIR=/var/tmp/whatever cargo build    # bypasses lock, budget, cleanup
```

It is *detected* (the monitor reports an unexpected build root and exits 10) but
not *prevented*. Enforcement would require one of:

1. `sy-build` on the default `PATH` for every account that builds, **and** a
   policy that direct `cargo` is a defect (weak — PATH is advisory).
2. A `cargo` shim earlier on `PATH` that execs the wrapper (bypassed by
   absolute paths and by setting `PATH`).
3. Filesystem permissions that deny write access to any target dir other than
   the canonical one (strong, but prevents legitimate out-of-tree builds and
   needs root).
4. Discretionary access control (AppArmor/SELinux profile) restricting write
   paths for the build account (strongest; needs root and a policy).

Until one of those is in place, treat "one build area" as a **team convention
with monitoring**, and say so plainly rather than claiming enforcement.

## 7. Detection and alerting

`storage-detect.sh` covers what a disk-usage check cannot see: a `cargo`
process that is not running under `sy-build`, a `CARGO_TARGET_DIR` that is not
the canonical one, an in-tree `target/` directory, abandoned build output,
concurrent campaigns, and growth since the previous sample. It reads
`/proc/PID/environ` only for own-uid processes.

`storage-daily.sh` produces the daily summary and runs the detection sweep as
part of its own execution, so one scheduled job covers both.

`storage-alert.sh` alerts on **transitions**, not levels: entering a worse
state, a bounded reminder while still in it, and recovery. A level-triggered
alert on a 4-hourly job fires six times a day for an unchanged condition, which
is how an alert channel stops being read.

No notification channel is configured on this host. Without
`STORAGE_ALERT_CMD`, alerts are appended to `storage-alerts.queue` and are not
sent anywhere. `STORAGE_ALERT_CMD` is invoked with the message on stdin; it must
be a simple command with arguments (it is not shell-evaluated, so redirection
operators are not interpreted). A failing dispatcher falls back to the queue so
an alert is never lost.

### Schedules

| Job | Schedule (local, Asia/Bangkok) | Mode |
|---|---|---|
| `storage-monitor.sh` | `17 */4 * * *` | read-only |
| `storage-prune.sh --explain` | `37 4 * * *` | dry run |
| `storage-daily.sh` | `17 5 * * *` | read-only |

Destructive pruning is not scheduled and requires `--apply --confirm`.
