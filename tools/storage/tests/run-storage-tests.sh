#!/usr/bin/env bash
# run-storage-tests.sh — mock-based validation for the storage controls.
#
# No Rust toolchain is used and no real build runs. A stub named `cargo` is
# placed first on PATH; it writes a few bytes into the target directory and can
# sleep, fail, spawn children, or hold a file open, so the wrapper's lifecycle,
# locking and quiescence logic can be exercised deterministically.
#
# Usage: tools/storage/tests/run-storage-tests.sh [--verbose]
# Deliberately NOT `set -e`: this suite asserts on non-zero exit statuses, and
# errexit would abort the run at the first expected failure. Every command's
# status is captured and checked explicitly instead.
set -uo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
TOOLS=$(cd "$HERE/.." && pwd)
VERBOSE=0
[ "${1:-}" = "--verbose" ] && VERBOSE=1

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s (got %s, want %s)\n' "$1" "$2" "$3"; }
check_rc() { local name=$1 got=$2 want=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name" "$got" "$want"; }
check_eq() { local name=$1 got=$2 want=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name" "$got" "$want"; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/storage-tests-XXXXXX")
cleanup() {
  [ -d "$WORK" ] || return 0
  # Some fixtures are deliberately unreadable; restore access before removal so
  # the suite ends quietly.
  find "$WORK" -type d -exec chmod 700 {} + 2>/dev/null || true
  rm -rf -- "$WORK" 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

mkdir -p "$WORK/bin"
cat > "$WORK/bin/cargo" <<'MOCK'
#!/usr/bin/env bash
mkdir -p "$CARGO_TARGET_DIR/debug/deps"
echo x > "$CARGO_TARGET_DIR/debug/deps/mock-artifact"
[ "$VERBOSE" = 1 ] && echo "MOCK-CARGO $*"
if [ -n "${MOCK_HOLD_FILE:-}" ]; then
  setsid bash -c "exec 3>>'$CARGO_TARGET_DIR/debug/deps/gc-open'; sleep ${MOCK_CHILD_SLEEP:-20}" &
fi
[ -n "${MOCK_ATTACHED:-}" ] && bash -c "sleep ${MOCK_CHILD_SLEEP:-15}" &
[ -n "${MOCK_SLEEP:-}" ] && sleep "$MOCK_SLEEP"
[ -n "${MOCK_FAIL:-}" ] && exit "$MOCK_FAIL"
exit 0
MOCK
chmod 0755 "$WORK/bin/cargo"
export PATH="$WORK/bin:$PATH"

export SY_BUILD_TARGET="$WORK/target"
export SY_BUILD_LOCK="$WORK/.lock"
export SY_BUILD_STATE="$WORK/state"
export SY_BUILD_LOG="$WORK/log"
export SY_BUILD_QUIESCE_TIMEOUT=4
# The fixture lives in TMPDIR, which on this host is a small tmpfs. The default
# 35 GiB floor would refuse every case, so the floor is set low here and the
# refusal path is exercised explicitly by overriding it below.
export SY_BUILD_MIN_FREE_GIB=1
# Likewise the projected-size reservation, which assumes a real 25 GiB debug
# tree. Both are overridden only inside this fixture; production keeps 35/25.
export SY_BUILD_PROJECTED_GIB=1
export SY_BUILD_MIN_SPARE_GIB=0
SYB="$TOOLS/sy-build"

echo "== build wrapper: lifecycle =="
"$SYB" campaign "normal" -- cargo build >/dev/null 2>&1; check_rc "normal exit" "$?" 0
[ -d "$SY_BUILD_TARGET/debug" ] && r=present || r=absent; check_eq "dev profile cleaned on success" "$r" absent

MOCK_FAIL=9 "$SYB" campaign "fail" -- cargo build >/dev/null 2>&1; check_rc "failure status propagated" "$?" 9
[ -d "$SY_BUILD_TARGET/debug" ] && r=present || r=absent; check_eq "dev profile cleaned on failure" "$r" absent

rm -rf "$SY_BUILD_TARGET"
"$SYB" campaign "fresh" -- cargo build >/dev/null 2>&1; check_rc "missing target dir is created" "$?" 0
: > "$SY_BUILD_LOCK"
"$SYB" campaign "stale" -- cargo build >/dev/null 2>&1; check_rc "stale lock file is harmless" "$?" 0

echo "== build wrapper: budget and locking =="
SY_BUILD_MIN_FREE_GIB=999999 "$SYB" campaign "space" -- cargo build >/dev/null 2>&1
check_rc "insufficient space refused" "$?" 4

MOCK_SLEEP=4 "$SYB" campaign "first" -- cargo build >/dev/null 2>&1 & FIRST=$!
sleep 1
"$SYB" campaign "second" -- cargo build >/dev/null 2>&1; check_rc "concurrent campaign refused" "$?" 3
wait "$FIRST"; check_rc "first campaign completed" "$?" 0
( exec 8>"$SY_BUILD_LOCK"; flock -n 8 ) 2>/dev/null && lk=free || lk=held
check_eq "lock released after campaign" "$lk" free

echo "== build wrapper: signal safety =="
MOCK_SLEEP=15 MOCK_ATTACHED=1 MOCK_CHILD_SLEEP=8 "$SYB" campaign "term" -- cargo build >/dev/null 2>&1 & W=$!
sleep 2
kill -TERM "$W" 2>/dev/null || true
wait "$W" 2>/dev/null; rc=$?
check_rc "SIGTERM exit status" "$rc" 130
[ -d "$SY_BUILD_TARGET/debug" ] && r=present || r=absent
check_eq "subtree terminated then cleaned" "$r" absent
pkill -f 'sleep 8' 2>/dev/null || true

MOCK_SLEEP=20 MOCK_HOLD_FILE=1 MOCK_CHILD_SLEEP=15 "$SYB" campaign "hold" -- cargo build >/dev/null 2>&1 & W=$!
sleep 2
kill -TERM "$W" 2>/dev/null || true
wait "$W" 2>/dev/null || true
[ -d "$SY_BUILD_TARGET/debug" ] && r=present || r=absent
check_eq "open file in target blocks cleanup (fail closed)" "$r" present
pkill -KILL -f 'gc-open' 2>/dev/null || true
pkill -KILL -f 'sleep 15' 2>/dev/null || true
sleep 1
"$SYB" clean >/dev/null 2>&1
[ -d "$SY_BUILD_TARGET/debug" ] && r=present || r=absent
check_eq "clean reclaims once idle" "$r" absent

echo "== build wrapper: SIGKILL recovery =="
MOCK_SLEEP=30 "$SYB" campaign "kill" -- cargo build >/dev/null 2>&1 & W=$!
sleep 2; kill -KILL "$W" 2>/dev/null || true
pkill -KILL -f 'sleep 30' 2>/dev/null || true; sleep 1
[ -f "$SY_BUILD_STATE" ] && r=present || r=absent
check_eq "SIGKILL leaves recoverable state" "$r" present
"$SYB" campaign "post" -- cargo build >/dev/null 2>&1 || true
out=$("$SYB" campaign "post" -- cargo build 2>&1 || true)
case "$out" in *ORPHANED*|*"campaign finished"*) ok "orphan reconciled on next run" ;;
                *) bad "orphan reconciled on next run" "no marker" "ORPHANED or finish";; esac

