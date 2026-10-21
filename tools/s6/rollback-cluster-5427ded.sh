#!/usr/bin/env bash
# S6-t2 ROLLBACK — predecessor-aware, one service at a time (RUN AS ROOT).
#
# Puts a service back on the verified cb7fece predecessor by restoring the
# EXACT drop-in bytes that were preserved when it was upgraded. It never
# "re-renders" a rollback file: the bytes on disk after a successful rollback
# are byte-identical to the bytes that were running before the upgrade, and
# that equality is asserted, not assumed.
#
# What it does NOT do: touch credentials, sudoers, OAuth stores, the registry,
# Output Guard, routing, Switchyard, users/groups, restart policy, the R2
# overlay, any immutable slot, or any telemetry. It does not delete a single
# observation.
#
# Usage:
#   rollback-cluster.sh --status            # read-only per-unit state
#   rollback-cluster.sh --dry-run           # print the plan, change nothing
#   rollback-cluster.sh                     # roll back every eligible unit
#   rollback-cluster.sh --unit turnstone-node@3
#   rollback-cluster.sh --wait-seconds 900  # wait up to 15 min for idle
#   rollback-cluster.sh --emergency turnstone-node@3
#
# --emergency is a DIFFERENT authority, documented in the handoff: it exists for
# a genuinely unhealthy service and it MAY TERMINATE IN-FLIGHT WORK. It refuses
# unless the unit is actually unhealthy, and it is recorded in the log.
#
# Exit codes:
#   0  every requested unit is on the predecessor and verified
#   2  refused (drift / bad backup / missing unit / emergency misuse)
#   3  a unit failed and its restoration did not verify
#   5  refused: a workstream is in flight and --emergency was not given
#   6  partial: some units rolled back, some still on the candidate
set -uo pipefail

# ---------------------------------------------------------------- configuration
SLOT=/opt/turnstone-overlays/sensor-v187-5427ded      # the CANDIDATE (what we roll back FROM)
PRED_SLOT=/opt/turnstone-overlays/sensor-v187-cb7fece # the PREDECESSOR (what we roll back TO)
PRED_EPOCH=/var/lib/turnstone/sensor/s6-t1
TOKENIZER=/var/lib/turnstone/sensor-runtime
ROLLOUT=/var/lib/turnstone/artifacts/s6/rollout
DROPIN_ROOT=${S6_DROPIN_ROOT:-/etc/systemd/system}
FRESH_MINUTES=${FRESH_MINUTES:-15}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG=$ROLLOUT/rollback-$STAMP.log

# unit kind port service-dir
UNITS=(
  "turnstone-node@5 node 8085 turnstone-node-5"
  "turnstone-node@4 node 8084 turnstone-node-4"
  "turnstone-node@3 node 8083 turnstone-node-3"
  "turnstone-node@2 node 8082 turnstone-node-2"
  "turnstone-console console 8090 console"
  "turnstone-server server 8080 turnstone-head-1"
)
# Roll back in REVERSE upgrade order: the last service upgraded is the first
# rolled back, so an interrupted run leaves a contiguous prefix on one version.
ORDER=(
  "turnstone-server server 8080 turnstone-head-1"
  "turnstone-console console 8090 console"
  "turnstone-node@2 node 8082 turnstone-node-2"
  "turnstone-node@3 node 8083 turnstone-node-3"
  "turnstone-node@4 node 8084 turnstone-node-4"
  "turnstone-node@5 node 8085 turnstone-node-5"
)

DRY_RUN=0
STATUS_ONLY=0
WAIT_SECONDS=0
ONLY_UNIT=""
EMERGENCY_UNIT=""
CURRENT_UNIT=""

say() { printf '%s\n' "$*" | tee -a "$LOG"; }
die() { say "FATAL: $*"; exit 2; }

mkdir -p "$ROLLOUT" 2>/dev/null || true
: > "$LOG" 2>/dev/null || true

while [ $# -gt 0 ]; do
  case "$1" in
    --status) STATUS_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --wait-seconds) WAIT_SECONDS=${2:?}; shift 2 ;;
    --unit) ONLY_UNIT=${2:?}; shift 2 ;;
    --emergency) EMERGENCY_UNIT=${2:?}; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "must run as root"

# ------------------------------------------------------------------ primitives
spec_for() {  # unit -> echoes "kind port dir"
  local line
  for line in "${UNITS[@]}"; do
    if [ "${line%% *}" = "$1" ]; then printf '%s\n' "${line#* }"; return 0; fi
  done
  return 1
}

dropin_path() { printf '%s/%s.service.d/95-sensor.conf\n' "$DROPIN_ROOT" "$1"; }

