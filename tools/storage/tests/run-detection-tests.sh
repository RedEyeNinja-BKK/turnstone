#!/usr/bin/env bash
# run-detection-tests.sh — validation for storage-detect.sh and storage-daily.sh.
#
# These tools are read-only, so every test asserts on reported findings and on
# the absence of side effects. No Rust toolchain is used: a stub named `cargo`
# simulates a bypassing build.
#
# Deliberately NOT `set -e`: the suite asserts on non-zero exit statuses.
set -uo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
TOOLS=$(cd "$HERE/.." && pwd)
DETECT="$TOOLS/storage-detect.sh"
DAILY="$TOOLS/storage-daily.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s (got %s, want %s)\n' "$1" "$2" "$3"; }
ck()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/detect-tests-XXXXXX")
cleanup() { [ -d "$WORK" ] && rm -rf -- "$WORK"; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

export STORAGE_LOG="$WORK/events.log"
export STORAGE_SAMPLE_FILE="$WORK/sample"
export SY_BUILD_TARGET="$WORK/canonical"
export SY_BUILD_LOCK="$WORK/lock"

echo "== detection: clean baseline =="
"$DETECT" >/dev/null 2>&1; ck "clean host exits 0" "$?" 0

echo "== detection: in-tree target is reported =="
ROOT="$WORK/srcrepo"; mkdir -p "$ROOT/target/debug/deps"
printf 'x' > "$ROOT/target/debug/deps/f"
# point the scan at the fixture via a temporary policy override
( export STORAGE_SCAN_ROOTS_OVERRIDE="$ROOT"
  STORAGE_TARGETS="$SY_BUILD_TARGET" "$DETECT" >/dev/null 2>&1 )
out=$(STORAGE_SCAN_ROOTS_OVERRIDE="$ROOT" STORAGE_TARGETS="$SY_BUILD_TARGET" "$DETECT" 2>&1)
case "$out" in *in_tree_target*) ok "in-tree target detected" ;;
                *) bad "in-tree target detected" "absent" "present" ;; esac

echo "== detection: retained release is not called a bypass =="
REL="$WORK/canonical"; mkdir -p "$REL/release"
out=$(STORAGE_TARGETS="$REL" "$DETECT" 2>&1)
case "$out" in *retained_release*) ok "retained release classified separately" ;;
                *) bad "retained release classified separately" "absent" "present" ;; esac

echo "== detection: cargo with a non-canonical target =="
mkdir -p "$WORK/bin"; cp /bin/sleep "$WORK/bin/cargo" 2>/dev/null || cp "$(command -v sleep)" "$WORK/bin/cargo"
chmod 0755 "$WORK/bin/cargo"
CARGO_TARGET_DIR="$WORK/elsewhere" "$WORK/bin/cargo" 15 &
BYPASS=$!
sleep 1
out=$(STORAGE_TARGETS="$SY_BUILD_TARGET" "$DETECT" 2>&1)
case "$out" in *non_canonical_target*) ok "non-canonical CARGO_TARGET_DIR detected" ;;
                *) bad "non-canonical CARGO_TARGET_DIR detected" "absent" "present" ;; esac
kill "$BYPASS" 2>/dev/null; sleep 1; wait "$BYPASS" 2>/dev/null

echo "== detection: two concurrent cargo processes =="
"$WORK/bin/cargo" 15 & C1=$!
"$WORK/bin/cargo" 15 & C2=$!
sleep 1
STORAGE_TARGETS="$SY_BUILD_TARGET" "$DETECT" >/dev/null 2>&1
ck "concurrent campaigns reported (exit 11)" "$?" 11
kill "$C1" "$C2" 2>/dev/null; sleep 1; wait 2>/dev/null

echo "== detection: growth sample =="
printf '%s %s\n' 1000 "$(date -u +%s)" > "$STORAGE_SAMPLE_FILE"
out=$(STORAGE_TARGETS="$SY_BUILD_TARGET" "$DETECT" 2>&1)
case "$out" in *growth*) ok "growth sample compared" ;;
                *) bad "growth sample compared" "absent" "present" ;; esac
[ -f "$STORAGE_SAMPLE_FILE" ] && ok "sample file rewritten" || bad "sample file rewritten" absent present

echo "== daily summary: schema and read-only behaviour =="
: > "$STORAGE_LOG"
printf '2026-10-23T00:00:00Z level=info event=monitor used_pct=25\n' >> "$STORAGE_LOG"
printf '2026-10-23T00:00:00Z level=info event=would_remove path=/x kind=pytest mib=100\n' >> "$STORAGE_LOG"
out=$(STORAGE_LOG="$STORAGE_LOG" STORAGE_MONITOR_LOG="$STORAGE_LOG" "$DAILY" --json 2>/dev/null)
printf '%s' "$out" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null \
  && ok "daily --json parses" || bad "daily --json parses" invalid valid

n=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["proposed_candidates"])' 2>/dev/null)
ck "daily counts one candidate" "$n" 1
m=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["proposed_mib"])' 2>/dev/null)
ck "daily reports candidate size once" "$m" 100