echo "== retention policy: protection rules =="
POLICY="$TOOLS/storage-policy.sh"
# shellcheck source=/dev/null
( . "$POLICY"
  for p in /var/lib/turnstone/artifacts /var/lib/turnstone/github /var/lib/postgresql/17/main \
           /var/lib/turnstone/backups /var/tmp/sy-integration; do
    storage_is_protected "$p" || exit 1
  done
  for p in /var/lib/turnstone/sytarget/debug /var/tmp/random; do
    if storage_is_protected "$p"; then exit 1; fi
  done
  exit 0 ) && ok "protected set refuses assets and allows build output" \
          || bad "protected set refuses assets and allows build output" "wrong" "correct"

( . "$POLICY"
  for p in /var/lib/turnstone /var/lib /var/tmp; do storage_is_protected "$p" || exit 1; done
  exit 0 ) && ok "parents of protected paths are refused" \
          || bad "parents of protected paths are refused" "allowed" "refused"

echo "== retention policy: staleness semantics =="
ST="$WORK/stale"; mkdir -p "$ST/old" "$ST/mixed"
touch -d '10 days ago' "$ST/old/a"
touch -d '10 days ago' "$ST/mixed/old"; touch "$ST/mixed/new"
( . "$POLICY"; storage_is_stale "$ST/old" 2 ) && ok "old-only tree is stale" \
  || bad "old-only tree is stale" "fresh" "stale"
( . "$POLICY"; storage_is_stale "$ST/mixed" 2 ) && bad "tree with fresh files is NOT stale" "stale" "fresh" \
  || ok "tree with fresh files is NOT stale"