backup_index() { printf '%s/predecessor/INDEX.tsv\n' "$ROLLOUT"; }
backup_sums() { printf '%s/predecessor/SHA256SUMS\n' "$ROLLOUT"; }

# The preserved predecessor drop-in for a unit: the LAST recorded entry, so a
# unit upgraded twice resolves to the bytes most recently replaced.
backup_for() {
  local unit=$1 index
  index=$(backup_index)
  [ -f "$index" ] || return 1
  awk -F'\t' -v u="$unit" '$1==u{last=$2} END{if(last)print last}' "$index"
}

# Byte-exact verification of a preserved file: it must exist, match the hash
# recorded when it was taken, and be recorded under THIS unit's path.
verify_backup() {  # unit backup -> 0 ok
  local unit=$1 file=$2 expected actual recorded
  [ -f "$file" ] || { say "   BACKUP MISSING for $unit: $file"; return 1; }
  actual=$(sha256sum "$file" | cut -d' ' -f1)
  recorded=$(awk -v p="$file" '$2==p{print $1}' "$(backup_sums)" 2>/dev/null | tail -1)
  [ -n "$recorded" ] || {
    say "   BACKUP UNRECORDED for $unit: $file has no checksum entry"; return 1; }
  [ "$actual" = "$recorded" ] || {
    say "   BACKUP MODIFIED for $unit: expected $recorded, found $actual"; return 1; }
  # The filename must carry the unit it belongs to: this is the unit->backup
  # correspondence check, so a copied INDEX.tsv cannot point one unit at
  # another's bytes.
  case "$(basename "$file")" in
    "${unit//@/_}-"*) : ;;
    *) say "   BACKUP MISFILED for $unit: $(basename "$file") names another unit"
       return 1 ;;
  esac
  # And the preserved bytes must actually select the predecessor overlay.
  grep -q "PYTHONPATH=$PRED_SLOT" "$file" || {
    say "   BACKUP WRONG TARGET for $unit: does not select $PRED_SLOT"; return 1; }
  printf '%s' "$actual"
  return 0
}

# ------------------------------------------------------------- in-flight work
# Same evidence rule as the activation script: state + heartbeat.
classify_workstreams() {
  PYTHONDONTWRITEBYTECODE=1 FRESH_MINUTES="$FRESH_MINUTES" \
  /opt/turnstone-venv/bin/python - <<'PY' 2>/dev/null || echo "__DB_UNAVAILABLE__"
import os
import tomllib
from datetime import datetime, timezone

ACTIVE = {"running", "thinking", "attention"}
fresh = int(os.environ.get("FRESH_MINUTES", "15"))
now = datetime.now(timezone.utc)
try:
    with open("/etc/turnstone/config.toml", "rb") as fh:
        url = tomllib.load(fh)["database"]["url"].replace(
            "postgresql+psycopg://", "postgresql://")
    import psycopg
    with psycopg.connect(url, connect_timeout=5) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("select ws_id, node_id, state, kind, updated from workstreams")
        rows = cur.fetchall()
except Exception:
    print("__DB_UNAVAILABLE__")
    raise SystemExit(0)

for ws_id, node_id, state, kind, updated in rows:
    age = None
    try:
        ts = datetime.fromisoformat(str(updated))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds() / 60.0
    except Exception:
        pass
    if state not in ACTIVE:
        verdict = "IDLE"
    elif age is None or age > fresh:
        verdict = "AMBIGUOUS"
    else:
        verdict = "EXECUTING"
    print(f"{node_id or 'unknown'}\t{state}\t{'?' if age is None else f'{age:.1f}'}"
          f"\t{verdict}\t{ws_id}\t{kind}")
PY
}

global_idle_preflight() {
  local rows
  rows=$(classify_workstreams)
  if printf '%s' "$rows" | grep -q "__DB_UNAVAILABLE__"; then
    say "IDLE PREFLIGHT: workstream database unreadable; refusing to restart anything"
    return 5
  fi
  say "IDLE PREFLIGHT (global, before any restart)"
  local blocked=0
  while IFS=$'\t' read -r node state age verdict ws kind; do
    [ -n "${node:-}" ] || continue
    case "$verdict" in
      EXECUTING|AMBIGUOUS)
        say "  BLOCKING  $node $state updated ${age}min ws=${ws:0:12} kind=$kind"
        blocked=1 ;;
    esac
  done <<< "$rows"
  if [ "$blocked" -eq 1 ]; then
    say "  Refusing: work is in flight. An advisory sensor is never worth"
    say "  terminating a live workstream. Wait for idle and re-run."
    return 5
  fi
  say "  all services idle"
  return 0
}