echo "== daily summary: repeated runs do not inflate the estimate =="
printf '2026-10-23T01:00:00Z level=info event=would_remove path=/x kind=pytest mib=100\n' >> "$STORAGE_LOG"
printf '2026-10-23T02:00:00Z level=info event=would_remove path=/x kind=pytest mib=100\n' >> "$STORAGE_LOG"
m=$(STORAGE_LOG="$STORAGE_LOG" STORAGE_MONITOR_LOG="$STORAGE_LOG" "$DAILY" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["proposed_mib"])' 2>/dev/null)
ck "repeated proposals counted once" "$m" 100

echo "== alerts: storm suppression and delivery =="
ALERT="$TOOLS/storage-alert.sh"
export STORAGE_ALERT_STATE="$WORK/alert.state" STORAGE_ALERT_QUEUE="$WORK/alert.queue"
: > "$STORAGE_ALERT_QUEUE"
"$ALERT" >/dev/null 2>&1
n=$(grep -c 'turnstone storage' "$STORAGE_ALERT_QUEUE" 2>/dev/null || true)
ck "baseline run is silent" "$n" 0

STORAGE_WARN_PCT=20 "$ALERT" >/dev/null 2>&1
n=$(grep -c 'turnstone storage' "$STORAGE_ALERT_QUEUE" 2>/dev/null || true)
ck "entering WARN alerts once" "$n" 1

STORAGE_WARN_PCT=20 "$ALERT" >/dev/null 2>&1
n=$(grep -c 'turnstone storage' "$STORAGE_ALERT_QUEUE" 2>/dev/null || true)
ck "repeated WARN does not re-alert" "$n" 1

STORAGE_WARN_PCT=20 STORAGE_ALERT_REMIND_HOURS=0 "$ALERT" >/dev/null 2>&1
n=$(grep -c 'turnstone storage' "$STORAGE_ALERT_QUEUE" 2>/dev/null || true)
ck "reminder fires when due" "$n" 2

"$ALERT" >/dev/null 2>&1
n=$(grep -c 'turnstone storage' "$STORAGE_ALERT_QUEUE" 2>/dev/null || true)
ck "recovery alerts once" "$n" 3

: > "$STORAGE_ALERT_QUEUE"; rm -f "$STORAGE_ALERT_STATE"
"$ALERT" >/dev/null 2>&1
DELIVERED="$WORK/delivered.txt"; : > "$DELIVERED"
STORAGE_ALERT_CMD="tee -a $DELIVERED" STORAGE_WARN_PCT=20 "$ALERT" >/dev/null 2>&1
n=$(grep -c 'turnstone storage' "$DELIVERED" 2>/dev/null || true)
q=$(grep -c 'turnstone storage' "$STORAGE_ALERT_QUEUE" 2>/dev/null || true)
ck "working dispatcher delivers" "$n" 1
ck "delivered alert is not also queued" "$q" 0

echo "== sampler: 15-minute capacity sampling =="
SM="$TOOLS/storage-sample.sh"
export STORAGE_SAMPLE_LOG="$WORK/samples.log" STORAGE_SAMPLE_STATE="$WORK/sample.state"
: > "$STORAGE_SAMPLE_LOG"; rm -f "$STORAGE_SAMPLE_STATE"

"$SM" >/dev/null 2>&1; ck "sampler exits 0 when healthy" "$?" 0
n=$(grep -c 'used_pct=' "$STORAGE_SAMPLE_LOG" 2>/dev/null || true)
ck "sampler records a sample line" "$n" 1
if grep -q 'total_gib=' "$STORAGE_SAMPLE_LOG" && grep -q 'rate_mib_per_h=' "$STORAGE_SAMPLE_LOG"; then
  ok "sample carries capacity, growth and rate fields"
else
  bad "sample carries capacity, growth and rate fields" "missing field" "all present"
fi

"$SM" >/dev/null 2>&1
n=$(grep -c 'used_pct=' "$STORAGE_SAMPLE_LOG" 2>/dev/null || true)
ck "second sample appended" "$n" 2

: > "$STORAGE_LOG"
"$SM" >/dev/null 2>&1
n=$(grep -c 'alert_level=util_' "$STORAGE_LOG" 2>/dev/null || true)
ck "no alert while healthy" "$n" 0

STORAGE_WARN_PCT=20 "$SM" >/dev/null 2>&1
n=$(grep -c 'alert_level=util_warn' "$STORAGE_LOG" 2>/dev/null || true)
ck "warn transition alerts once" "$n" 1
STORAGE_WARN_PCT=20 "$SM" >/dev/null 2>&1
STORAGE_WARN_PCT=20 "$SM" >/dev/null 2>&1
n=$(grep -c 'alert_level=util_warn' "$STORAGE_LOG" 2>/dev/null || true)
ck "sustained warn does not re-alert" "$n" 1
"$SM" >/dev/null 2>&1
n=$(grep -c 'alert_level=util_recovered' "$STORAGE_LOG" 2>/dev/null || true)
ck "recovery alerts once" "$n" 1

"$SM" >/dev/null 2>&1
rc=$(STORAGE_SAMPLE_ANCHOR_OVERRIDE=/nonexistent/path "$SM" >/dev/null 2>&1; echo $?)
ck "measurement failure returns 20" "$rc" 20
n=$(grep -c 'MEASURE-FAILED' "$STORAGE_SAMPLE_LOG" 2>/dev/null || true)
ck "failure recorded in sample log" "$n" 1

echo
echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
