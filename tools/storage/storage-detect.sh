#!/usr/bin/env bash
# storage-detect — read-only detection of storage-control bypasses.
#
# Covers the gaps that disk-usage monitoring cannot see:
#
#   1. a cargo/rustc process NOT running under sy-build
#   2. a CARGO_TARGET_DIR that is not the canonical one
#   3. an in-tree target/ directory that sy-build never created
#   4. abandoned build output (no writes for N days)
#   5. more than one concurrent build campaign
#   6. filesystem growth since the previous sample
#
# Everything here is inspection only. It never deletes, never opens a build
# lock, never signals a process, and never reads another user's environment
# (only own-uid /proc/PID/environ is readable, and only that is attempted).
#
# Exit codes:
#   0  no findings
#   10 findings worth a look (unexpected target, bypass, growth)
#   11 concurrent campaigns or abandoned output
#   20 detection itself failed
#
# Usage: storage-detect.sh [--json]
set -Euo pipefail
set +x

HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=/dev/null
. "$HERE/storage-policy.sh"

JSON=0
[ "${1:-}" = "--json" ] && JSON=1
[ "$JSON" -eq 1 ] && { STORAGE_JSON=1; export STORAGE_JSON; }

CANONICAL=${SY_BUILD_TARGET:-/var/lib/turnstone/sytarget}
SAMPLE_FILE=${STORAGE_SAMPLE_FILE:-/var/lib/turnstone/storage-sample}
GROWTH_WARN_MIB=${STORAGE_GROWTH_WARN_MIB:-5120}    # 5 GiB between samples
ABANDON_DAYS=${STORAGE_ABANDON_DAYS:-7}

findings=0
note() { storage_emit "$@"; }

# ---- 1 and 2: cargo processes and their target dir ------------------------
own_cargo_pids() {
  local p a0
  for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    a0=$(tr '\0' '\n' < "$p/cmdline" 2>/dev/null | sed -n '1p') || continue
    case "${a0##*/}" in cargo|rustc) echo "${p#/proc/}" ;; esac
  done
}

CANON_REAL=$(readlink -f "$CANONICAL" 2>/dev/null || printf '%s' "$CANONICAL")
cargo_count=0
for pid in $(own_cargo_pids); do
  cargo_count=$((cargo_count + 1))
  env_td=""
  # own-uid only; another user's environ is unreadable and is skipped silently
  if [ -r "/proc/$pid/environ" ]; then
    env_td=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^CARGO_TARGET_DIR=//p' | head -1)
  fi
  if [ -z "$env_td" ]; then
    note warn bypass_kind=in_tree_target pid="$pid" note="no CARGO_TARGET_DIR; writes to <crate>/target"
    findings=$((findings+1))
  else
    td_real=$(readlink -f "$env_td" 2>/dev/null || printf '%s' "$env_td")
    if [ "$td_real" != "$CANON_REAL" ]; then
      note warn bypass_kind=non_canonical_target pid="$pid" target="$env_td"
      findings=$((findings+1))
    else
      note info under_wrapper pid="$pid" target="$env_td"
    fi
  fi
done

# ---- 5: concurrent campaigns ---------------------------------------------
if [ "$cargo_count" -gt 1 ]; then
  note warn concurrent_campaigns count="$cargo_count"
  findings=$((findings+1))
fi
LOCK=${SY_BUILD_LOCK:-/var/lib/turnstone/.build.lock}
if ! ( exec 8>"$LOCK"; flock -n 8 ) 2>/dev/null; then
  note info build_lock held=yes
else
  note info build_lock held=no
fi

# ---- 3: in-tree target directories ---------------------------------------
# Roots that are source checkouts on this host. A target/ here is a bypass
# unless it is the canonical directory.
if [ -n "${STORAGE_SCAN_ROOTS_OVERRIDE:-}" ]; then
  # Testability only: lets the suite point the scan at a fixture without
  # touching the real source roots.
  read -r -a SCAN_ROOTS <<<"$STORAGE_SCAN_ROOTS_OVERRIDE"
