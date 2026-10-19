#!/usr/bin/env bash
# S6-t2 — one-shot execution driver (RUN AS ROOT).
#
# This is the only file an operator invokes. It does the whole approved
# sequence and, critically, it REPORTS rather than aborting silently:
#
#   1. verify every script hash it is about to run (as root, before running)
#   2. ensure the immutable slot (idempotent: absent -> install, present and
#      exact -> verify, present and different -> refuse without overwriting)
#   3. non-mutating dry run of the rolling upgrade
#   4. roll the six services, one at a time, with the per-service verifier
#   5. establish the epoch boundary ONLY after all six verify (the activation
#      script owns that step; this driver never writes it)
#   6. record the boundary, run the read-only reports as the service account,
#      and prove the reports changed nothing
#
# On ANY failure it prints the real exit code and the current six-service
# status, and exits with that code. It never closes the operator's shell: it is
# a child process, and the caller is a plain `sudo bash <path>` — no `set -e`
# is ever applied to the operator's interactive shell.
#
# It does not touch credentials, sudoers, OAuth, the registry, Output Guard,
# routing, Switchyard, users/groups, restart policy, telemetry or any existing
# overlay. The only writes are: the staging->slot copy (step 2), the six
# drop-ins and the epoch dir/boundary (step 4/5 via the activation script), and
# its own log.
set -uo pipefail
# errexit is deliberately off, and is explicitly disabled here. This driver's
# whole contract is to REPORT a step's real exit code and the six-service
# status instead of dying at the failing line. A caller that exports SHELLOPTS,
# or launches this file as `bash -e`, would otherwise abort the run before the
# capture, before `fail`, and before the status block - turning a busy cluster
# into a silent exit and a read-only violation into a bare `diff` status.
# Every step below ALSO uses `if cmd; then rc=0; else rc=$?; fi`, so the capture
# stays correct even if errexit is re-armed after this point.
set +e

S6=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STAGE=/var/lib/turnstone/staging/sensor-v187-5427ded
SLOT=/opt/turnstone-overlays/sensor-v187-5427ded
EPOCH=/var/lib/turnstone/sensor/s6-t2
BOUNDARY=$EPOCH/T2
REPORT_USER=turnstone
PY=/opt/turnstone-venv/bin/python
LOG=/var/lib/turnstone/artifacts/s6/rollout/execute-$(date -u +%Y%m%dT%H%M%SZ).log

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
: > "$LOG" 2>/dev/null || true
say() { printf '%s\n' "$*" | tee -a "$LOG"; }

# Hashes of everything this driver will execute or trust. A mismatch stops the
# run BEFORE anything is installed or restarted.
#
# 2026-10-19: the collect/liveness hashes are updated for the C3 bucket render
# and the provenance.py hash is ADDED - provenance.py is loaded by both
# reporters, so it was trusted without being pinned. The chain is only a
# check if it covers everything that is executed or trusted.
#
# 2026-10-20: the collect/liveness hashes are updated again - the rate and the
# ETA now key on the attributable count, and the reports no longer label a raw
# row count as progress toward 100.
MANIFEST_SHA=f785179fad1963d541c67bf22a22dd1916a58960bace195c5666c8ef1fbb267c
INSTALLER_SHA=823a094e6b59db8dff74964d5849f49fb2b63fd6a3b152a182e9e6f79e683f68
ACTIVATE_SHA=5843cc8ce518cfcdc16b5401ba49dbe9c493d282f5c21d0114a436f95f3f7a83
ROLLBACK_SHA=9f8d7815708e6442d5274c80f46609a29020706e5c1a8749798e0cdd7e938055
COLLECT_SHA=f1d290b84fb419f9069dbcb72a96d0a55b64f7a97831b0f107da3dd44c275557
LIVENESS_SHA=13988c65607ca3dd5f12a863d1908950eb3ee8bb9add0bf514ceec643edf2f6a
PROVENANCE_SHA=5e5f6932eae97f13986041002db13294374e68688b4c55c39c41e81e3eb0dbca

status_block() {
  say ""
  say "-- six-service status"
  bash "$S6/activate-cluster-5427ded.sh" --status 2>&1 | tee -a "$LOG" \
    | sed -n '/CLUSTER ACTIVATION STATUS/,$p' || true
}

fail() {  # rc message
  local rc=$1; shift
  say ""
  say "=============================================================="
  say "STOPPED: $*"
  say "  exit code: $rc"
  say "  log:       $LOG"
  say "=============================================================="
  status_block
  say ""
  say "The SSH session stays open. Nothing further was attempted."
  case "$rc" in
    2)
      say "exit 2 means the activation script refused before changing anything:"
      say "  a drop-in that is neither this configuration nor a verified"
      say "  predecessor, or a preflight check that did not pass."
      say "  Nothing was changed. Inspect the named unit, then re-run." ;;
    3)
      say "exit 3 means a service failed verification; its automatic rollback verified."
      say "  Earlier services stay active on the new slot; the failing unit is"
      say "  back on the predecessor and healthy. Inspect the failure, then re-run:"
      say "  services already upgraded are verified without another restart." ;;
    4)
      say "exit 4 means the automatic rollback did not verify. That unit may be in"
      say "  an unknown state: inspect the failing unit before re-running, and do"
      say "  not repeat the command blindly." ;;
    5)
      say "exit 5 means work was in flight. Wait for the cluster to go idle"
      say "and run the same command again: the slot install is idempotent and"
      say "services already upgraded are verified without a restart." ;;
  esac
  exit "$rc"
}

