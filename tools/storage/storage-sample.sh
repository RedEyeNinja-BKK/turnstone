#!/usr/bin/env bash
# storage-sample — 15-minute capacity sampler. Read-only, never deletes.
#
# DESIGN CONSTRAINTS
#   * One `df` call. No recursive scan: `du` on a multi-GiB tree every 15
#     minutes would itself become the load the sampler is supposed to notice.
#   * Writes only its own sample log and the shared event log. It never takes
#     the build lock, never opens a target directory, and never runs cargo, so
#     it cannot interfere with the 4-hourly monitor, the daily summary or a
#     build campaign.
#   * Alerts are transition-based with a cooldown, so a sustained condition
#     does not emit a line every 15 minutes (96 a day).
#
# STATE MACHINE
#   util    : 0 OK | 1 WARN | 2 PRUNE | 3 HARD   -> fires on transition up,
#             on recovery, and as a bounded reminder while sustained.
#   growth  : fires when the delta between consecutive samples crosses the
#             threshold, then once per cooldown while it keeps happening.
#   failure : fires after FAIL_THRESHOLD consecutive unmeasurable samples, so a
#             single transient blip does not page anyone.
set -Euo pipefail
set +x

HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=/dev/null
. "$HERE/storage-policy.sh"

SAMPLE_LOG=${STORAGE_SAMPLE_LOG:-/var/lib/turnstone/storage-samples.log}
STATE=${STORAGE_SAMPLE_STATE:-/var/lib/turnstone/storage-sample.state}
GROWTH_WARN_GIB=${STORAGE_GROWTH_WARN_GIB:-5}
COOLDOWN_S=${STORAGE_SAMPLE_COOLDOWN:-3600}
REMIND_S=${STORAGE_REMIND_INTERVAL:-21600}   # 6h reminder while sustained
FAIL_THRESHOLD=${STORAGE_FAIL_THRESHOLD:-2}
MAX_LINES=${STORAGE_SAMPLE_MAX_LINES:-20000}

# Testability hook: lets the suite point the sampler at an unmeasurable path to
# exercise the failure branch. Production always uses storage_anchor().
anchor=${STORAGE_SAMPLE_ANCHOR_OVERRIDE:-$(storage_anchor)}
now=$(date -u +%s)
ts=$(date -u +%FT%TZ)

# ---- measure --------------------------------------------------------------
# -B1 gives exact bytes. One call, kernel-side, no directory traversal.
read -r total used avail pct < <(df -P -B1 "$anchor" 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $2, $3, $4, $5}')

if [ -z "${total:-}" ] || [ -z "${used:-}" ] || [ -z "${pct:-}" ]; then
  # Measurement failure: record it, and count consecutive failures.
  prev_fail=0
  [ -f "$STATE" ] && prev_fail=$(awk '{print $5}' "$STATE" 2>/dev/null || echo 0)
  [ -n "$prev_fail" ] || prev_fail=0
  fails=$((prev_fail + 1))
  printf '%s %s %s\n' "$ts" "MEASURE-FAILED" "$fails" >> "$SAMPLE_LOG" 2>/dev/null || true
  storage_emit error sample_failed anchor="$anchor" consecutive="$fails"
  if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
    storage_emit warn alert_level=measurement_failed consecutive="$fails" \
      note="capacity could not be read $fails times in a row"
  fi
  # Preserve the last good numbers so a failure does not corrupt the delta.
  prev_used=0; prev_sev=0; prev_alert_ts=0; prev_growth_ts=0
  [ -f "$STATE" ] && read -r prev_used prev_sev prev_alert_ts prev_growth_ts _ < "$STATE" 2>/dev/null || true
  printf '%s %s %s %s %s\n' "${prev_used:-0}" "${prev_sev:-0}" "${prev_alert_ts:-0}" "${prev_growth_ts:-0}" "$fails" > "$STATE" 2>/dev/null || true
  exit 20
fi

# ---- previous state -------------------------------------------------------
prev_used=0; prev_sev=0; prev_alert_ts=0; prev_growth_ts=0; prev_fail=0
if [ -f "$STATE" ]; then
  read -r prev_used prev_sev prev_alert_ts prev_growth_ts prev_fail _ < "$STATE" 2>/dev/null || true
fi
: "${prev_used:=0}" "${prev_sev:=0}" "${prev_alert_ts:=0}" "${prev_growth_ts:=0}"

# ---- growth ---------------------------------------------------------------
delta_bytes=0; growth_gib=0; rate_mib_h=0
if [ "$prev_used" -gt 0 ] 2>/dev/null; then
  delta_bytes=$(( used - prev_used ))
  growth_gib=$(awk -v d="$delta_bytes" 'BEGIN{printf "%.2f", d/1073741824}')
  rate_mib_h=$(awk -v d="$delta_bytes" 'BEGIN{printf "%.0f", (d/1048576)*(3600/900)}')