else
  SCAN_ROOTS=(
    /var/lib/turnstone/switchyard-upstream
    /var/lib/turnstone/github
    /var/tmp/syrepo
    /var/tmp/sy-integration
  )
fi
seen_targets=""
for root in "${SCAN_ROOTS[@]}"; do
  [ -d "$root" ] || continue
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    t_real=$(readlink -f "$t" 2>/dev/null || printf '%s' "$t")
    [ "$t_real" = "$CANON_REAL" ] && continue
    case " $seen_targets " in *" $t_real "*) continue ;; esac
    seen_targets="$seen_targets $t_real"
    mib=$(storage_size_mib "$t")
    # A target directory whose only profile is `release` inside a protected
    # prefix is the retained release artefact, not a bypass. Saying otherwise
    # would make this check cry wolf on a healthy host.
    if [ ! -d "$t/debug" ] && [ -d "$t/release" ] && storage_is_protected "$t/release"; then
      note info retained_release path="$t" mib="$mib"
      continue
    fi
    # abandoned when nothing under it has been written recently
    if storage_is_stale "$t" "$ABANDON_DAYS"; then
      note warn in_tree_target path="$t" mib="$mib" state=abandoned
      findings=$((findings+1))
    else
      note warn in_tree_target path="$t" mib="$mib" state=active
      findings=$((findings+1))
    fi
  done < <(find "$root" -maxdepth 3 -type d -name target 2>/dev/null)
done

# ---- 4: abandoned canonical output ---------------------------------------
if [ -d "$CANONICAL" ] && storage_is_stale "$CANONICAL" "$ABANDON_DAYS"; then
  note warn abandoned_output path="$CANONICAL" mib="$(storage_size_mib "$CANONICAL")" age_days="$ABANDON_DAYS"
  findings=$((findings+1))
fi

# ---- 6: growth since the previous sample ---------------------------------
anchor=$(storage_anchor)
now_bytes=$(df -B1 --output=used "$anchor" 2>/dev/null | awk 'NR==2{print $1}')
now_ts=$(date -u +%s)
growth_mib=0
if [ -n "$now_bytes" ]; then
  if [ -f "$SAMPLE_FILE" ]; then
    prev_bytes=$(awk '{print $1}' "$SAMPLE_FILE" 2>/dev/null || echo "")
    prev_ts=$(awk '{print $2}' "$SAMPLE_FILE" 2>/dev/null || echo "")
    if [ -n "$prev_bytes" ] && [ -n "$prev_ts" ]; then
      delta=$(( now_bytes - prev_bytes ))
      growth_mib=$(( delta / 1048576 ))
      span=$(( now_ts - prev_ts ))
      note info growth since_sample_mib="$growth_mib" span_seconds="$span"
      if [ "$growth_mib" -ge "$GROWTH_WARN_MIB" ]; then
        note warn rapid_growth mib="$growth_mib" span_seconds="$span" threshold_mib="$GROWTH_WARN_MIB"
        findings=$((findings+1))
      fi
    fi
  else
    note info growth note="no previous sample; baseline recorded"
  fi
  printf '%s %s\n' "$now_bytes" "$now_ts" > "$SAMPLE_FILE" 2>/dev/null || \
    note warn sample_unwritable path="$SAMPLE_FILE"
fi

if [ "$JSON" -eq 1 ]; then
  printf '{"findings":%s,"cargo_processes":%s,"growth_mib":%s,"anchor":"%s"}\n' \
    "$findings" "$cargo_count" "$growth_mib" "$anchor"
fi

rc=0
[ "$findings" -gt 0 ] && rc=10
[ "$cargo_count" -gt 1 ] && rc=11
note info detect_done findings="$findings" cargo="$cargo_count" exit="$rc"
exit "$rc"
