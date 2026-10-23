#!/usr/bin/env bash
# storage-daily — one consolidated daily storage summary. Read-only.
#
# Reads the retained logs rather than re-deriving everything, so the summary
# reflects what the scheduled jobs actually saw. It also compares scheduled
# runs against what should have happened, which is how a silently dead monitor
# is caught: a monitor that stops is the failure mode nobody notices.
#
# Usage: storage-daily.sh [--json]
set -Euo pipefail
set +x

HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=/dev/null
. "$HERE/storage-policy.sh"

JSON=0
[ "${1:-}" = "--json" ] && JSON=1
[ "$JSON" -eq 1 ] && { STORAGE_JSON=1; export STORAGE_JSON; }

EVENTS=$(storage_log_file)
MONLOG=${STORAGE_MONITOR_LOG:-/var/lib/turnstone/storage-monitor.log}
WIN_START=${OBSERVATION_START:-2026-10-23T19:05:08Z}
WIN_END=${OBSERVATION_END:-2026-10-30T19:05:08Z}

# Run the detection sweep as part of the daily summary, so the report contains
# real bypass/in-tree/growth findings without needing a second scheduled job.
# Detection is read-only; a failure here must not prevent the summary.
DETECT=${STORAGE_DETECT:-$(dirname "$(readlink -f "$0")")/storage-detect.sh}
if [ "${STORAGE_SKIP_DETECT:-0}" -ne 1 ] && [ -x "$DETECT" ]; then
  "$DETECT" >/dev/null 2>&1 || true
fi

today=$(date -u +%Y-%m-%d)
day_start="${today}T00:00:00Z"

anchor=$(storage_anchor)
pct=$(storage_used_pct "$anchor")
free=$(storage_free_gib "$anchor")

# --- monitoring liveness ---------------------------------------------------
# Six runs per day are expected (every 4 hours). Count today's monitor events.
mon_all=$(grep -c "event=monitor" "$EVENTS" 2>/dev/null || true)
last_mon=$(grep "event=monitor" "$EVENTS" 2>/dev/null | tail -1 | awk '{print $1}')
if [ -n "$last_mon" ]; then
  last_epoch=$(date -u -d "$last_mon" +%s 2>/dev/null || true)
  now_epoch=$(date -u +%s)
  age_h=$(( (now_epoch - last_epoch) / 3600 ))
else
  age_h=-1
fi

# --- findings from the retained event log ---------------------------------
unexpected=$(grep -c "event=unexpected_build_root" "$EVENTS" 2>/dev/null || true)
would_remove=$(grep -c "event=would_remove" "$EVENTS" 2>/dev/null || true)
blocked=$(grep -c "event=prune_blocked" "$EVENTS" 2>/dev/null || true)
errors=$(grep -cE "level=error|event=monitor_failed|event=prune_failed" "$EVENTS" 2>/dev/null || true)
bypass=$(grep -c "event=bypass_kind" "$EVENTS" 2>/dev/null || true)
in_tree=$(grep -c "event=in_tree_target" "$EVENTS" 2>/dev/null || true)
rapid=$(grep -c "event=rapid_growth" "$EVENTS" 2>/dev/null || true)

# proposed candidates, de-duplicated to the most recent path
# Latest size per path. Summing every historical would_remove line would count
# the same directory once per scheduled run.
cand_map=$(grep "event=would_remove" "$EVENTS" 2>/dev/null | \
  sed -n 's/.*path=\([^ ]*\).*mib=\([0-9]*\).*/\1 \2/p' | \
  awk '{last[$1]=$2} END{for (p in last) print p, last[p]}' | sort)
candidates=$(printf '%s\n' "$cand_map" | awk '{print $1}' | grep . || true)
cand_count=$(printf '%s\n' "$cand_map" | grep -c . || true)
cand_mib=$(printf '%s\n' "$cand_map" | awk '{s+=$2} END{printf "%d", s+0}')