unit_idle_check() {  # unit node
  local unit=$1 node=$2 deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while :; do
    local rows hits
    rows=$(classify_workstreams)
    printf '%s' "$rows" | grep -q "__DB_UNAVAILABLE__" && {
      say "   WARN: DB unreadable; cannot recheck $node"; return 0; }
    hits=$(printf '%s\n' "$rows" | awk -F'\t' -v n="$node" \
      '$1==n && ($4=="EXECUTING" || $4=="AMBIGUOUS")')
    [ -z "$hits" ] && { say "   idle recheck OK: $node"; return 0; }
    if [ "$unit" = "$EMERGENCY_UNIT" ]; then
      say "   EMERGENCY: bypassing the idle gate for $unit"
      say "   THIS MAY TERMINATE IN-FLIGHT WORK (recorded)"
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      say "   BUSY at restart time on $node:"
      printf '%s\n' "$hits" | while IFS=$'\t' read -r n st age v ws k; do
        say "     $n $st updated ${age}min ws=${ws:0:12}"
      done
      return 5
    fi
    say "   waiting for $node to become idle (${WAIT_SECONDS}s budget)..."
    sleep 15
  done
}

# ------------------------------------------------------------------- verdicts
health_ok() {  # kind port
  local body
  body=$(curl -s -m 5 "http://127.0.0.1:$2/health" 2>/dev/null) || return 1
  printf '%s' "$body" | grep -q '"status":"ok"' || return 1
  if [ "$1" = "console" ]; then
    printf '%s' "$body" | grep -q '"nodes":5' || return 1
  fi
  return 0
}

wait_health() {  # kind port tries
  local i tries=${3:-40}
  for i in $(seq 1 "$tries"); do health_ok "$1" "$2" && return 0; sleep 1; done
  return 1
}

pid_cmdline() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | sed 's/ $//'; }

pid_is_service() {  # pid unit kind port
  local pid=$1 unit=$2 kind=$3 port=$4 cmd binary
  case "$pid" in ''|0|1|*[!0-9]*) return 1 ;; esac
  [ -d "/proc/$pid" ] || return 1
  cmd=$(pid_cmdline "$pid"); [ -n "$cmd" ] || return 1
  case "$kind" in console) binary=turnstone-console ;; *) binary=turnstone-server ;; esac
  case "$cmd" in *"$binary"*) ;; *) return 1 ;; esac
  case "$cmd" in *"--port $port"*) ;; *) return 1 ;; esac
  grep -q "/${unit}\.service\$" "/proc/$pid/cgroup" 2>/dev/null || return 1
  return 0
}

resolve_service_pid() {  # unit kind port
  local i pid
  for ((i = 0; i < ${READY_TENTHS:-250}; i++)); do
    for pid in $(systemctl show "$1" -p MainPID -p ExecMainPID --value 2>/dev/null); do
      pid_is_service "$pid" "$1" "$2" "$3" && { printf '%s' "$pid"; return 0; }
    done
    sleep 0.1
  done
  return 1
}

# Full post-restart verification against the PREDECESSOR's expected runtime.
verify_on_predecessor() {  # unit kind port servicedir
  local unit=$1 kind=$2 port=$3 sdir=$4 pid envfile pp
  envfile="$ROLLOUT/rollback-${unit//@/_}-$STAMP.environ"
  if ! pid=$(resolve_service_pid "$unit" "$kind" "$port"); then
    say "   FAILED: no provably-executed process for $unit"; return 1
  fi
  tr '\0' '\n' < "/proc/$pid/environ" > "$envfile" 2>/dev/null || {
    say "   FAILED: cannot read /proc/$pid/environ"; return 1; }
  pp=$(sed -n 's/^PYTHONPATH=//p' "$envfile" | head -1)
  [ "$pp" = "$PRED_SLOT" ] || {
    say "   FAILED: pid $pid PYTHONPATH=$pp, expected $PRED_SLOT"; return 1; }
  grep -qx "TURNSTONE_SENSOR_ENABLED=1" "$envfile" || {
    say "   FAILED: sensor not enabled after rollback"; return 1; }
  grep -qx "TURNSTONE_SENSOR_CONTENT_MODE=sanitized" "$envfile" || {
    say "   FAILED: content mode is not sanitized"; return 1; }
  grep -qx "TURNSTONE_SENSOR_STORE_DIR=$PRED_EPOCH/$sdir" "$envfile" || {
    say "   FAILED: store dir is not $PRED_EPOCH/$sdir"; return 1; }
  grep -qx "TURNSTONE_SENSOR_TOKENIZER_DIR=$TOKENIZER" "$envfile" || {
    say "   FAILED: tokenizer dir changed"; return 1; }
  if [ "$kind" = "node" ]; then
    grep -q "^TURNSTONE_NODE_ID=$sdir$" "$envfile" || {
      say "   FAILED: node identity lost (expected $sdir)"; return 1; }
  fi
  if [ "$kind" = "server" ]; then
    grep -qx "TURNSTONE_NODE_ID=turnstone-head-1" "$envfile" || {
      say "   FAILED: head identity lost"; return 1; }
  fi
  if ! wait_health "$kind" "$port"; then
    say "   FAILED: health check on :$port"; return 1
  fi
  say "   verified on predecessor: pid $pid, overlay ${PRED_SLOT##*/}, health ok"
  return 0
}

