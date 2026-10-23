# Agent instructions

Operational contract for any agent session working in this repository or on
this host. Normative, not advisory.

## Builds

- Build and test **only** through the canonical wrapper:
  ```bash
  sy-build campaign "<short reason>" -- cargo build --release
  sy-build campaign "<short reason>" -- cargo test --workspace
  ```
  Source: `tools/storage/sy-build`. Installed as `/var/lib/turnstone/bin/sy-build`
  (on `PATH` for the service account via `~/.profile`).
- There is exactly one target directory: `/var/lib/turnstone/sytarget`.
- **Never** set `CARGO_TARGET_DIR` to an ad-hoc path. A per-session directory
  under `/var/tmp` is the mechanism that produced 43 GiB of duplicate debug
  output in five days.
- One campaign at a time. If the wrapper exits `3`, another campaign holds the
  lock — wait, do not work around it.
- If the wrapper exits `4`, there is not enough free space. Do not start a build
  "anyway"; run `storage-monitor.sh` and report.
- Do not run `cargo` directly. It bypasses the lock, the budget check and the
  cleanup, and the monitor will flag the resulting tree as an unexpected build
  root.

## Temporary files

- Put build and test scratch under `/var/lib/turnstone/`, never `/var/tmp`,
  unless the wrapper owns the cleanup.
- `/var/tmp` is scratch: nothing durable may be written there, and anything
  found there may be reclaimed.
- Do not copy the S6 evidence corpus. Reference
  `/var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/` in place.
- Never place a SQLite database or its `-wal`/`-shm` in a temp directory.

## Cleanup

- Clean your own campaign's dev output when the campaign ends.
- Do not delete another session's output, and do not run filesystem cleanup
  unless the task explicitly authorises it.
- `storage-prune.sh` needs `--apply --confirm`. Never pass `--confirm` to
  "tidy up".

## Storage checks before expensive work

```bash
/var/lib/turnstone/bin/sy-build status
```

Reports target size, filesystem usage, free space, whether a build is running,
and whether the lock is held. If it says a campaign would be refused, it would
be refused.

## Secrets

Never write credentials, tokens, private keys, database contents, or live
operator conversation identifiers into the repository, a commit message, a PR
body, or a log file that is published. Use placeholders with the same shape.