# --- build footprint -------------------------------------------------------
build_total=0
build_lines=""
for t in "${STORAGE_TARGETS[@]}"; do
  if [ -d "$t" ]; then
    m=$(storage_size_mib "$t"); build_total=$((build_total + m))
    build_lines="$build_lines  $(printf '%-62s %8s MiB' "$t" "$m")\n"
  fi
done

# --- verdict ---------------------------------------------------------------
rc=0
[ "${pct:-0}" -ge "$STORAGE_WARN_PCT" ] && rc=10
[ "${pct:-0}" -ge "$STORAGE_PRUNE_PCT" ] && rc=11
[ "${pct:-0}" -ge "$STORAGE_HARD_PCT" ] && rc=12
[ "$errors" -gt 0 ] && rc=20

health=OK
[ "$rc" -eq 10 ] && health=WARN
[ "$rc" -eq 11 ] && health=PRUNE
[ "$rc" -eq 12 ] && health=HARD
[ "$rc" -eq 20 ] && health=ERROR

if [ "$JSON" -eq 1 ]; then
  # One printf, one format string: several chained printfs made it too easy to
  # emit a stray separator between fields.
  printf '{"date":"%s","used_pct":%s,"free_gib":%s,"health":"%s","monitor_runs_total":%s,"monitor_age_hours":%s,"unexpected_roots":%s,"bypass_findings":%s,"in_tree_targets":%s,"rapid_growth":%s,"proposed_candidates":%s,"proposed_mib":%s,"blocked_prunes":%s,"errors":%s,"build_total_mib":%s,"observation_start":"%s","observation_end":"%s"}\n' \
    "$today" "${pct:-0}" "${free:-0}" "$health" \
    "$mon_all" "$age_h" "$unexpected" "$bypass" "$in_tree" "$rapid" \
    "$cand_count" "$cand_mib" "$blocked" "$errors" "$build_total" \
    "$WIN_START" "$WIN_END"
  storage_emit info daily anchor="$anchor" used_pct="${pct:-0}" health="$health" exit="$rc"
  exit "$rc"
fi

cat <<EOF
Daily storage summary — ${today}
=================================================
Observation window : ${WIN_START} .. ${WIN_END}
Health             : ${health} (exit ${rc})
Filesystem         : ${anchor} at ${pct}% used, ${free} GiB free
Thresholds         : warn ${STORAGE_WARN_PCT}% / prune ${STORAGE_PRUNE_PCT}% / hard ${STORAGE_HARD_PCT}%

Monitoring liveness
  monitor runs recorded : ${mon_all} (expected 6/day)
  last monitor event    : ${last_mon:-none}
  last monitor age      : ${age_h}h $([ "$age_h" -gt 8 ] && echo '<-- OVERDUE, monitor may have stopped' || echo '(within the 4h cadence)')

Build footprint
  total across roots    : ${build_total} MiB
$(printf "%b" "$build_lines")
Findings since start
  unexpected build roots : ${unexpected}
  bypass findings        : ${bypass} (in-tree targets seen: ${in_tree})
  multiple campaigns     : $(grep -c 'event=concurrent_campaigns' "$EVENTS" 2>/dev/null || true)
  rapid growth events    : ${rapid}
  monitoring errors      : ${errors}
  prunes blocked         : ${blocked}

Proposed cleanup (dry run only — nothing is deleted)
  candidates             : ${cand_count}
  total proposed          : ${cand_mib} MiB
$(printf '%s\n' "$candidates" | grep . | sed 's/^/    /' || echo "    (none)")
EOF

storage_emit info daily anchor="$anchor" used_pct="${pct:-0}" health="$health" \
  monitor_runs="$mon_all" monitor_age_h="$age_h" unexpected="$unexpected" \
  bypass="$bypass" in_tree="$in_tree" rapid="$rapid" \
  proposed_candidates="$cand_count" proposed_mib="$cand_mib" \
  blocked="$blocked" errors="$errors" build_total_mib="$build_total" exit="$rc"
exit "$rc"