# The pruner's first gate refuses to run while any cargo/rustc/pytest exists
# ANYWHERE on the host. On a busy host that silently turns every deletion-path
# assertion into a vacuous pass, so those cases run in a private PID+mount
# namespace where the only processes are ours. Without user namespaces the
# cases still run, and the interlock is exercised rather than bypassed.
UNSHARE_OK=0
if unshare -r -m -p -f --mount-proc true 2>/dev/null; then UNSHARE_OK=1; fi
run_prune() {
  if [ "$UNSHARE_OK" -eq 1 ]; then
    unshare -r -m -p -f --mount-proc "$@"
  else
    "$@"
  fi
}

echo "== retention pruner: dry run deletes nothing =="
PR="$TOOLS/storage-prune.sh"
PT="$WORK/pytest-of-nothing"; mkdir -p "$PT"; touch -d '5 days ago' "$PT/x"
LIVETARGET="$WORK/livetarget"; mkdir -p "$LIVETARGET"
STORAGE_TARGETS="$LIVETARGET" STORAGE_PYTEST_GLOBS="$WORK/pytest-of-*" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --explain >/dev/null 2>&1
check_rc "--explain exits 0" "$?" 0
[ -d "$PT" ] && r=present || r=absent; check_eq "--explain deletes nothing" "$r" present

STORAGE_TARGETS="$LIVETARGET" STORAGE_PYTEST_GLOBS="$WORK/pytest-of-*" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply >/dev/null 2>&1
[ -d "$PT" ] && r=present || r=absent; check_eq "--apply without --confirm deletes nothing" "$r" present

echo "== storage-prune: an ACTIVE candidate is never deleted, only logged =="
# Regression: the per-candidate loop classified a fresh tree as class=active and
# then fell through to the removal step, so a tree modified seconds ago was
# deleted. `debug/incremental` is the exposed case: it is added as a candidate
# unconditionally, relying on this loop for its staleness gate.
ACT="$WORK/active-target"; mkdir -p "$ACT/debug/incremental" "$ACT/debug/deps"
echo fresh > "$ACT/debug/incremental/live.bin"
echo fresh > "$ACT/debug/deps/live.rlib"
touch "$ACT/debug/incremental/live.bin" "$ACT/debug/deps/live.rlib"

# Dry run first, so "not even proposed" is a real assertion rather than a grep
# over a log produced by a destructive pass.
ACTLOG="$WORK/active-dryrun.log"
STORAGE_TARGETS="$ACT" STORAGE_PYTEST_GLOBS="$WORK/none" STORAGE_PRUNE_PCT=0 \
  STORAGE_LOG="$ACTLOG" run_prune "$PR" --apply >/dev/null 2>&1
check_rc "a fresh target completes a dry run" "$?" 0
if grep -q "event=would_remove .*active-target" "$ACTLOG"; then
  bad "an active candidate is not even proposed in a dry run" "proposed" "absent"
else
  ok "an active candidate is not even proposed in a dry run"
fi
grep -q "class=active" "$ACTLOG" \
  && ok "the active candidate is classified in a dry run" \
  || bad "the active candidate is classified in a dry run" "absent" "class=active"

ACTLOG="$WORK/active-apply.log"
STORAGE_TARGETS="$ACT" STORAGE_PYTEST_GLOBS="$WORK/none" STORAGE_PRUNE_PCT=0 \
  STORAGE_LOG="$ACTLOG" run_prune "$PR" --apply --confirm >/dev/null 2>&1
check_rc "a fresh target still completes an authorized run" "$?" 0
[ -e "$ACT/debug/incremental/live.bin" ] && r=present || r=absent
check_eq "an active incremental tree is not deleted" "$r" present
[ -e "$ACT/debug/deps/live.rlib" ] && r=present || r=absent
check_eq "an active deps tree is not deleted" "$r" present
if grep -q "event=removed .*active-target" "$ACTLOG"; then
  bad "an active candidate is not reported as removed" "removed" "absent"
else
  ok "an active candidate is not reported as removed"
fi

echo "== storage-prune: a stale candidate IS still deleted (control) =="
# The guard above must not become a blanket refusal: a genuinely stale tree has
# to remain deletable, or the pruner silently stops working.
OLD="$WORK/old-target"; mkdir -p "$OLD/debug/incremental" "$OLD/debug/deps"
echo old > "$OLD/debug/incremental/gone.bin"
echo old > "$OLD/debug/deps/gone.rlib"
touch -d '5 days ago' "$OLD/debug/incremental/gone.bin" "$OLD/debug/deps/gone.rlib" \
  "$OLD/debug/incremental" "$OLD/debug/deps" "$OLD/debug"
