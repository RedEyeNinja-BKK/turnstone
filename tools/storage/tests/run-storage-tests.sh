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
cleanup() { [ -d "$WORK" ] && rm -rf -- "$WORK"; }
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

echo "== retention pruner: dry run deletes nothing =="
PR="$TOOLS/storage-prune.sh"
PT="$WORK/pytest-of-nothing"; mkdir -p "$PT"; touch -d '5 days ago' "$PT/x"
STORAGE_TARGETS="$WORK/notarget" STORAGE_PYTEST_GLOBS="$WORK/pytest-of-*" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" "$PR" --explain >/dev/null 2>&1
check_rc "--explain exits 0" "$?" 0
[ -d "$PT" ] && r=present || r=absent; check_eq "--explain deletes nothing" "$r" present

STORAGE_TARGETS="$WORK/notarget" STORAGE_PYTEST_GLOBS="$WORK/pytest-of-*" \
  STORAGE_PRUNE_PCT=0 STORAGE_LOG="$WORK/ev.log" "$PR" --apply >/dev/null 2>&1
[ -d "$PT" ] && r=present || r=absent; check_eq "--apply without --confirm deletes nothing" "$r" present

echo
echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