fi

# ---- severity -------------------------------------------------------------
sev=0; label=OK
if   [ "$pct" -ge "$STORAGE_HARD_PCT" ];  then sev=3; label=HARD
elif [ "$pct" -ge "$STORAGE_PRUNE_PCT" ]; then sev=2; label=PRUNE
elif [ "$pct" -ge "$STORAGE_WARN_PCT" ];  then sev=1; label=WARN
fi

used_gib=$(awk -v b="$used"  'BEGIN{printf "%.2f", b/1073741824}')
avail_gib=$(awk -v b="$avail" 'BEGIN{printf "%.2f", b/1073741824}')
total_gib=$(awk -v b="$total" 'BEGIN{printf "%.2f", b/1073741824}')

# ---- one sample line ------------------------------------------------------
printf '%s anchor=%s total_gib=%s used_gib=%s avail_gib=%s used_pct=%s delta_mib=%.0f rate_mib_per_h=%s sev=%s\n' \
  "$ts" "$anchor" "$total_gib" "$used_gib" "$avail_gib" "$pct" \
  "$(awk -v d="$delta_bytes" 'BEGIN{printf "%.0f", d/1048576}')" "$rate_mib_h" "$label" \
  >> "$SAMPLE_LOG" 2>/dev/null || storage_emit warn sample_log_unwritable path="$SAMPLE_LOG"

# Keep the log bounded (append-only, trimmed rarely and cheaply).
if [ -f "$SAMPLE_LOG" ]; then
  n=$(wc -l < "$SAMPLE_LOG" 2>/dev/null || echo 0)
  if [ "$n" -gt "$MAX_LINES" ]; then
    tail -n "$MAX_LINES" "$SAMPLE_LOG" > "$SAMPLE_LOG.tmp" 2>/dev/null && \
      mv "$SAMPLE_LOG.tmp" "$SAMPLE_LOG" 2>/dev/null || rm -f "$SAMPLE_LOG.tmp" 2>/dev/null
  fi
fi

new_alert_ts=$prev_alert_ts
new_growth_ts=$prev_growth_ts

# ---- utilisation transitions ---------------------------------------------
if [ "$sev" -gt "$prev_sev" ]; then
  storage_emit warn alert_level="util_${label,,}" used_pct="$pct" \
    prev_sev="$prev_sev" available_gib="$avail_gib" action="run storage-monitor.sh"
  new_alert_ts=$now
elif [ "$sev" -lt "$prev_sev" ] && [ "$prev_sev" -ge 1 ]; then
  storage_emit info alert_level="util_recovered" used_pct="$pct" \
    from_sev="$prev_sev" to_sev="$sev"
  new_alert_ts=$now
elif [ "$sev" -ge 1 ]; then
  # sustained: remind, but no more often than REMIND_S
  if [ $(( now - prev_alert_ts )) -ge "$REMIND_S" ]; then
    storage_emit warn alert_level="util_${label,,}_sustained" used_pct="$pct" \
      since_s="$(( now - prev_alert_ts ))"
    new_alert_ts=$now
  fi
fi

# ---- rapid growth ---------------------------------------------------------
# A jump of >= GROWTH_WARN_GIB between two 15-minute samples is an event in
# itself: it is what a runaway build looks like long before the disk fills.
rapid=$(awk -v g="$growth_gib" -v t="$GROWTH_WARN_GIB" 'BEGIN{print (g >= t) ? 1 : 0}')
if [ "$rapid" -eq 1 ]; then
  if [ $(( now - prev_growth_ts )) -ge "$COOLDOWN_S" ]; then
    storage_emit warn alert_level=rapid_growth growth_gib="$growth_gib" \
      rate_mib_per_h="$rate_mib_h" used_pct="$pct" \
      note="check for an unexpected cargo/rustc process: storage-detect.sh"
    new_growth_ts=$now
  fi
fi

printf '%s %s %s %s %s\n' "$used" "$sev" "$new_alert_ts" "$new_growth_ts" 0 > "$STATE" 2>/dev/null || \
  storage_emit warn sample_state_unwritable path="$STATE"

storage_emit info sample used_pct="$pct" used_gib="$used_gib" avail_gib="$avail_gib" \
  delta_mib="$(awk -v d="$delta_bytes" 'BEGIN{printf "%.0f", d/1048576}')" \
  rate_mib_per_h="$rate_mib_h" sev="$label"

rc=0
[ "$sev" -ge 1 ] && rc=10
[ "$sev" -ge 2 ] && rc=11
[ "$sev" -ge 3 ] && rc=12
exit "$rc"