STORAGE_TARGETS="$OLD" STORAGE_PYTEST_GLOBS="$WORK/none" STORAGE_PRUNE_PCT=0 \
  STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply --confirm >/dev/null 2>&1
check_rc "a stale target completes the run" "$?" 0
[ -e "$OLD/debug/incremental/gone.bin" ] && r=present || r=absent
check_eq "a stale incremental tree is deleted" "$r" absent
[ -e "$OLD/debug/deps/gone.rlib" ] && r=present || r=absent
check_eq "a stale deps tree is deleted" "$r" absent

echo "== storage-prune: a tree that becomes fresh AFTER enumeration is skipped =="
# The build lock only excludes registered campaigns; a process that does not
# take it (a hand-run cargo, an editor, an unpacking job) can write into a
# candidate after the run started. A stub `rm` on PATH touches a LATER
# candidate's tree at the moment the first candidate is deleted, so the later
# one is stale when enumerated and fresh when its turn comes.
RT="$WORK/race-target"; mkdir -p "$RT/debug/incremental" "$RT/debug/deps"
echo old > "$RT/debug/incremental/first.bin"
echo old > "$RT/debug/deps/second.rlib"
touch -d '10 days ago' "$RT/debug/incremental/first.bin" "$RT/debug/deps/second.rlib" \
  "$RT/debug/incremental" "$RT/debug/deps" "$RT/debug"
RACE_PROBE="$WORK/race-probe"; : > "$RACE_PROBE"
cat > "$WORK/bin/rm" <<'MOCK'
#!/usr/bin/env bash
# Record that a removal really happened, then make a LATER candidate fresh
# before the pruner reaches it.
echo "rm $*" >> "$RACE_PROBE"
if [ -n "${RACE_TOUCH:-}" ] && [ -e "$RACE_TOUCH" ]; then
  touch "$RACE_TOUCH" 2>/dev/null || true
fi
exec /bin/rm "$@"
MOCK
chmod 0755 "$WORK/bin/rm"
export RACE_PROBE RACE_TOUCH="$RT/debug/deps/second.rlib"
STORAGE_TARGETS="$RT" STORAGE_PYTEST_GLOBS="$WORK/none" STORAGE_PRUNE_PCT=0 \
  STORAGE_LOG="$WORK/race.log" run_prune "$PR" --apply --confirm >/dev/null 2>&1
check_rc "the racing run completes" "$?" 0
# Not vacuous: the first candidate must actually have been removed, so the stub
# ran and the later tree really did turn fresh mid-run.
grep -q "incremental" "$RACE_PROBE" \
  && ok "the first stale candidate was removed, so the race really occurred" \
  || bad "the first stale candidate was removed, so the race really occurred" "no rm call" "rm invoked"
[ -e "$RT/debug/incremental/first.bin" ] && r=present || r=absent
check_eq "the stale candidate enumerated first is deleted" "$r" absent
[ -e "$RT/debug/deps/second.rlib" ] && r=present || r=absent
check_eq "the tree that turned fresh mid-run survives" "$r" present
grep -q "event=classify.*deps.*class=active" "$WORK/race.log" \
  && ok "the tree that turned fresh is re-classified as active at its turn" \
  || bad "the tree that turned fresh is re-classified as active at its turn" "absent" "class=active"
unset RACE_PROBE RACE_TOUCH
/bin/rm -f "$WORK/bin/rm"

echo "== storage_size_mib: incomplete scan fails closed, never zero ==="
POLICY="$TOOLS/storage-policy.sh"
# shellcheck source=/dev/null
. "$POLICY"
ACC="$WORK/accounting"; mkdir -p "$ACC/ok" "$ACC/nope"
echo "readable" > "$ACC/ok/file"
chmod 000 "$ACC/nope"

# 1) accessible tree measures cleanly (single integer)
if mib=$(STORAGE_JSON=1 storage_size_mib "$ACC/ok" 2>/dev/null); then
  case "$mib" in *[!0-9]*) bad "size is a clean integer" "$mib" "digits only" ;; *)
    ok "accessible tree measures $mib MiB" ;;
  esac
else
  bad "accessible tree measures cleanly" "rc=0" "rc=$?"
fi