# ---------------------------------------------------------------- state model
# absent | candidate | predecessor | drift  (echoed)
dropin_state() {  # unit
  local file current_sha backup_sha
  file=$(dropin_path "$1")
  [ -f "$file" ] || { echo absent; return; }
  local backup
  backup=$(backup_for "$1")
  if [ -n "$backup" ] && [ -f "$backup" ]; then
    current_sha=$(sha256sum "$file" | cut -d' ' -f1)
    backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)
    [ "$current_sha" = "$backup_sha" ] && { echo predecessor; return; }
  fi
  # Byte-exact match to the preserved predecessor wins; otherwise decide by the
  # overlay the file selects. A file selecting the CANDIDATE is ours to fix.
  if grep -q "PYTHONPATH=$SLOT" "$file"; then echo candidate; return; fi
  if grep -q "PYTHONPATH=$PRED_SLOT" "$file"; then echo predecessor-other; return; fi
  echo drift
}

report_status() {
  say ""
  say "S6-t2 ROLLBACK STATUS (read-only)"
  say "  unit                 dropin         running-overlay            health"
  local spec unit kind port sdir file state pid overlay health
  for spec in "${UNITS[@]}"; do
    set -- $spec
    unit=$1; kind=$2; port=$3; sdir=$4
    file=$(dropin_path "$unit")
    state=$(dropin_state "$unit")
    pid=$(systemctl show "$unit" -p MainPID --value 2>/dev/null)
    overlay=""
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
      overlay=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null |
                sed -n 's/^PYTHONPATH=//p' | head -1)
    fi
    health_ok "$kind" "$port" && health=ok || health=DOWN
    printf '  %-20s %-14s %-26s %s\n' \
      "$unit" "$state" "${overlay##*/}" "$health" | tee -a "$LOG"
  done
  say ""
  local onpred oncand
  onpred=0; oncand=0
  for spec in "${UNITS[@]}"; do
    set -- $spec
    pid=$(systemctl show "$1" -p MainPID --value 2>/dev/null)
    overlay=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null |
              sed -n 's/^PYTHONPATH=//p' | head -1)
    case "$overlay" in
      "$PRED_SLOT") onpred=$((onpred + 1)) ;;
      "$SLOT") oncand=$((oncand + 1)) ;;
    esac
  done
  say "  on the predecessor: $onpred/6    on the candidate: $oncand/6"
  if [ "$onpred" -eq 6 ]; then
    say "  STATE: fully rolled back"
  elif [ "$oncand" -eq 6 ]; then
    say "  STATE: fully on the candidate (nothing rolled back yet)"
  else
    say "  STATE: PARTIAL - re-run this script to continue; it is resumable"
  fi
}

