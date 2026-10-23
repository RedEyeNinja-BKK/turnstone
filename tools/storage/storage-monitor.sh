#!/usr/bin/env bash
# storage-monitor — read-only storage monitoring. NEVER deletes anything.
#
# Separation of concerns: this script only observes and reports. Every
# destructive decision lives in storage-prune.sh, which is separately gated.
# A monitor that can also delete is a monitor you cannot safely run on a timer.
#
# Exit codes (for systemd / alerting):
#   0  healthy, below WARN
#   10 WARN    threshold reached — report only
#   11 CRITICAL at or above PRUNE — a human must act
#   12 CRITICAL at or above HARD
#   20 the monitor itself failed to measure (fail loud, do not report "ok")
#
# Usage: storage-monitor.sh [--json]
set -Eeuo pipefail
set +x

HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=/dev/null
. "$HERE/storage-policy.sh"

JSON=0
[ "${1:-}" = "--json" ] && JSON=1
# Tell the shared emitter to keep stdout clean for the JSON document.
[ "$JSON" -eq 1 ] && STORAGE_JSON=1
export STORAGE_JSON

anchor=$(storage_anchor)
pct=$(storage_used_pct "$anchor")
free=$(storage_free_gib "$anchor")

if [ -z "$pct" ] || [ -z "$free" ]; then
  storage_emit error monitor_failed reason=cannot_measure anchor="$anchor"
  echo "storage-monitor: cannot measure $anchor" >&2
  exit 20
fi

# Collect build roots.
build_rows=()
build_total=0
for t in "${STORAGE_TARGETS[@]}"; do
  if [ -d "$t" ]; then
    mib=$(storage_size_mib "$t"); build_total=$((build_total + mib))
    build_rows+=("$t|$mib|present")
  else
    build_rows+=("$t|0|absent")
  fi
done

# Detect unexpected build roots: an ad-hoc CARGO_TARGET_DIR is the exact
# mechanism that produced 43 GiB before. Report, never remove.
unexpected=()
for pat in "${STORAGE_UNEXPECTED_PATTERNS[@]}"; do
  for hit in $pat; do
    [ -e "$hit" ] || continue
    # skip anything already in TARGETS
    skip=0
    for t in "${STORAGE_TARGETS[@]}"; do [ "$hit" = "$t" ] && skip=1; done
    [ "$skip" -eq 1 ] && continue
    unexpected+=("$hit|$(storage_size_mib "$hit")")
  done
done

# pytest sandboxes.
pytest_rows=()
pytest_total=0
for g in "${STORAGE_PYTEST_GLOBS[@]}"; do
  for d in $g; do
    [ -d "$d" ] || continue
    mib=$(storage_size_mib "$d"); pytest_total=$((pytest_total + mib))
    pytest_rows+=("$d|$mib")
  done
done

running="no"
storage_build_active && running="yes"

# Protected-path presence check: a missing protected path is a finding, not a
# reason to stop reporting.
missing_protected=()
for p in "${STORAGE_REQUIRED[@]}"; do
  [ -e "$p" ] || missing_protected+=("$p")
done

if [ "$JSON" -eq 1 ]; then
  printf '{'
  printf '"anchor":"%s","used_pct":%s,"free_gib":%s,' "$anchor" "$pct" "$free"
  printf '"warn":%s,"prune":%s,"hard":%s,' "$STORAGE_WARN_PCT" "$STORAGE_PRUNE_PCT" "$STORAGE_HARD_PCT"
  printf '"build_active":"%s","build_total_mib":%s,' "$running" "$build_total"
  printf '"build_roots":['
  first=1
  for r in "${build_rows[@]}"; do
    IFS='|' read -r path mib state <<<"$r"
    [ $first -eq 1 ] || printf ','
    first=0
    printf '{"path":"%s","mib":%s,"state":"%s"}' "$path" "$mib" "$state"
  done
  printf '],"unexpected":['
  first=1
  for r in "${unexpected[@]}"; do
    IFS='|' read -r path mib <<<"$r"
    [ $first -eq 1 ] || printf ','
    first=0
    printf '{"path":"%s","mib":%s}' "$path" "$mib"
  done
  printf '],"pytest":['
  first=1
  for r in "${pytest_rows[@]}"; do
    IFS='|' read -r path mib <<<"$r"
    [ $first -eq 1 ] || printf ','
    first=0
    printf '{"path":"%s","mib":%s}' "$path" "$mib"
  done
  printf '],"pytest_total_mib":%s' "$pytest_total"
  printf ',"missing_protected":['
  first=1
  for p in "${missing_protected[@]}"; do
    [ $first -eq 1 ] || printf ','
    first=0
    printf '"%s"' "$p"
  done
  printf ']}\n'
else
  echo "storage-monitor $(date -u +%FT%TZ)"
  echo "  anchor          : $anchor"
  echo "  filesystem used : ${pct}%   free: ${free} GiB"
  echo "  thresholds      : warn ${STORAGE_WARN_PCT}% / prune ${STORAGE_PRUNE_PCT}% / hard ${STORAGE_HARD_PCT}%"
  echo "  build active    : $running"
  echo "  build roots     : ${build_total} MiB total"
  for r in "${build_rows[@]}"; do
    IFS='|' read -r path mib state <<<"$r"
    printf '    %-62s %8s MiB  %s\n' "$path" "$mib" "$state"
  done
  if [ "${#unexpected[@]}" -gt 0 ]; then
    echo "  UNEXPECTED build roots (ad-hoc CARGO_TARGET_DIR):"
    for r in "${unexpected[@]}"; do
      IFS='|' read -r path mib <<<"$r"
      printf '    %-62s %8s MiB\n' "$path" "$mib"
    done
  else
    echo "  unexpected roots: none"
  fi
  echo "  pytest sandboxes: ${pytest_total} MiB"
  for r in "${pytest_rows[@]}"; do
    IFS='|' read -r path mib <<<"$r"
    printf '    %-62s %8s MiB\n' "$path" "$mib"
  done
  if [ "${#missing_protected[@]}" -gt 0 ]; then
    echo "  MISSING protected paths:"
    for p in "${missing_protected[@]}"; do echo "    $p"; done
  fi
fi

# ---- decision -------------------------------------------------------------
storage_emit info monitor anchor="$anchor" used_pct="$pct" free_gib="$free" \
  build_active="$running" build_total_mib="$build_total" \
  pytest_total_mib="$pytest_total" unexpected_roots="${#unexpected[@]}" \
  missing_protected="${#missing_protected[@]}"

for r in "${unexpected[@]}"; do
  IFS='|' read -r path mib <<<"$r"
  storage_emit warn unexpected_build_root path="$path" mib="$mib"
done

rc=0
verdict="OK"
if [ "$pct" -ge "$STORAGE_HARD_PCT" ]; then
  rc=12; verdict="HARD"
elif [ "$pct" -ge "$STORAGE_PRUNE_PCT" ]; then
  rc=11; verdict="PRUNE"
elif [ "$pct" -ge "$STORAGE_WARN_PCT" ]; then
  rc=10; verdict="WARN"
fi

[ "${#missing_protected[@]}" -gt 0 ] && storage_emit warn protected_path_missing count="${#missing_protected[@]}"
[ "${#unexpected[@]}" -gt 0 ] && [ "$rc" -lt 10 ] && rc=10

if [ "$JSON" -eq 0 ]; then
  echo "  verdict         : $verdict (exit $rc)"
else
  :
fi
storage_emit info verdict state="$verdict" exit="$rc"
exit "$rc"