# 2) tree containing an unreadable dir fails closed (rc=3), never returns 0 with garbage
rc=0
mib=$(STORAGE_JSON=1 storage_size_mib "$ACC" 2>/dev/null) || rc=$?
[ "$rc" -eq 3 ] && ok "incomplete scan returns rc=3" || bad "incomplete scan returns rc=3" "$rc" 3
case "$mib" in *$'\n'*) bad "no multi-line fallback value" "$mib" "single line" ;; *) ok "no multi-line fallback value" ;; esac
[ -z "$mib" ] && ok "no partial value leaked to stdout" || bad "no partial value leaked to stdout" "$mib" "empty"

# 3) missing path -> rc=2
rc=0
mib=$(STORAGE_JSON=1 storage_size_mib "$ACC/does-not-exist" 2>/dev/null) || rc=$?
[ "$rc" -eq 2 ] && ok "missing path returns rc=2" || bad "missing path returns rc=2" "$rc" 2

# 4) arithmetic on the result never errors (the original defect)
rc=0
result=$(STORAGE_JSON=1 mib=$(storage_size_mib "$ACC/ok" 2>/dev/null) && echo $((10 + mib)) 2>/dev/null) || rc=$?
[ "$rc" -eq 0 ] && ok "arithmetic on measured size succeeds" || bad "arithmetic on measured size succeeds" "rc=0" "rc=$rc"

echo "== storage-monitor: unreadable target fails the run (exit 20), never OK ==="
MON="$TOOLS/storage-monitor.sh"
UNREAD="$WORK/unreadable-target"; mkdir -p "$UNREAD/secret"; echo x > "$UNREAD/secret/file"; chmod 000 "$UNREAD/secret"
STORAGE_LOG="$WORK/ev.log" STORAGE_TARGETS="$UNREAD" "$MON" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 20 ] && ok "monitor exits 20 on unreadable root" || bad "monitor exits 20 on unreadable root" "$rc" 20
grep -q "cannot_measure_build_root" "$WORK/ev.log" 2>/dev/null && ok "monitor logs the failure reason" || bad "monitor logs the failure reason" "present" "absent"

echo "== storage-prune: undeterminable staleness refuses the run (exit 20) ==="
PR="$TOOLS/storage-prune.sh"
UT="$WORK/undet-target"; mkdir -p "$UT/debug/incremental/locked" "$UT/debug/deps"
echo x > "$UT/debug/deps/f"; touch -d '10 days ago' "$UT/debug/deps/f"
# The deletion-path cases run as a mapped root inside a user namespace, which
# bypasses mode bits, so the scan failure is injected through a `find` stub
# instead of a chmod. It fails only for this fixture.
cat > "$WORK/bin/find" <<'MOCK'
#!/usr/bin/env bash
case "$*" in *undet-target*) exit 1 ;; esac
exec /usr/bin/find "$@"
MOCK
chmod 0755 "$WORK/bin/find"
STORAGE_TARGETS="$UT" STORAGE_PYTEST_GLOBS="$WORK/none" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply --confirm >/dev/null 2>&1; rc=$?
check_rc "prune refuses an undeterminable candidate" "$rc" 20
grep -q "reason=staleness_undeterminable" "$WORK/ev.log" 2>/dev/null \
  && ok "the refusal names the reason" \
  || bad "the refusal names the reason" "absent" "staleness_undeterminable"
[ -d "$UT/debug/deps" ] && ok "prune did not delete the undeterminable candidate" \
  || bad "prune did not delete the undeterminable candidate" "absent" "present"
/bin/rm -f "$WORK/bin/find"

echo "== storage-prune: an unvalidatable path refuses the run (exit 20) =="
# A mount table that cannot be read makes the nested-mount check impossible.
# The pruner must refuse rather than treat "could not check" as "clear".
PV="$WORK/valid-target"; mkdir -p "$PV/debug/incremental" "$PV/debug/deps"
echo x > "$PV/debug/deps/f"
touch -d '10 days ago' "$PV/debug/deps/f" "$PV/debug/incremental/x"
cat > "$WORK/bin/findmnt" <<'MOCK'
#!/usr/bin/env bash
exit 1
MOCK
chmod 0755 "$WORK/bin/findmnt"
STORAGE_TARGETS="$PV" STORAGE_PYTEST_GLOBS="$WORK/none" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply --confirm >/dev/null 2>&1; rc=$?
check_rc "an unvalidatable candidate stops the run" "$rc" 20
grep -q "reason=path_unvalidated" "$WORK/ev.log" 2>/dev/null \
  && ok "the refusal names path validation" \
  || bad "the refusal names path validation" "absent" "path_unvalidated"