# ---------------------------------------------------------------- 0. identity
[ "$(id -u)" -eq 0 ] || {
  echo "This driver must run as root. Invoke it with:"
  echo "  sudo bash $S6/s6-t2-execute.sh"
  exit 1
}

say "S6-t2 execution $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "candidate 5427ded3aa7668975e2cef2621e81b9131498281"
say "log       $LOG"
say ""

# ------------------------------------------------------- 1. verify artifacts
say "== 1/6 verifying every artifact this run will execute"
verify_one() {  # path expected-sha
  local actual
  [ -f "$1" ] || fail 1 "missing artifact: $1"
  actual=$(sha256sum "$1" | cut -d' ' -f1)
  [ "$actual" = "$2" ] || {
    say "   MISMATCH $1"
    say "     expected $2"
    say "     actual   $actual"
    fail 1 "artifact hash mismatch (nothing was executed)"
  }
  say "   OK $(basename "$1")"
}
verify_one "$S6/staging-5427ded/MANIFEST.sha256"   "$MANIFEST_SHA"
verify_one "$S6/staging-5427ded/install-slot.sh"   "$INSTALLER_SHA"
verify_one "$S6/activate-cluster-5427ded.sh"       "$ACTIVATE_SHA"
verify_one "$S6/rollback-cluster-5427ded.sh"       "$ROLLBACK_SHA"
verify_one "$S6/collect.py"                        "$COLLECT_SHA"
verify_one "$S6/liveness.py"                       "$LIVENESS_SHA"
verify_one "$S6/provenance.py"                     "$PROVENANCE_SHA"
say "   all seven artifacts verified"

# ----------------------------------------------------------- 2. ensure slot
say ""
say "== 2/6 ensuring the immutable slot (idempotent)"
if [ -e "$SLOT" ]; then
  say "   slot already present -> verifying the installed bytes and posture"
else
  say "   slot absent -> installing from the verified staging tree"
fi
if bash "$S6/staging-5427ded/install-slot.sh" ensure; then
  rc=0
else
  rc=$?
fi
[ "$rc" -eq 0 ] || fail "$rc" "slot install/verify refused"

# --------------------------------------------------------------- 3. dry run
say ""
say "== 3/6 dry run (writes nothing, restarts nothing)"
# The dry run still runs the global idle preflight, so it can return 5 for a
# busy cluster. Report its real code rather than collapsing every failure to 1,
# or the operator loses the "wait for idle and re-run" guidance.
if bash "$S6/activate-cluster-5427ded.sh" --dry-run; then
  rc=0
else
  rc=$?
fi
[ "$rc" -eq 0 ] || fail "$rc" "dry run failed"

# ------------------------------------------------------------- 4. roll out
say ""
say "== 4/6 rolling the six services from cb7fece"
say "   (global idle preflight runs first; a busy cluster exits 5 and"
say "    nothing is written)"
if bash "$S6/activate-cluster-5427ded.sh"; then
  rc=0
else
  rc=$?
fi
if [ "$rc" -ne 0 ]; then
  fail "$rc" "activation did not complete"
fi

# ------------------------------------------------ 5. boundary (owned by 4)
say ""
say "== 5/6 epoch boundary"
if [ -f "$BOUNDARY" ]; then
  say "   $(basename "$BOUNDARY") = $(cat "$BOUNDARY")"
  say "   (authoritative acceptance boundary for ${EPOCH##*/})"
else
  # The activation script writes this only after 6/6 verify. If we are here and
  # it is missing, the rollout did not reach its final step.
  fail 3 "activation returned 0 but $BOUNDARY is absent"
fi
if [ -f "$EPOCH/CAPABILITY" ]; then
  say "   capability record:"
  sed 's/^/     /' "$EPOCH/CAPABILITY" | tee -a "$LOG"
fi

# ----------------------------------------------------------- 6. reporting
say ""
say "== 6/6 read-only reports (as $REPORT_USER)"
# Snapshot the epoch (names, sizes, mtimes) so we can PROVE the reports did not
# write to telemetry or to the boundary marker.
snapshot() { find "$EPOCH" -printf '%p\t%s\t%T@\n' 2>/dev/null | LC_ALL=C sort; }
before=$(snapshot || true)
as_report_user() {
  if [ "$(id -un)" = "$REPORT_USER" ]; then
    "$@"
  else
    runuser -u "$REPORT_USER" -- "$@"
  fi
}
as_report_user "$PY" "$S6/collect.py" --epoch "$EPOCH" 2>&1 | tee -a "$LOG" \
  || say "   (collect.py exited non-zero; see above)"
say ""
as_report_user "$PY" "$S6/liveness.py" --epoch "$EPOCH" 2>&1 | tee -a "$LOG" \
  || say "   (liveness.py exited non-zero; see above)"
after=$(snapshot || true)

say ""
if [ "$before" = "$after" ]; then
  say "   READ-ONLY PROOF: the epoch tree is unchanged by the reports"
else
  say "   WARNING: the epoch tree changed while the reports ran:"
  changes=$(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") 2>&1 || true)
  printf '%s\n' "$changes" | head -20 | tee -a "$LOG"
  fail 3 "a read-only report modified telemetry"
fi

say ""
say "=============================================================="
say "S6-t2 rollover complete. Measurement is ACTIVE, NOT COMPLETE."
say "  gate: >=100 genuine unique admitted events AND >=48h from the boundary"
say "  boundary: $(cat "$BOUNDARY")"
say "  log: $LOG"
say "=============================================================="
status_block
exit 0