# --------------------------------------------------------------------- action
rollback_one() {  # unit kind port sdir
  local unit=$1 kind=$2 port=$3 sdir=$4 file backup state
  CURRENT_UNIT=$unit
  file=$(dropin_path "$unit")
  say ""
  say "== $unit"

  state=$(dropin_state "$unit")
  case "$state" in
    predecessor)
      say "   already on the preserved predecessor bytes; verifying, no restart"
      if [ "$DRY_RUN" -eq 1 ]; then say "   [dry-run] would verify only"; return 0; fi
      verify_on_predecessor "$unit" "$kind" "$port" "$sdir" && return 0
      say "   FAILED: file matches the backup but the process does not"
      return 3
      ;;
    predecessor-other)
      say "   drop-in selects the predecessor but is not the preserved bytes:"
      say "   refusing to overwrite a configuration this run did not create."
      say "   Inspect: $file"; return 2
      ;;
    candidate)
      backup=$(backup_for "$unit")
      [ -n "$backup" ] || {
        say "   REFUSING: no preserved predecessor backup recorded for $unit"
        say "   (looked in $(backup_index))"; return 2; }
      if ! verify_output=$(verify_backup "$unit" "$backup"); then
        say "   REFUSING: the preserved backup did not verify"; return 2
      fi
      say "   backup verified byte-exactly: $(basename "$backup")"
      if [ "$DRY_RUN" -eq 1 ]; then
        say "   [dry-run] would idle-check, restore $(basename "$backup") atomically,"
        say "   [dry-run] daemon-reload, restart ONLY $unit, then verify"
        return 0
      fi
      unit_idle_check "$unit" "$sdir" || return 5
      # Atomic restore: write a sibling, fsync, rename over the original.
      local dir tmp
      dir=$(dirname "$file")
      tmp="$dir/.95-sensor.conf.restore.$$"
      install -d -m 0755 -o root -g root "$dir"
      cp -p "$backup" "$tmp"
      chown root:root "$tmp"; chmod 0644 "$tmp"
      sync "$tmp" 2>/dev/null || true
      mv -f "$tmp" "$file"
      cmp -s "$backup" "$file" || {
        say "   FAILED: restored file is not byte-identical to the backup"; return 3; }
      say "   restored $(basename "$file") (byte-identical to the preserved bytes)"
      systemctl daemon-reload
      say "   restarting ONLY $unit"
      systemctl restart "$unit"
      verify_on_predecessor "$unit" "$kind" "$port" "$sdir" || return 3
      say "   ROLLED BACK and verified: $unit"
      return 0
      ;;
    absent)
      say "   no sensor drop-in: this unit is not on the candidate and has no"
      say "   predecessor drop-in to restore. Reported, not silently skipped."
      return 2
      ;;
    drift)
      say "   DRIFT: $file is neither the candidate nor the preserved predecessor."
      say "   Refusing; nothing changed."; return 2
      ;;
  esac
}

# ---------------------------------------------------------------------- main
say "S6-t2 rollback $STAMP (dry-run=$DRY_RUN status=$STATUS_ONLY wait=$WAIT_SECONDS)"
if [ "$STATUS_ONLY" -eq 1 ]; then report_status; exit 0; fi

[ -d "$PRED_SLOT" ] || die "predecessor slot missing: $PRED_SLOT"
[ -d "$PRED_EPOCH" ] || die "predecessor epoch missing: $PRED_EPOCH"
[ -d "$TOKENIZER" ] || die "tokenizer runtime missing: $TOKENIZER"
[ -f "$(backup_index)" ] || die "no recorded upgrades: $(backup_index) is absent"

if [ -n "$ONLY_UNIT" ]; then
  spec_for "$ONLY_UNIT" >/dev/null || die "unknown unit: $ONLY_UNIT"
fi
if [ -n "$EMERGENCY_UNIT" ]; then
  spec_for "$EMERGENCY_UNIT" >/dev/null || die "unknown unit: $EMERGENCY_UNIT"
  set -- $(spec_for "$EMERGENCY_UNIT")
  say ""
  say "EMERGENCY MODE for $EMERGENCY_UNIT"
  say "  Different authority: this bypasses the idle gate for that unit and"
  say "  MAY TERMINATE IN-FLIGHT WORK. It is allowed only when the unit is"
  say "  genuinely unhealthy."
  if health_ok "$1" "$2" && [ "$(systemctl show "$EMERGENCY_UNIT" -p ActiveState --value)" = "active" ]; then
    die "$EMERGENCY_UNIT is healthy and active; emergency rollback is not warranted"
  fi
fi

if [ "$DRY_RUN" -eq 0 ]; then
  global_idle_preflight || exit 5
fi

failures=0
partial=0
for spec in "${ORDER[@]}"; do
  set -- $spec
  unit=$1; kind=$2; port=$3; sdir=$4
  [ -z "$ONLY_UNIT" ] || [ "$unit" = "$ONLY_UNIT" ] || continue
  rollback_one "$unit" "$kind" "$port" "$sdir"
  rc=$?
  case "$rc" in
    0) : ;;
    5) say ""; say "STOPPED: work in flight. Nothing was changed for $unit."
       report_status; exit 5 ;;
    *) failures=$((failures + 1))
       say ""
       say "FAILED for $unit (rc=$rc). Earlier units stay rolled back;"
       say "later units are untouched. This script is resumable."
       report_status
       exit 3 ;;
  esac
done

report_status
say ""
say "Rollback complete for the requested units. Telemetry, credentials,"
say "permissions and every immutable overlay are untouched."
exit 0