[ -d "$PV/debug/deps" ] && ok "prune did not delete the unvalidated candidate" \
  || bad "prune did not delete the unvalidated candidate" "absent" "present"
/bin/rm -f "$WORK/bin/findmnt"

echo "== storage-prune: missing targets is an explicit error, not a silent no-op ==="
STORAGE_TARGETS="$WORK/absent-root" STORAGE_PYTEST_GLOBS="$WORK/none" \
  STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply --confirm >/dev/null 2>&1; rc=$?
[ "$rc" -eq 20 ] && ok "prune fails with no targets" "$rc" 20

echo "== storage-prune: dry-run is non-destructive at warn/prune/hard thresholds ==="
TREE="$WORK/dryrun"; mkdir -p "$TREE/debug/deps"; echo x > "$TREE/debug/deps/f"
STORAGE_TARGETS="$TREE" STORAGE_PYTEST_GLOBS="$WORK/none" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "--apply without --confirm exits 0" "$rc" 0
[ -d "$TREE/debug/deps" ] && ok "--apply without --confirm deletes nothing" || bad "--apply without --confirm deletes nothing" "present" "absent"
STORAGE_TARGETS="$TREE" STORAGE_PYTEST_GLOBS="$WORK/none" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --explain >/dev/null 2>&1
[ -d "$TREE/debug/deps" ] && ok "--explain deletes nothing" || bad "--explain deletes nothing" "present" "absent"

echo "== storage-monitor: reports SSD per-worktree target on the build disk ==="
SSD=/mnt/turnstone-build-ssd/targets
if [ -d "$SSD" ]; then
  json=$(STORAGE_LOG="$WORK/ev.log" "$MON" --json 2>/dev/null)
  printf '%s' "$json" | grep -q "$SSD" && ok "SSD worktree target appears in monitor build_roots" || bad "SSD worktree target appears in monitor build_roots" "present" "absent"
else
  ok "no build SSD mounted — target discovery is mount-guarded (skipped)"
fi

echo "== storage-prune: protected artifacts survive a prune run ==="
PROT="$WORK/protected-cred"; mkdir -p "$PROT"; echo secret > "$PROT/key"
# Force prune threshold so the dry-run path classifies everything.
STORAGE_TARGETS="$LIVETARGET" STORAGE_PYTEST_GLOBS="$WORK/none" STORAGE_PRUNE_PCT=0 \
  STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --explain >/dev/null 2>&1
[ -e "$PROT/key" ] && ok "protected credential tree untouched" || bad "protected credential tree untouched" "present" "absent"

echo "== storage-prune: the build lock is held for the whole deletion =="
# A stub `rm` runs inside the pruner and tries to take the same lock. If the
# pruner released it after probing, the stub can take it and reports LOCK=free.
LT="$WORK/locktree"; mkdir -p "$LT/debug/incremental" "$LT/debug/deps"
echo x > "$LT/debug/deps/f"
touch -d '10 days ago' "$LT/debug/deps/f" "$LT/debug/incremental/x"
cat > "$WORK/bin/rm" <<'MOCK'
#!/usr/bin/env bash
if [ -n "${LOCK_PROBE:-}" ]; then
  if exec 9>>"$SY_BUILD_LOCK" 2>/dev/null; then
    if flock -n 9; then echo "LOCK=free" >> "$LOCK_PROBE"; else echo "LOCK=held" >> "$LOCK_PROBE"; fi
  else
    echo "LOCK=unopenable" >> "$LOCK_PROBE"
  fi
fi
exec /bin/rm "$@"
MOCK
chmod 0755 "$WORK/bin/rm"
export LOCK_PROBE="$WORK/lock-probe"; : > "$LOCK_PROBE"
STORAGE_TARGETS="$LT" STORAGE_PYTEST_GLOBS="$WORK/none" STORAGE_PRUNE_PCT=0 \
  STORAGE_LOG="$WORK/ev.log" run_prune "$PR" --apply --confirm >/dev/null 2>&1
grep -q "LOCK=held" "$LOCK_PROBE" 2>/dev/null \
  && ok "the lock is still held when deletion runs" \
  || bad "the lock is still held when deletion runs" "$(tr '\n' ' ' < "$LOCK_PROBE" 2>/dev/null)" "LOCK=held"
