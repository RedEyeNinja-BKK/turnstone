#!/usr/bin/env bash
# storage-alert — transition-based alerting for the storage controls.
#
# WHY TRANSITIONS, NOT LEVELS
#   A level-triggered alert on a 4-hourly job sends the same message six times a
#   day for a condition that has not changed. That is how an alert channel gets
#   ignored. This emits on ENTERING a worse state, on a bounded reminder while
#   still in it, and on RECOVERY.
#
# DELIVERY
#   No notification channel is configured on this host and none is created
#   here. If STORAGE_ALERT_CMD is set, it is invoked once per alert with the
#   message on stdin. Otherwise alerts are written to a queue file that the
#   owner (or a future dispatcher) can drain. Either way nothing is lost and
#   nothing outbound happens without an explicit channel.
#
# Usage: storage-alert.sh [--test]
set -Euo pipefail
set +x

HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=/dev/null
. "$HERE/storage-policy.sh"

STATE=${STORAGE_ALERT_STATE:-/var/lib/turnstone/storage-alert.state}
QUEUE=${STORAGE_ALERT_QUEUE:-/var/lib/turnstone/storage-alerts.queue}
REMIND_HOURS=${STORAGE_ALERT_REMIND_HOURS:-24}
CMD=${STORAGE_ALERT_CMD:-}

TEST_MODE=0
[ "${1:-}" = "--test" ] && TEST_MODE=1

now=$(date -u +%s)
anchor=$(storage_anchor)
pct=$(storage_used_pct "$anchor")
[ -n "$pct" ] || { echo "cannot measure; no alert emitted" >&2; exit 20; }

# ---- current severity -----------------------------------------------------
# 0 OK | 1 WARN | 2 PRUNE | 3 HARD | 4 MEASURE-FAILURE
sev=0; label=OK
if [ "$pct" -ge "$STORAGE_HARD_PCT" ]; then sev=3; label=HARD
elif [ "$pct" -ge "$STORAGE_PRUNE_PCT" ]; then sev=2; label=PRUNE
elif [ "$pct" -ge "$STORAGE_WARN_PCT" ]; then sev=1; label=WARN
fi

# An unexpected build root is its own condition: it is a control bypass, not a
# fullness problem, and it should be reported even at low utilisation.
unexpected=""
if [ -x "$HERE/storage-detect.sh" ]; then
  unexpected=$("$HERE/storage-detect.sh" 2>/dev/null | grep -c 'event=in_tree_target' || true)
fi
unexpected=${unexpected:-0}
if [ "$unexpected" -gt 0 ] && [ "$sev" -lt 1 ]; then sev=1; label="WARN(bypass)"; fi

# ---- previous state -------------------------------------------------------
prev_sev=-1; prev_label=""; prev_ts=0
if [ -f "$STATE" ]; then
  prev_sev=$(awk '{print $1}' "$STATE" 2>/dev/null || echo -1)
  prev_label=$(awk '{print $2}' "$STATE" 2>/dev/null || echo "")
  prev_ts=$(awk '{print $3}' "$STATE" 2>/dev/null || echo 0)
fi
[ -n "$prev_sev" ] || prev_sev=-1
[ -n "$prev_ts" ] || prev_ts=0

emit_alert() {
  local subject=$1 body=$2
  local msg
  msg="[turnstone storage] ${subject}
${body}
host     : $(hostname)
time     : $(date -u +%FT%TZ)
anchor   : ${anchor}
used     : ${pct}%  (warn ${STORAGE_WARN_PCT} / prune ${STORAGE_PRUNE_PCT} / hard ${STORAGE_HARD_PCT})
free     : $(storage_free_gib "$anchor") GiB
unexpected build roots: ${unexpected}
inspect  : storage-monitor.sh --json ; storage-daily.sh ; tail -n 50 $(storage_log_file)"
  storage_emit warn alert_emitted subject="$subject" sev="$sev" channel="${CMD:-queue}"
  if [ -n "$CMD" ]; then
    # A failing dispatcher must not lose the alert: fall through to the queue.
    if printf '%s\n' "$msg" | $CMD; then
      return 0
    fi
    storage_emit error alert_dispatch_failed channel="$CMD"
  fi
  printf '%s\n---\n' "$msg" >> "$QUEUE" 2>/dev/null || \
    storage_emit error alert_queue_unwritable path="$QUEUE"
  return 0
}

if [ "$TEST_MODE" -eq 1 ]; then
  emit_alert "TEST: severity ${label}" "Synthetic alert to verify delivery. No storage condition exists."
  echo "test alert written to ${QUEUE} (or dispatched to ${CMD:-queue})" >&2
  exit 0
fi

changed=0
if [ "$prev_sev" -lt 0 ]; then
  # First ever run: record the baseline silently. Announcing "OK" on install
  # trains the reader to ignore the channel before anything is wrong.
  printf '%s %s %s\n' "$sev" "$label" "$now" > "$STATE" 2>/dev/null || true
  storage_emit info alert_baseline sev="$sev" label="$label"
  storage_emit info alert_eval sev="$sev" label="$label" prev_sev=-1 emitted=0
  exit 0
fi

if [ "$sev" -gt "$prev_sev" ]; then
  changed=1
  emit_alert "storage ${label}: ${pct}% used" \
    "Escalated from ${prev_label:-none} to ${label}."
elif [ "$sev" -lt "$prev_sev" ] && [ "$prev_sev" -ge 1 ]; then
  changed=1
  emit_alert "storage RECOVERED: ${label}" \
    "Recovered from ${prev_label} to ${label}."
elif [ "$sev" -ge 1 ]; then
  # still in a bad state: remind, but not more often than REMIND_HOURS
  age_h=$(( (now - prev_ts) / 3600 ))
  if [ "$age_h" -ge "$REMIND_HOURS" ]; then
    changed=1
    emit_alert "storage ${label} (reminder): ${pct}% used" \
      "Unchanged for ${age_h}h."
  fi
fi

# Persist the state every run so the reminder clock is measured from the last
# emission, not from the last check.
if [ "$changed" -eq 1 ] || [ "$prev_sev" != "$sev" ]; then
  printf '%s %s %s\n' "$sev" "$label" "$now" > "$STATE" 2>/dev/null || true
fi

storage_emit info alert_eval sev="$sev" label="$label" prev_sev="$prev_sev" emitted="$changed"
exit 0