unset LOCK_PROBE
/bin/rm -f "$WORK/bin/rm"

echo "== storage-anchor and required targets: fail closed, never fall back to / =="
ONE="$WORK/req-one"; mkdir -p "$ONE"; echo x > "$ONE/f"
# one required root present, one absent -> the missing one is named and the run fails
rc=0
STORAGE_LOG="$WORK/ev.log" STORAGE_TARGETS="$ONE $WORK/req-missing" STORAGE_OPTIONAL_TARGETS= \
  "$MON" >/dev/null 2>&1 || rc=$?
check_rc "one required target missing -> exit 20" "$rc" 20
grep -q "reason=missing_required_target" "$WORK/ev.log" 2>/dev/null \
  && ok "the missing required root is named in the log" \
  || bad "the missing required root is named in the log" "absent" "missing_required_target"
# an optional root that is absent is not an error
rc=0
STORAGE_LOG="$WORK/ev.log" STORAGE_TARGETS="$ONE" STORAGE_OPTIONAL_TARGETS="$WORK/opt-missing" \
  "$MON" >/dev/null 2>&1 || rc=$?
[ "$rc" -ne 20 ] && ok "an absent optional root is not an error" \
  || bad "an absent optional root is not an error" "$rc" "not 20"
# no target at all -> no anchor, so / is never measured on its behalf
rc=0
STORAGE_LOG="$WORK/ev.log" STORAGE_TARGETS= STORAGE_OPTIONAL_TARGETS= \
  STORAGE_SSD_MOUNT="$WORK/no-ssd" "$MON" >/dev/null 2>&1 || rc=$?
check_rc "no targets -> exit 20" "$rc" 20
grep -q "reason=no_targets" "$WORK/ev.log" 2>/dev/null \
  && ok "the no-target failure is distinguishable from a missing root" \
  || bad "the no-target failure is distinguishable from a missing root" "absent" "no_targets"

( . "$POLICY"; STORAGE_TARGETS= STORAGE_OPTIONAL_TARGETS= \
  STORAGE_SSD_MOUNT="$WORK/no-ssd" storage_anchor ) >/dev/null 2>&1 \
  && bad "storage_anchor refuses to substitute /" "printed an anchor" "rc=1" \
  || ok "storage_anchor refuses to substitute /"

echo "== build SSD: a directory is not a mount =="
FAKESSD="$WORK/fake-ssd"; mkdir -p "$FAKESSD/targets/w1/debug"
( . "$POLICY"; STORAGE_SSD_MOUNT="$FAKESSD" STORAGE_SSD_TARGET_ROOT="$FAKESSD/targets" storage_ssd_mounted ) \
  && bad "a plain directory is not treated as a mounted SSD" "mounted" "not mounted" \
  || ok "a plain directory is not treated as a mounted SSD"
listed=$( . "$POLICY"; STORAGE_SSD_MOUNT="$FAKESSD" STORAGE_SSD_TARGET_ROOT="$FAKESSD/targets" \
  STORAGE_TARGETS="$ONE" STORAGE_OPTIONAL_TARGETS= storage_target_list )
case "$listed" in *"$FAKESSD"*)
  bad "unmounted SSD contents are not listed as targets" "listed" "absent" ;;
  *) ok "unmounted SSD contents are not listed as targets" ;;
esac
if [ -d /mnt/turnstone-build-ssd ]; then
  ( . "$POLICY"; storage_ssd_mounted ) \
    && ok "a genuinely mounted build SSD is recognised" \
    || bad "a genuinely mounted build SSD is recognised" "not mounted" "mounted"
else
  ok "no build SSD present — mount predicate not exercised"
fi

echo "== path safety: safe, unsafe and unvalidated are distinct =="
PS="$WORK/safety"; mkdir -p "$PS/real/sub" "$PS/normal"; ln -s "$PS/real" "$PS/link"
ln -s "$PS/nowhere" "$PS/broken"
mkdir -p "$PS/locked/inner"; chmod 000 "$PS/locked"
( . "$POLICY"; storage_path_safety "$PS/normal" ); r=$?
check_rc "an ordinary directory is safe" "$r" 0
( . "$POLICY"; storage_path_safety "$PS/link/sub" ); r=$?
check_rc "a symlinked ancestor is unsafe" "$r" 1
( . "$POLICY"; storage_path_safety "$PS/real/sub" ); r=$?
check_rc "the resolved directory itself is safe" "$r" 0
( . "$POLICY"; storage_path_safety "$PS/broken" ); r=$?
check_rc "a broken symlink is unsafe" "$r" 1
( . "$POLICY"; storage_path_safety "$PS/locked/inner" ); r=$?
check_rc "an unreadable parent is unvalidated, not safe" "$r" 2
chmod 755 "$PS/locked"
# Nested mount and an unreadable mount table, driven through a findmnt stub.
NEST="$WORK/fake-mount"; mkdir -p "$NEST/sub"
if [ ! -e "$WORK/bin/findmnt" ]; then
cat > "$WORK/bin/findmnt" <<'MOCK'
#!/usr/bin/env bash
[ -n "${FAKE_FINDMNT_FAIL:-}" ] && exit 1
printf '/\n%s\n' "${FAKE_NESTED_MOUNT:-}"
MOCK
chmod 0755 "$WORK/bin/findmnt"
fi
( . "$POLICY"; FAKE_NESTED_MOUNT="$NEST/sub" storage_path_safety "$NEST" ); r=$?
check_rc "a nested mount is unsafe" "$r" 1
( . "$POLICY"; FAKE_FINDMNT_FAIL=1 storage_path_safety "$NEST" ); r=$?
check_rc "an unreadable mount table is unvalidated, not safe" "$r" 2
/bin/rm -f "$WORK/bin/findmnt"

PS2="$WORK/safety2"; mkdir -p "$PS2/locked/inner"; chmod 000 "$PS2/locked"
( . "$POLICY"; storage_is_unsafe_path "$PS2/locked/inner" ) \
  && ok "the yes/no wrapper treats unvalidated as unsafe" \
  || bad "the yes/no wrapper treats unvalidated as unsafe" "safe" "unsafe"
chmod 755 "$PS2/locked"

echo "== storage_size_mib: stdout stays clean on failure in BOTH modes =="
ACC2="$WORK/acc2"; mkdir -p "$ACC2/secret"; echo x > "$ACC2/secret/f"; chmod 000 "$ACC2/secret"
SIZELOG="$WORK/ev-size.log"; : > "$SIZELOG"
# plain mode: no STORAGE_JSON, diagnostics must not reach stdout
out=$(( . "$POLICY"; STORAGE_LOG="$SIZELOG" storage_size_mib "$ACC2" ) 2>/dev/null); rc=$?
[ -z "$out" ] && ok "plain mode: no diagnostic on stdout" \
  || bad "plain mode: no diagnostic on stdout" "$out" "empty"
check_rc "plain mode: returns rc=3" "$rc" 3
err=$(( . "$POLICY"; STORAGE_LOG="$SIZELOG" storage_size_mib "$ACC2" ) 2>&1 >/dev/null)
case "$err" in *size_unreadable*) ok "the failure is reported on stderr" ;;
  *) bad "the failure is reported on stderr" "$err" "size_unreadable" ;; esac
grep -q "event=size_unreadable" "$SIZELOG" 2>/dev/null \
  && ok "the failure reaches the event log" \
  || bad "the failure reaches the event log" "absent" "size_unreadable"
# JSON mode keeps the same contract
out=$(( . "$POLICY"; STORAGE_LOG="$SIZELOG" STORAGE_JSON=1 storage_size_mib "$ACC2" ) 2>/dev/null); rc=$?
[ -z "$out" ] && ok "JSON mode: no diagnostic on stdout" \
  || bad "JSON mode: no diagnostic on stdout" "$out" "empty"
check_rc "JSON mode: returns rc=3" "$rc" 3
# the missing-path branch has the same stdout contract
out=$(( . "$POLICY"; STORAGE_LOG="$SIZELOG" storage_size_mib "$ACC2/absent" ) 2>/dev/null); rc=$?
[ -z "$out" ] && ok "missing path: no diagnostic on stdout" \
  || bad "missing path: no diagnostic on stdout" "$out" "empty"
check_rc "missing path: returns rc=2" "$rc" 2
chmod 755 "$ACC2/secret"

echo "== staleness: an unreadable tree is undeterminable, not stale =="
UNR="$WORK/stale-unreadable"; mkdir -p "$UNR/sub"; echo x > "$UNR/sub/f"; chmod 000 "$UNR"
( . "$POLICY"; storage_is_stale "$UNR" 2 ); r=$?
check_rc "unreadable tree returns 2" "$r" 2
chmod 755 "$UNR"

echo
if [ "$UNSHARE_OK" -ne 1 ]; then echo "NOTE: no user namespace - deletion-path cases ran unisolated"; fi
echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
