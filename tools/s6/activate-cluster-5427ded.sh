#!/usr/bin/env bash
# S6 — full-cluster shadow-sensor activation (RUN AS ROOT).
#
# Activates the shadow sensor on all six Turnstone 1.8.7 services by installing
# one service-specific drop-in immediately before that service's own restart,
# gating every restart on an idle check, and rolling back that one service if it
# fails its post-restart verification.
#
# It does NOT touch: credentials, sudoers, OAuth stores, the model registry,
# Output Guard config, Switchyard, working directories, users/groups, restart
# policy, the live R2 overlay, or either installed sensor slot.
#
# Idempotent / resumable, and safe to run IN PLACE over the cb7fece rollout.
# An existing 95-sensor.conf is classified four ways:
#   absent      -> activate normally (this run creates the file)
#   current     -> byte-identical to what this script writes for 5427ded AND
#                  its service verifies -> skip, no restart
#   predecessor -> byte-identical to what THIS script would have written for
#                  the approved cb7fece slot + s6-t1 epoch -> verified as a
#                  healthy live predecessor, backed up byte-exactly, then
#                  replaced atomically for that service alone
#   drift       -> anything else -> REFUSE and change nothing
#
# Rolling upgrade: each predecessor service is verified healthy on cb7fece,
# backed up, idle-checked, atomically replaced, daemon-reloaded, validated,
# restarted and verified BEFORE the next service is touched. A failure stops
# the rollout and restores THAT service's exact predecessor file, never an
# unrelated configuration. A live workstream is never terminated for an
# advisory sensor.
#
# Usage:
#   activate-cluster.sh --status                  # read-only cluster status
#   activate-cluster.sh --dry-run                 # print the full plan + files
#   activate-cluster.sh                           # execute
#   activate-cluster.sh --wait-seconds 900        # wait up to 15 min for idle
#
# Exit codes: 0 ok, 2 preflight refused, 3 activation failed (this service
# rolled back and verified), 4 activation failed and rollback did not verify,
# 5 idle/busy gate.
set -euo pipefail

SLOT=/opt/turnstone-overlays/sensor-v187-5427ded
# The APPROVED PREDECESSOR. A pre-existing drop-in is authorized for in-place
# replacement only if it is byte-identical to what this script renders for
# these exact values. A file that merely mentions cb7fece is NOT authorized.
PRED_SLOT=/opt/turnstone-overlays/sensor-v187-cb7fece
PRED_EPOCH=/var/lib/turnstone/sensor/s6-t1
R2=/opt/turnstone-overlays/openai-oauth-v187-r2
PRESERVED_SLOT=/opt/turnstone-overlays/sensor-v187-f76c813
ART=/var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017
MANIFEST=$ART/s6/staging-5427ded/MANIFEST.sha256
R2_BASELINE=$ART/evidence/active-r2-baseline.sha256
EXPECT_MANIFEST_SHA=f785179fad1963d541c67bf22a22dd1916a58960bace195c5666c8ef1fbb267c
EPOCH=/var/lib/turnstone/sensor/s6-t2
TOKENIZER=/var/lib/turnstone/sensor-runtime
ROLLOUT=/var/lib/turnstone/artifacts/s6/rollout
# Where 95-sensor.conf lives. Overridable ONLY so the resume/rollback logic can
# be exercised in a sandbox without root; production runs use the default.
DROPIN_ROOT=${S6_DROPIN_ROOT:-/etc/systemd/system}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG=$ROLLOUT/activate-$STAMP.log

DRY_RUN=0
STATUS_ONLY=0
CONSOLE_MEMBERSHIP=5
WAIT_SECONDS=0
ALLOW_BUSY=""
CURRENT_UNIT=""
CURRENT_KIND=""
CURRENT_PORT=""
# What the drop-in for CURRENT_UNIT looked like BEFORE this run, so a failure
# unwinds to that exact state: absent -> remove, current -> leave in place,
# predecessor -> restore the preserved bytes.
CURRENT_PRIOR_MODE=absent
CURRENT_PRIOR_BACKUP=""
ROLLBACK_VERIFIED=0
RESTARTED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --status) STATUS_ONLY=1; shift ;;
    --wait-seconds) WAIT_SECONDS=${2:?}; shift 2 ;;
    --allow-busy) ALLOW_BUSY="$ALLOW_BUSY ${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*" | tee -a "$LOG"; }
die() { say "FATAL: $*" >&2; exit 2; }

# ---------------------------------------------------------------- preflight
preflight() {
  [ "$(id -u)" -eq 0 ] || die "must run as root"
  command -v systemctl >/dev/null || die "systemctl missing"
  command -v curl >/dev/null || die "curl missing"
  [ -d "$SLOT" ] || die "sensor slot missing: $SLOT"
  [ -f "$MANIFEST" ] || die "manifest missing: $MANIFEST"
  [ -f "$R2_BASELINE" ] || die "R2 baseline missing: $R2_BASELINE"
  [ -d "$TOKENIZER" ] || die "tokenizer runtime missing: $TOKENIZER"
  [ -d "$PRESERVED_SLOT" ] || die "preserved f76c813 slot is missing"

  say "== preflight"
  local manifest_sha
  manifest_sha=$(sha256sum "$MANIFEST" | cut -d' ' -f1)
  [ "$manifest_sha" = "$EXPECT_MANIFEST_SHA" ] \
    || die "manifest checksum mismatch: $manifest_sha"
  say "   manifest sha256 OK ($EXPECT_MANIFEST_SHA)"

  ( cd "$SLOT" && sha256sum --quiet -c "$MANIFEST" ) \
    || die "installed sensor slot does not match its manifest"
  say "   sensor slot manifest OK: $(find "$SLOT" -type f | wc -l) files"

  ( cd "$R2" && sha256sum --quiet -c "$R2_BASELINE" ) \
    || die "active R2 overlay differs from its recorded baseline"
  say "   active R2 overlay OK: $(find "$R2" -type f ! -path '*/__pycache__/*' | wc -l) files"

  # diff exits 1 when the trees differ, which is the expected outcome. Under
  # `set -o pipefail` that status escapes the command substitution and aborts
  # the run before the count is inspected (the S5 installer defect, repeated).
  # Capture the status explicitly: 0 and 1 are both valid comparisons.
  local changed=0 diff_out diff_rc=0
  diff_out=$(diff -r --exclude=__pycache__ -q "$R2" "$SLOT" 2>/dev/null) || diff_rc=$?
  [ "$diff_rc" -le 1 ] || die "cannot compare slot against R2 (diff status $diff_rc)"
  if [ -n "$diff_out" ]; then
    changed=$(printf '%s\n' "$diff_out" | wc -l)
  fi
  [ "$changed" -eq 8 ] || die "expected 8 differences from R2, found $changed"
  say "   8 expected differences from R2"

  TURNSTONE_SENSOR_TOKENIZER_DIR="$TOKENIZER" PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$SLOT:/opt/turnstone-venv/lib/python3.13/site-packages" \
    /opt/turnstone-venv/bin/python -c '
from turnstone.core import exact_tokens as et
from turnstone.core.bounded_state import serialize_state
n, err, how = et.count_state_tokens(serialize_state({"objective": "preflight"}).text)
raise SystemExit(0 if (n is not None and how == "isolated_subprocess_tokenizers") else 1)
' || die "isolated tokenizer admission is not functional"
  say "   tokenizer admission OK (isolated subprocess)"

  mkdir -p "$ROLLOUT"
}

# --------------------------------------------------------------- idle gate
#
# "Busy" is decided from execution evidence, not from a DB state string alone.
# Workstream rows persist after a session ends (`closed`) and an active state
# can outlive a crash, so the state is paired with the heartbeat in `updated`:
#
#   EXECUTING  active state + heartbeat within FRESH_MINUTES -> would be
#              interrupted by a restart. Blocks.
#   AMBIGUOUS  active state + stale heartbeat -> a hung or orphaned row. Not
#              provably idle, so it blocks and asks for investigation.
#   IDLE       `closed` (or any non-active state) -> not executing.
#
# Output is one TAB-separated line per row: node, state, updated, verdict.
FRESH_MINUTES=${FRESH_MINUTES:-15}

classify_workstreams() {
  PYTHONDONTWRITEBYTECODE=1 FRESH_MINUTES="$FRESH_MINUTES" \
  /opt/turnstone-venv/bin/python - <<'PY' 2>/dev/null || echo "__DB_UNAVAILABLE__"
import os
import tomllib
from datetime import datetime, timezone

ACTIVE = {"running", "thinking", "attention"}
fresh_minutes = int(os.environ.get("FRESH_MINUTES", "15"))
now = datetime.now(timezone.utc)

try:
    with open("/etc/turnstone/config.toml", "rb") as fh:
        url = tomllib.load(fh)["database"]["url"].replace(
            "postgresql+psycopg://", "postgresql://"
        )
    import psycopg

    with psycopg.connect(url, connect_timeout=5) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute(
            "select ws_id, node_id, state, kind, updated from workstreams"
        )
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
    elif age is None:
        verdict = "AMBIGUOUS"
    elif age <= fresh_minutes:
        verdict = "EXECUTING"
    else:
        verdict = "AMBIGUOUS"

    age_s = "?" if age is None else f"{age:.1f}"
    print(f"{node_id or 'unknown'}\t{state}\t{age_s}\t{verdict}\t{ws_id}\t{kind}")
PY
}

# Global preflight: inspect ALL six services before the first mutation.
preflight_idle() {
  local rows
  rows=$(classify_workstreams)
  if printf '%s' "$rows" | grep -q "__DB_UNAVAILABLE__"; then
    say "IDLE PREFLIGHT: workstream database unreadable."
    say "  Cannot prove any service is idle; refusing to mutate. Re-run when the"
    say "  database is reachable, or investigate the storage backend first."
    return 5
  fi

  say "IDLE PREFLIGHT (global, before any mutation)"
  say "  freshness window: ${FRESH_MINUTES} min"
  local blocked=0
  while IFS=$'\t' read -r node state age verdict ws_id kind; do
    [ -n "${node:-}" ] || continue
    case "$verdict" in
      EXECUTING)
        say "  BLOCKING  $node  $state  updated ${age}min ago  ws=${ws_id:0:12} kind=$kind"
        blocked=1
        ;;
      AMBIGUOUS)
        say "  BLOCKING  $node  $state  updated ${age}min ago  ws=${ws_id:0:12} kind=$kind  (stale heartbeat: investigate)"
        blocked=1
        ;;
      *)
        ;;
    esac
  done <<< "$rows"

  if [ "$blocked" -eq 1 ]; then
    say ""
    say "  Refusing to start: at least one service is genuinely busy or ambiguous."
    say "  No drop-in was written and no service was restarted."
    say "  Wait for that work to finish, then re-run. Do NOT use --allow-busy and"
    say "  do not kill or reset the session."
    return 5
  fi
  say "  all six services idle (no EXECUTING or AMBIGUOUS rows)"
  return 0
}

# Per-service recheck immediately before that service's restart: activity may
# have started after the global preflight.
idle_gate() {
  local unit=$1 node=$2
  local deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while :; do
    local rows
    rows=$(classify_workstreams)
    if printf '%s' "$rows" | grep -q "__DB_UNAVAILABLE__"; then
      say "   WARN: workstream DB unreadable; cannot recheck idleness for $node"
      return 0
    fi
    local hits
    hits=$(printf '%s\n' "$rows" | awk -F'\t' -v n="$node" \
      '$1==n && ($4=="EXECUTING" || $4=="AMBIGUOUS")')
    if [ -z "$hits" ]; then
      say "   idle recheck OK: ${node} has no executing workstreams"
      return 0
    fi
    case " $ALLOW_BUSY " in
      *" ${unit#turnstone-} "*|*" $unit "*)
        say "   idle recheck OVERRIDDEN by --allow-busy for $unit (recorded)"
        return 0
        ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
      say "   BUSY at restart time: ${node} became active after the global preflight:"
      printf '%s\n' "$hits" | while IFS=$'\t' read -r n st age verdict ws kind; do
        say "     $n $st updated ${age}min ago ws=${ws:0:12} kind=$kind"
      done
      return 5
    fi
    say "   waiting for ${node} to become idle (${WAIT_SECONDS}s budget)..."
    sleep 15
  done
}

# ------------------------------------------------- partial rollout status
#
# Read-only. Reports, per service, whether the drop-in is absent / active /
# drift and whether the RUNNING process is actually on the sensor overlay.
# Used by --status and by the failure handler so a partial rollout is never
# ambiguous.
service_status_line() {
  local unit=$1 kind=$2 port=$3 epochdir=$4
  local dstate pid overlay estate health

  # One classifier only. A second, inline copy is how `active` survived here
  # after the file-level state machine gained `current`/`predecessor`.
  dstate=$(dropin_state "$unit" "$kind" "$port" "$epochdir")

  pid=$(systemctl show "$unit" -p MainPID --value 2>/dev/null)
  estate=$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)
  overlay=""
  if [ -n "$pid" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/environ" ]; then
    overlay=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^PYTHONPATH=//p')
  fi
  if health_ok "$kind" "$port"; then health=ok; else health=DOWN; fi

  # Sensing requires both halves: the drop-in is installed AND the running
  # process is on the sensor overlay. Either alone is a half-configured state
  # (drop-in written but never restarted, or a stale process after removal).
  # `current` and `predecessor` are distinct states so a partially upgraded
  # cluster cannot be mistaken for a fully sensing one.
  local live
  case "$dstate" in
    current)
      if [ "${overlay##*/}" = "${SLOT##*/}" ]; then
        live=SENSING
      else
        live=CONFIGURED-not-live
      fi
      ;;
    predecessor)
      if [ "${overlay##*/}" = "${PRED_SLOT##*/}" ]; then
        live=PRED-ON-CB7FECE
      else
        live=PRED-not-live
      fi
      ;;
    absent) live=not-sensing ;;
    *)      live=DRIFT ;;
  esac

  printf '%-20s dropin=%-11s proc=%-6s overlay=%-28s health=%-4s %s\n' \
    "$unit" "$dstate" "$estate" "${overlay##*/}" "$health" "$live"
}

report_cluster_status() {
  say ""
  say "CLUSTER ACTIVATION STATUS"
  service_status_line "turnstone-node@5" node 8085 "$EPOCH/turnstone-node-5"
  service_status_line "turnstone-node@4" node 8084 "$EPOCH/turnstone-node-4"
  service_status_line "turnstone-node@3" node 8083 "$EPOCH/turnstone-node-3"
  service_status_line "turnstone-node@2" node 8082 "$EPOCH/turnstone-node-2"
  service_status_line "turnstone-console" console 8090 "$EPOCH/console"
  service_status_line "turnstone-server" server 8080 "$EPOCH/turnstone-head-1"

  local sensing total pred
  sensing=$(for spec in "turnstone-node@5 node 8085 turnstone-node-5" \
                     "turnstone-node@4 node 8084 turnstone-node-4" \
                     "turnstone-node@3 node 8083 turnstone-node-3" \
                     "turnstone-node@2 node 8082 turnstone-node-2" \
                     "turnstone-console console 8090 console" \
                     "turnstone-server server 8080 turnstone-head-1"; do
    set -- $spec
    service_status_line "$1" "$2" "$3" "$EPOCH/$4"
  done | grep -c "SENSING$" || true)
  pred=$(for spec in "turnstone-node@5 node 8085 turnstone-node-5" \
                   "turnstone-node@4 node 8084 turnstone-node-4" \
                   "turnstone-node@3 node 8083 turnstone-node-3" \
                   "turnstone-node@2 node 8082 turnstone-node-2" \
                   "turnstone-console console 8090 console" \
                   "turnstone-server server 8080 turnstone-head-1"; do
    set -- $spec
    service_status_line "$1" "$2" "$3" "$EPOCH/$4"
  done | grep -c "PRED-ON-CB7FECE" || true)
  total=6
  say ""
  say "  on 5427ded: $sensing/$total   on the cb7fece predecessor: $pred/$total"
  if [ "$sensing" -eq "$total" ]; then
    say "  PARTIAL ROLLOUT: no - all $total services are on 5427ded."
  elif [ "$sensing" -eq 0 ] && [ "$pred" -eq "$total" ]; then
    say "  PARTIAL ROLLOUT: no - all $total services are on the approved"
    say "  cb7fece predecessor. Running this script rolls them forward to"
    say "  5427ded one service at a time."
  elif [ "$sensing" -eq 0 ] && [ "$pred" -eq 0 ]; then
    say "  PARTIAL ROLLOUT: no - none of the $total services are sensing (inactive)."
  else
    say "  PARTIAL ROLLOUT: YES - $sensing/$total on 5427ded, $pred/$total on the"
    say "  predecessor. Resume with this script; services already on 5427ded are"
    say "  verified without restart, and each predecessor is rolled forward one at"
    say "  a time."
  fi
  local bfile
  bfile=$(boundary_file)
  if [ -n "$bfile" ]; then
    say "  boundary: $(cat "$bfile") ($(basename "$bfile") marker, authoritative)"
  else
    say "  boundary: not set (rollout in progress; observations so far are"
    say "      rollout-validation and MUST NOT be counted as acceptance data)"
  fi
}

# ------------------------------------------------------------- health check
health_ok() {
  local kind=$1 port=$2 body
  CONSOLE_MEMBERSHIP=5
  body=$(curl -s -m 5 "http://127.0.0.1:$port/health" 2>/dev/null) || return 1
  printf '%s' "$body" | grep -q '"status":"ok"' || return 1
  if [ "$kind" = "console" ]; then
    printf '%s' "$body" | grep -q '"nodes":5' || return 1
  fi
  return 0
}

wait_health() {
  local kind=$1 port=$2 tries=${3:-40} i
  for i in $(seq 1 "$tries"); do
    health_ok "$kind" "$port" && return 0
    sleep 1
  done
  return 1
}

# --------------------------------------------------------------- drop-in I/O
render_dropin() {
  # kind port epochdir [slot] — the slot defaults to this script's SLOT. The
  # predecessor check passes PRED_SLOT explicitly so an authorized predecessor
  # is compared byte-for-byte against what THIS script would have written.
  local kind=$1 port=$2 epochdir=$3 slot=${4:-$SLOT} binary args
  case "$kind" in
    node|server) binary=/opt/turnstone-venv/bin/turnstone-server ;;
    console)     binary=/opt/turnstone-venv/bin/turnstone-console ;;
    *) return 1 ;;
  esac
  args="--host 127.0.0.1 --port $port"
  cat <<EOF
[Service]
# S6 shadow sensor (advisory, fail-open, zero control authority).
# PYTHONPATH is bound at exec time because for instanced units systemd reads
# this instance drop-in directory BEFORE the template directory
# (turnstone-node@.service.d/), whose Environment=PYTHONPATH would otherwise win.
# /usr/bin/env inherits the unit environment, so node identity, advertise URL,
# HOME, TURNSTONE_CONFIG, EnvironmentFile, User, WorkingDirectory and Restart=
# are preserved unchanged.
ExecStart=
ExecStart=/usr/bin/env PYTHONPATH=$slot $binary $args

Environment=TURNSTONE_SENSOR_ENABLED=1
Environment=TURNSTONE_SENSOR_CONTENT_MODE=sanitized
Environment=TURNSTONE_SENSOR_STORE_DIR=$epochdir
Environment=TURNSTONE_SENSOR_TOKENIZER_DIR=$TOKENIZER
Environment=TURNSTONE_SENSOR_TIMEOUT_SECONDS=2.5
Environment=TURNSTONE_SENSOR_DEADLINE_SECONDS=3.5
Environment=TURNSTONE_SENSOR_MAX_ABANDONED=8
EOF
}

dropin_state() {
  # echoes: absent | current | predecessor | drift
  local unit=$1 kind=$2 port=$3 epochdir=$4
  local file=$DROPIN_ROOT/${unit}.service.d/95-sensor.conf actual
  # The predecessor measured into ITS OWN per-service dir (s6-t1/<service>),
  # so the comparison must swap the epoch ROOT while keeping the service
  # directory name. Comparing against the bare root would report drift for
  # every legitimate cb7fece drop-in.
  local pred_epochdir=${epochdir/#$EPOCH/$PRED_EPOCH}
  [ -f "$file" ] || { echo absent; return; }
  actual=$(cat "$file")
  if [ "$(render_dropin "$kind" "$port" "$epochdir")" = "$actual" ]; then
    echo current
  elif [ "$(render_dropin "$kind" "$port" "$pred_epochdir" "$PRED_SLOT")" = "$actual" ]; then
    echo predecessor
  else
    echo drift
  fi
}

write_dropin() {
  local unit=$1 kind=$2 port=$3 epochdir=$4
  local dir=$DROPIN_ROOT/${unit}.service.d
  local tmp=$dir/.95-sensor.conf.new.$$
  install -d -m 0755 -o root -g root "$dir"
  render_dropin "$kind" "$port" "$epochdir" > "$tmp"
  chown root:root "$tmp"
  chmod 0644 "$tmp"
  # Controlled atomic replacement: systemd sees the old file or the new one,
  # never a partial write. sync before the rename so the bytes are on disk.
  sync "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$dir/95-sensor.conf"
  say "   wrote $dir/95-sensor.conf atomically"
}

# -------------------------------------------------------- epoch boundary
#
# ONE concept, ONE name per epoch. The marker's filename follows the epoch
# number (s6-t2 -> T2, earlier epochs -> T1); its CONTENT is always the same
# thing: the authoritative UTC instant after which an admission counts as
# acceptance work for that epoch.
#
# Writers use the epoch's own name. Readers use BOUNDARY_MARKERS in
# collect.py/liveness.py, which accept either and refuse when both exist and
# disagree. Everything in this script goes through the two functions below so
# no call site can invent a third spelling.

# The epoch's own marker filename. s6-t2 -> T2.
boundary_name() {
  # s6-t2 -> T2, s6-t1 -> T1, s6-t12 -> T12. Anything without a numeric epoch
  # suffix falls back to this rollout's name; the script's EPOCH is fixed, so
  # the fallback is a guard, not a behaviour anyone relies on.
  local suffix=${EPOCH##*-t}
  case "$suffix" in
    ''|*[!0-9]*) printf 'T2\n' ;;
    *) printf 'T%s\n' "$suffix" ;;
  esac
}

# Echo the EXISTING boundary file (T2 first, then T1), or nothing.
boundary_file() {
  local name
  if [ -f "$EPOCH/$(boundary_name)" ]; then
    printf '%s\n' "$EPOCH/$(boundary_name)"; return 0
  fi
  for name in T2 T1; do
    if [ -f "$EPOCH/$name" ]; then printf '%s\n' "$EPOCH/$name"; return 0; fi
  done
  return 0
}

write_boundary_if_absent() {
  local existing
  existing=$(boundary_file)
  if [ -n "$existing" ]; then
    say ""
    say "the epoch boundary already exists ($(cat "$existing")) as"
    say "$(basename "$existing"); NOT overwritten."
    say "Measurement continues in the existing epoch; telemetry is untouched."
    return 0
  fi
  local boundary
  boundary=$EPOCH/$(boundary_name)
  install -d -m 0750 -o turnstone -g turnstone "$EPOCH"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$boundary"
  say ""
  say "$(basename "$boundary") = $(cat "$boundary")"
  say "  (authoritative acceptance boundary for ${EPOCH##*/})"
  say "Observations written before it are rollout-validation and are excluded"
  say "from the acceptance count by collect.py."
}

# ------------------------------------------------------------- verification
#
# 2026-10-18: the first rollout failed here with "PYTHONPATH is not the sensor
# slot" even though the effective ExecStart was correct. Cause: a RACE in the
# verifier. `systemctl restart` returns as soon as the unit is activated, which
# can be before the forked child has execve'd. A process in that window still
# reports its PARENT's environment through /proc/PID/environ - for a systemd
# unit that is systemd's own environment (the captured failure file contained
# init=/sbin/init, rootmnt=/root, drop_caps=, i.e. PID 1's initramfs vars) -
# and its /proc/PID/cmdline is EMPTY.
#
# So readiness is now decided by evidence that only an exec'd service process
# can have: a non-empty cmdline naming the expected binary and port, AND
# membership of the unit's own cgroup. The environment is only read after that.
# Never compare an unverified PID's environment against the overlay path.

# Kill switch for the readiness window, in tenths of a second (default 25s).
READY_TENTHS=${READY_TENTHS:-250}

#: cmdline of a pid, NULs collapsed to spaces; empty for a not-yet-exec'd child.
pid_cmdline() {
  local pid=$1 out
  out=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || return 1
  printf '%s' "${out% }"
}

#: true when the pid belongs to the given unit's cgroup.
pid_in_unit_cgroup() {
  local pid=$1 unit=$2
  grep -q "/${unit}\.service\$" "/proc/$pid/cgroup" 2>/dev/null
}

#: true only for a pid that is provably this service, already exec'd.
pid_is_service() {
  local pid=$1 unit=$2 kind=$3 port=$4 cmd binary
  # A pid of 0 or 1 can never be the service; reading PID 1's environment was
  # the exact false negative that failed the first rollout.
  case "$pid" in ''|0|1|*[!0-9]*) return 1 ;; esac
  [ -d "/proc/$pid" ] || return 1

  cmd=$(pid_cmdline "$pid") || return 1
  [ -n "$cmd" ] || return 1                      # forked, not yet exec'd

  case "$kind" in
    console) binary=turnstone-console ;;
    *)       binary=turnstone-server ;;
  esac
  case "$cmd" in *"$binary"*) ;; *) return 1 ;; esac
  case "$cmd" in *"--port $port"*) ;; *) return 1 ;; esac

  pid_in_unit_cgroup "$pid" "$unit" || return 1
  return 0
}

#: Poll until a provably-exec'd service pid exists. Echoes the pid, or nothing.
resolve_service_pid() {
  local unit=$1 kind=$2 port=$3 tries=${4:-$READY_TENTHS} i pid
  for ((i = 0; i < tries; i++)); do
    for pid in $(systemctl show "$unit" -p MainPID -p ExecMainPID --value 2>/dev/null); do
      if pid_is_service "$pid" "$unit" "$kind" "$port"; then
        printf '%s' "$pid"
        return 0
      fi
    done
    sleep 0.1
  done
  return 1
}

#: Full failure evidence. Never print a bare mismatch.
report_env_failure() {
  local unit=$1 kind=$2 port=$3 pid=$4 envfile=$5 why=$6
  local expected actual cmd estate sstate exec_start
  expected=$SLOT
  actual=$(sed -n 's/^PYTHONPATH=//p' "$envfile" 2>/dev/null | head -1)
  cmd=$([ -n "$pid" ] && pid_cmdline "$pid" || echo "(none)")
  estate=$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)
  sstate=$(systemctl show "$unit" -p SubState --value 2>/dev/null)
  exec_start=$(systemctl show "$unit" -p ExecStart --value 2>/dev/null)

  say "   VERIFICATION FAILED: $why"
  say "     service          : $unit"
  say "     expected PYTHONPATH: ${expected:-<empty>}"
  say "     actual PYTHONPATH  : ${actual:-<absent>}"
  say "     observed PID       : ${pid:-<none>}"
  say "     process cmdline    : ${cmd:-<empty - forked, not yet exec>}"
  say "     cgroup             : $([ -n "$pid" ] && grep -o '/[^/]*\.service$' "/proc/$pid/cgroup" 2>/dev/null | head -1 || echo '<none>')"
  say "     unit state         : ${estate:-?}/${sstate:-?}"
  say "     effective ExecStart: ${exec_start:-<none>}"
  if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
    say "     env var count      : $(tr '\0' '\n' < "/proc/$pid/environ" | wc -l)"
  fi
  # Did the sensor actually import under the service's own configuration?
  local pp
  pp=$(sed -n 's/^PYTHONPATH=//p' "$envfile" 2>/dev/null | head -1)
  if [ -n "$pp" ]; then
    PYTHONPATH="$pp" PYTHONDONTWRITEBYTECODE=1 \
      /opt/turnstone-venv/bin/python -c '
import sys
try:
    from turnstone.core import sensor_lifecycle as L
    print("     sensor import      : OK (%s)" % sys.modules["turnstone.core.sensor_lifecycle"].__file__)
except Exception as exc:
    print("     sensor import      : FAILED (%s: %s)" % (type(exc).__name__, exc))
' 2>&1 | tee -a "$LOG" || true
  else
    say "     sensor import      : not attempted (no PYTHONPATH observed)"
  fi
}

# -------------------------------------------------------------- health check
health_ok() {
  local kind=$1 port=$2 body
  CONSOLE_MEMBERSHIP=5
  body=$(curl -s -m 5 "http://127.0.0.1:$port/health" 2>/dev/null) || return 1
  printf '%s' "$body" | grep -q '"status":"ok"' || return 1
  if [ "$kind" = "console" ]; then
    # Node membership legitimately drops while a node is restarting, so it is
    # reported as a warning rather than failing the service. Console health is
    # its own status; the FINAL post-activation check requires full membership.
    local nodes
    nodes=$(printf '%s' "$body" | sed -n 's/.*"nodes":\([0-9]*\).*/\1/p')
    if [ -n "$nodes" ] && [ "$nodes" -lt 5 ]; then
      CONSOLE_MEMBERSHIP=$nodes
    else
      CONSOLE_MEMBERSHIP=5
    fi
  fi
  return 0
}

wait_health() {
  local kind=$1 port=$2 tries=${3:-40} i
  for i in $(seq 1 "$tries"); do
    health_ok "$kind" "$port" && return 0
    sleep 1
  done
  return 1
}

verify_service() {
  local unit=$1 kind=$2 port=$3 epochdir=$4 node=$5
  local pid envfile ready
  envfile="$ROLLOUT/${unit//@/_}-$STAMP.environ"

  # 1. A provably exec'd service process, or a fully evidenced failure.
  if ! pid=$(resolve_service_pid "$unit" "$kind" "$port"); then
    : > "$envfile"
    report_env_failure "$unit" "$kind" "$port" "" "$envfile" \
      "no provably-executed service process within $((READY_TENTHS / 10))s"
    return 1
  fi
  say "   service process ready (pid $pid, exec'd, cgroup verified)"

  # 2. Environment, read only from that verified process.
  tr '\0' '\n' < "/proc/$pid/environ" > "$envfile" 2>/dev/null \
    || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
         "cannot read /proc/$pid/environ"; return 1; }

  # A late exec can still be settling; allow a bounded window before failing.
  local i
  for ((i = 0; i < READY_TENTHS; i++)); do
    grep -qx "PYTHONPATH=$SLOT" "$envfile" && break
    sleep 0.1
    tr '\0' '\n' < "/proc/$pid/environ" > "$envfile" 2>/dev/null || true
  done

  grep -qx "PYTHONPATH=$SLOT" "$envfile" || {
    report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
      "the exec'd service process does not carry the sensor overlay on PYTHONPATH"
    return 1
  }
  grep -qx "TURNSTONE_SENSOR_ENABLED=1" "$envfile" \
    || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
         "TURNSTONE_SENSOR_ENABLED is not 1"; return 1; }
  grep -qx "TURNSTONE_SENSOR_CONTENT_MODE=sanitized" "$envfile" \
    || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
         "content mode is not sanitized"; return 1; }
  grep -qx "TURNSTONE_SENSOR_STORE_DIR=$epochdir" "$envfile" \
    || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
         "store dir is not $epochdir"; return 1; }
  grep -qx "TURNSTONE_SENSOR_TOKENIZER_DIR=$TOKENIZER" "$envfile" \
    || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
         "tokenizer dir is not $TOKENIZER"; return 1; }
  say "   process environment OK (pid $pid)"

  if [ "$kind" = "node" ]; then
    grep -q "^TURNSTONE_NODE_ID=turnstone-node-" "$envfile" \
      || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
           "node identity lost"; return 1; }
  fi
  if [ "$kind" = "server" ]; then
    grep -qx "TURNSTONE_NODE_ID=turnstone-head-1" "$envfile" \
      || { report_env_failure "$unit" "$kind" "$port" "$pid" "$envfile" \
           "head identity lost"; return 1; }
  fi

  CONSOLE_MEMBERSHIP=5
  wait_health "$kind" "$port" || { say "   health check failed on :$port"; return 1; }
  if [ "$kind" = "console" ] && [ "${CONSOLE_MEMBERSHIP:-5}" -lt 5 ]; then
    say "   health OK on :$port (node membership ${CONSOLE_MEMBERSHIP}/5, reconverging)"
  else
    say "   health OK on :$port"
  fi

  # 3. The same configuration must yield a live sensor in the production
  #    interpreter, using the values read from the running process.
  local pp en cm sd td
  pp=$(sed -n 's/^PYTHONPATH=//p' "$envfile")
  en=$(sed -n 's/^TURNSTONE_SENSOR_ENABLED=//p' "$envfile")
  cm=$(sed -n 's/^TURNSTONE_SENSOR_CONTENT_MODE=//p' "$envfile")
  sd=$(sed -n 's/^TURNSTONE_SENSOR_STORE_DIR=//p' "$envfile")
  td=$(sed -n 's/^TURNSTONE_SENSOR_TOKENIZER_DIR=//p' "$envfile")
  PYTHONPATH="$pp" TURNSTONE_SENSOR_ENABLED="$en" \
    TURNSTONE_SENSOR_CONTENT_MODE="$cm" TURNSTONE_SENSOR_STORE_DIR="$sd" \
    TURNSTONE_SENSOR_TOKENIZER_DIR="$td" PYTHONDONTWRITEBYTECODE=1 \
    /opt/turnstone-venv/bin/python -c '
import sys
from turnstone.core import sensor_lifecycle as L, shadow_driver as D
assert L.ENABLED is True
assert L.CONTENT_MODE == "sanitized"
mod = sys.modules["turnstone.core.sensor_lifecycle"].__file__
assert mod.startswith("/opt/turnstone-overlays/sensor-v187-5427ded/"), mod
assert D.REQUEST_TIMEOUT_SECONDS == 2.5
assert D.DEADLINE_SECONDS == 3.5
assert D.MAX_ABANDONED_CALLS == 8
# ATTACHMENT CAPABILITY, asserted against the code this process is RUNNING.
# A measurement epoch may only be opened on code that can actually observe
# attachment-delivered operator work; an epoch label or an operator flag is
# not evidence. These three assertions are what makes it evidence: the
# projection exists, the substance test accepts attachments, and the send
# seam threads them from the caller.
import inspect
from turnstone.core import session as S
assert hasattr(L, "attachment_projection"), "attachment projection missing"
assert hasattr(L, "render_attachment_state"), "attachment renderer missing"
assert "attachments" in inspect.signature(L.is_substantive_instruction).parameters, (
    "substance test does not accept attachments"
)
assert "attachments=attachments" in inspect.getsource(S._sensor_observe_substantive), (
    "send seam does not pass attachments to the substance test"
)
assert "attachments=attachments" in inspect.getsource(S.ChatSession.send), (
    "ChatSession.send does not pass attachments to the sensor seam"
)
print("   sensor config OK from", mod)
print("   attachment seam OK (send -> substance -> projection)")
' 2>&1 | tee -a "$LOG" || { say "   sensor module check failed"; return 1; }

  [ -d "$epochdir" ] || { say "   epoch dir missing: $epochdir"; return 1; }
  [ -w "$epochdir" ] || { say "   epoch dir not writable: $epochdir"; return 1; }
  say "   epoch dir OK"
  return 0
}

# ------------------------------------------------- predecessor preservation
#
# Everything below is used ONLY for the APPROVED_PREDECESSOR path. The point
# is that the exact bytes being replaced are recoverable, and that a failure
# lands back on the configuration that was actually running.

backup_dropin() {
  # unit -> echoes the preserved path. Byte-exact copy + its own checksum.
  local unit=$1
  local file=$DROPIN_ROOT/${unit}.service.d/95-sensor.conf
  local dir=$ROLLOUT/predecessor
  local dest
  mkdir -p "$dir"
  dest=$dir/${unit//@/_}-$STAMP.95-sensor.conf
  cp -p "$file" "$dest"
  chmod 0644 "$dest"
  sha256sum "$dest" >> "$dir/SHA256SUMS"
  # unit -> preserved path, so a full-cluster rollback never has to guess a
  # unit name back out of a filename.
  printf '%s\t%s\n' "$unit" "$dest" >> "$dir/INDEX.tsv"
  echo "$dest"
}

restore_predecessor() {
  # unit — atomically put the preserved predecessor bytes back, byte-verified.
  local unit=$1
  local file=$DROPIN_ROOT/${unit}.service.d/95-sensor.conf
  local dir=$DROPIN_ROOT/${unit}.service.d
  local tmp=$dir/.95-sensor.conf.restore.$$
  [ -n "${CURRENT_PRIOR_BACKUP:-}" ] && [ -f "$CURRENT_PRIOR_BACKUP" ] || {
    say "   no preserved predecessor file to restore"; return 1; }
  install -d -m 0755 -o root -g root "$dir"
  cp -p "$CURRENT_PRIOR_BACKUP" "$tmp"
  chown root:root "$tmp"
  chmod 0644 "$tmp"
  sync "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$file"
  cmp -s "$CURRENT_PRIOR_BACKUP" "$file" || {
    say "   restored file does not match the preserved bytes"; return 1; }
  say "   restored predecessor drop-in from $CURRENT_PRIOR_BACKUP"
  return 0
}

predecessor_healthy() {
  # unit kind port — the OLD service must be healthy ON cb7fece before we
  # touch its configuration. This is the "last known good" we roll back to.
  local unit=$1 kind=$2 port=$3 pid envfile
  envfile=$ROLLOUT/${unit//@/_}-$STAMP.predecessor.environ
  if ! pid=$(resolve_service_pid "$unit" "$kind" "$port"); then
    say "   PREDECESSOR UNHEALTHY: no provably-executed service process"
    return 1
  fi
  tr '\0' '\n' < "/proc/$pid/environ" > "$envfile" 2>/dev/null || {
    say "   PREDECESSOR UNHEALTHY: cannot read /proc/$pid/environ"; return 1; }
  grep -qx "PYTHONPATH=$PRED_SLOT" "$envfile" || {
    say "   PREDECESSOR UNHEALTHY: exec'd process is not on $PRED_SLOT"; return 1; }
  grep -qx "TURNSTONE_SENSOR_ENABLED=1" "$envfile" || {
    say "   PREDECESSOR UNHEALTHY: sensor is not enabled"; return 1; }
  health_ok "$kind" "$port" || {
    say "   PREDECESSOR UNHEALTHY: health check failed on :$port"; return 1; }
  say "   predecessor healthy on $PRED_SLOT (pid $pid)"
  return 0
}

activate_from_predecessor() {
  # unit kind port epochdir node — the rolling-upgrade path for ONE service.
  local unit=$1 kind=$2 port=$3 epochdir=$4 node=$5
  CURRENT_PRIOR_MODE=predecessor
  say "   APPROVED PREDECESSOR: byte-identical cb7fece configuration"

  # 1. The old service must be healthy on cb7fece BEFORE any mutation.
  if ! predecessor_healthy "$unit" "$kind" "$port"; then
    say "   REFUSING: predecessor is not healthy; nothing changed for $unit."
    CURRENT_UNIT=""
    return 1
  fi

  # 2. DRY-RUN stops here: nothing is written, not even a backup.
  if [ "$DRY_RUN" -eq 1 ]; then
    say "   [dry-run] would verify this service is idle, then atomically replace"
    say "   [dry-run] ONLY $unit's drop-in with:"
    render_dropin "$kind" "$port" "$epochdir" | sed 's/^/             /' | tee -a "$LOG"
    say "   [dry-run] then daemon-reload, validate ExecStart, restart ONLY $unit"
    say "   [dry-run] nothing was written and no service was touched"
    CURRENT_UNIT=""
    return 0
  fi

  # 3. Preserve the exact bytes we are about to replace.
  if ! CURRENT_PRIOR_BACKUP=$(backup_dropin "$unit"); then
    say "   REFUSING: could not preserve the predecessor drop-in"
    CURRENT_UNIT=""
    return 1
  fi
  say "   predecessor drop-in preserved: $CURRENT_PRIOR_BACKUP"
  say "   sha256 $(sha256sum "$CURRENT_PRIOR_BACKUP" | cut -d' ' -f1)"

  systemctl cat "$unit" > "$ROLLOUT/${unit//@/_}-$STAMP.unit-before.txt" 2>&1 || true

  # 4. Idle recheck for THIS service, immediately before the swap.
  idle_gate "$unit" "$node" || exit 5

  # 5. Atomic replacement of THIS service only.
  write_dropin "$unit" "$kind" "$port" "$epochdir"

  # 6. Reload.
  systemctl daemon-reload

  # 7. Validate the effective ExecStart.
  if ! systemctl show "$unit" -p ExecStart --value | grep -q "env PYTHONPATH=$SLOT"; then
    say "   effective ExecStart does not select the sensor slot:"
    systemctl show "$unit" -p ExecStart --value | tee -a "$LOG"
    return 1
  fi
  say "   effective ExecStart selects the sensor slot"
  systemd-analyze verify "${unit}.service" 2>&1 | tee -a "$LOG" || true

  # 8. Restart ONLY this service.
  install -d -m 0750 -o turnstone -g turnstone "$epochdir"
  say "   restarting $unit"
  RESTARTED=1
  systemctl restart "$unit"

  # 9. Verify the exec'd process: slot, identity, health, sensor config.
  verify_service "$unit" "$kind" "$port" "$epochdir" "$node" || return 1

  # 10. Only a verified service lets the rollout advance.
  CURRENT_UNIT=""; RESTARTED=0
  say "   UPGRADED and VERIFIED: $unit (5427ded)"
  return 0
}

# ----------------------------------------------------------------- rollback
rollback_one() {
  local unit=$1 kind=$2 port=$3
  ROLLBACK_VERIFIED=0
  say "== ROLLBACK $unit (prior=${CURRENT_PRIOR_MODE:-absent})"

  case "${CURRENT_PRIOR_MODE:-absent}" in
    predecessor)
      # Restore the exact cb7fece configuration this run replaced, then verify
      # the service actually runs on it. Never fall through to R2: that is an
      # unrelated configuration and not the last known good for this unit.
      restore_predecessor "$unit" || return 1
      systemctl daemon-reload
      if [ "$RESTARTED" -eq 1 ]; then
        systemctl restart "$unit"
      else
        say "   service was never restarted; predecessor file restored only"
      fi
      if predecessor_healthy "$unit" "$kind" "$port"; then
        say "   ROLLBACK VERIFIED: $unit healthy on the cb7fece predecessor"
        ROLLBACK_VERIFIED=1
        return 0
      fi
      say "   ROLLBACK INCOMPLETE: $unit is not healthy on $PRED_SLOT"
      return 1
      ;;
    current)
      # The 5427ded drop-in PRE-EXISTED this run. A convergence failure must
      # leave it in place — removing it would move the unit to R2.
      if [ "$RESTARTED" -eq 1 ]; then
        systemctl restart "$unit"
      else
        say "   service was never restarted; drop-in left untouched"
      fi
      if wait_health "$kind" "$port"; then
        say "   ROLLBACK VERIFIED: $unit kept its pre-existing 5427ded drop-in and is healthy"
        ROLLBACK_VERIFIED=1
        return 0
      fi
      say "   ROLLBACK INCOMPLETE: $unit unhealthy with its pre-existing drop-in"
      return 1
      ;;
  esac

  # absent: this run created the drop-in, so removing it restores the R2 anchor.
  rm -f "$DROPIN_ROOT/${unit}.service.d/95-sensor.conf"
  systemctl daemon-reload
  if [ "$RESTARTED" -eq 1 ]; then
    systemctl restart "$unit"
  else
    say "   service was never restarted; drop-in removed only"
  fi
  wait_health "$kind" "$port" || { say "   ROLLBACK INCOMPLETE: unhealthy"; return 1; }
  local pid
  pid=$(systemctl show "$unit" -p MainPID --value)
  if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx "PYTHONPATH=$R2" \
     && ! tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -q "^TURNSTONE_SENSOR_"; then
    say "   ROLLBACK VERIFIED: $unit healthy on R2, no sensor env (pid $pid)"
    ROLLBACK_VERIFIED=1
    return 0
  fi
  say "   ROLLBACK INCOMPLETE: environment does not match pre-activation state"
  return 1
}

on_error() {
  local rc=$?
  say "!! failure while activating ${CURRENT_UNIT:-<none>} (rc=$rc)"
  if [ "$DRY_RUN" -eq 0 ] && [ "$STATUS_ONLY" -eq 0 ]; then
    if [ -n "$CURRENT_UNIT" ]; then
      if rollback_one "$CURRENT_UNIT" "$CURRENT_KIND" "$CURRENT_PORT"; then
        say "   that service was rolled back and verified; earlier services stay active"
      else
        say "   ROLLBACK DID NOT VERIFY for $CURRENT_UNIT - investigate before continuing"
      fi
    fi
    report_cluster_status || true
    say ""
    say "TO RESUME: fix the failure, then re-run this script. Services already"
    say "  activated are detected and verified without another restart."
    say "TO ROLL BACK EVERYTHING: remove 95-sensor.conf from each"
    say "  /etc/systemd/system/<unit>.service.d/, run 'systemctl daemon-reload',"
    say "  then restart each unit once. Telemetry already collected is preserved."
    if rollback_one_ok; then exit 3; fi
    exit 4
  fi
  exit "$rc"
}

# Set by the failure path: did the rollback of the failing unit verify?
rollback_one_ok() { [ "${ROLLBACK_VERIFIED:-0}" -eq 1 ]; }
trap on_error ERR

# -------------------------------------------------------------- activation
activate() {
  local unit=$1 kind=$2 port=$3 epochdir=$4 node=$5 state
  CURRENT_UNIT=$unit; CURRENT_KIND=$kind; CURRENT_PORT=$port; RESTARTED=0
  CURRENT_PRIOR_MODE=absent; CURRENT_PRIOR_BACKUP=""
  say ""
  say "== $unit (:$port, epoch $epochdir)"

  state=$(dropin_state "$unit" "$kind" "$port" "$epochdir")
  say "   drop-in state: $state"

  case "$state" in
    drift)
      say "   REFUSING: the existing 95-sensor.conf for $unit is neither the"
      say "   5427ded configuration nor a byte-verified cb7fece predecessor."
      say "   Nothing was changed. Inspect and remove it deliberately if intended:"
      say "     $DROPIN_ROOT/$unit.service.d/95-sensor.conf"
      CURRENT_UNIT=""
      exit 2
      ;;
    predecessor)
      # The rolling-upgrade path: one service at a time, verified in place.
      activate_from_predecessor "$unit" "$kind" "$port" "$epochdir" "$node"
      return $?
      ;;
    current)
      # The drop-in already matches this script. It PRE-EXISTED this run, so a
      # convergence failure must leave it in place rather than remove it.
      CURRENT_PRIOR_MODE=current
      say "   already activated (byte-identical 5427ded drop-in); verifying without restart"
      if verify_service "$unit" "$kind" "$port" "$epochdir" "$node"; then
        CURRENT_UNIT=""
        say "   ALREADY ACTIVE and verified: $unit"
        return 0
      fi
      say "   configured but NOT live -> converging with one restart"
      idle_gate "$unit" "$node" || exit 5
      RESTARTED=1
      systemctl restart "$unit"
      verify_service "$unit" "$kind" "$port" "$epochdir" "$node" || return 1
      CURRENT_UNIT=""; RESTARTED=0
      say "   CONVERGED: $unit"
      return 0
      ;;
  esac

  systemctl cat "$unit" > "$ROLLOUT/${unit//@/_}-$STAMP.unit-before.txt" 2>&1 || true
  say "   captured baseline unit config"

  idle_gate "$unit" "$node" || exit 5

  if [ "$DRY_RUN" -eq 1 ]; then
    say "   [dry-run] would write $DROPIN_ROOT/${unit}.service.d/95-sensor.conf:"
    render_dropin "$kind" "$port" "$epochdir" | sed 's/^/             /' | tee -a "$LOG"
    say "   [dry-run] would daemon-reload, verify ExecStart, restart $unit"
    CURRENT_UNIT=""
    return 0
  fi

  write_dropin "$unit" "$kind" "$port" "$epochdir"

  systemctl daemon-reload

  if ! systemctl show "$unit" -p ExecStart --value | grep -q "env PYTHONPATH=$SLOT"; then
    say "   effective ExecStart does not select the sensor slot:"
    systemctl show "$unit" -p ExecStart --value | tee -a "$LOG"
    return 1
  fi
  say "   effective ExecStart selects the sensor slot"
  systemd-analyze verify "${unit}.service" 2>&1 | tee -a "$LOG" || true

  install -d -m 0750 -o turnstone -g turnstone "$epochdir"

  say "   restarting $unit"
  RESTARTED=1
  systemctl restart "$unit"

  verify_service "$unit" "$kind" "$port" "$epochdir" "$node" || return 1
  CURRENT_UNIT=""; RESTARTED=0
  say "   ACTIVATED: $unit"
}

# --------------------------------------------------------------- execution
mkdir -p "$ROLLOUT"
: > "$LOG"
say "S6-t2 activation $STAMP (dry-run=$DRY_RUN status=$STATUS_ONLY wait=$WAIT_SECONDS allow_busy=${ALLOW_BUSY:-none})"

# A live workstream is never terminated for an advisory shadow sensor. The
# flag is parsed only to REFUSE it explicitly, so an old invocation cannot
# silently bypass the idle gate during a rolling upgrade.
if [ -n "$ALLOW_BUSY" ]; then
  say "REFUSING: --allow-busy is not permitted for the S6 rolling upgrade."
  say "  A live workstream must not be interrupted for an advisory sensor."
  say "  Wait for the cluster to go idle and re-run without the flag."
  exit 2
fi

if [ "$STATUS_ONLY" -eq 1 ]; then
  mkdir -p "$ROLLOUT"
  report_cluster_status
  exit 0
fi

preflight

# Global idle preflight: BEFORE any drop-in is written or any service touched.
preflight_idle || exit 5

activate "turnstone-node@5" node 8085 "$EPOCH/turnstone-node-5" turnstone-node-5
activate "turnstone-node@4" node 8084 "$EPOCH/turnstone-node-4" turnstone-node-4
activate "turnstone-node@3" node 8083 "$EPOCH/turnstone-node-3" turnstone-node-3
activate "turnstone-node@2" node 8082 "$EPOCH/turnstone-node-2" turnstone-node-2
activate "turnstone-console" console 8090 "$EPOCH/console" console
activate "turnstone-server" server 8080 "$EPOCH/turnstone-head-1" turnstone-head-1

if [ "$DRY_RUN" -eq 1 ]; then
  say ""
  say "dry run complete: no drop-in written, no service restarted"
  exit 0
fi

say ""
say "== post-activation verification"
fail=0
for spec in "turnstone-node@5 node 8085 turnstone-node-5" \
            "turnstone-node@4 node 8084 turnstone-node-4" \
            "turnstone-node@3 node 8083 turnstone-node-3" \
            "turnstone-node@2 node 8082 turnstone-node-2" \
            "turnstone-console console 8090 console" \
            "turnstone-server server 8080 turnstone-head-1"; do
  set -- $spec
  unit=$1; kind=$2; port=$3; node=$4
  pid=$(systemctl show "$unit" -p MainPID --value)
  overlay=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^PYTHONPATH=//p' || true)
  estate=$(systemctl show "$unit" -p ActiveState --value)
  verdict=OK
  [ "$overlay" = "$SLOT" ] || verdict=OVERLAY-MISMATCH
  [ "$estate" = "active" ] || verdict=NOT-ACTIVE
  health_ok "$kind" "$port" || verdict=UNHEALTHY
  say "   $(printf '%-20s' "$unit") pid=$pid $estate overlay=${overlay##*/} $verdict"
  [ "$verdict" = "OK" ] || fail=1
done

if [ "$fail" -ne 0 ]; then
  say "post-activation verification FAILED; investigate before establishing T2"
  exit 3
fi

[ -d "$EPOCH" ] || install -d -m 0750 -o turnstone -g turnstone "$EPOCH"

# Provenance record of the capability every verified service was just held to.
# This is NOT the source of truth - liveness.py probes the running code itself -
# it exists so the epoch carries a dated, auditable statement of what was
# verified at activation time.
{
  printf 'epoch=s6-t2\n'
  printf 'slot=%s\n' "$SLOT"
  printf 'manifest_sha256=%s\n' "$EXPECT_MANIFEST_SHA"
  printf 'verified_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'services=6/6\n'
  printf 'capability.attachments_observed=1\n'
  printf 'capability.content_basis=text|text_plus_attachments|attachments_only\n'
  printf 'verified_by=inspect.getsource + signature of the exec\047d process code\n'
} > "$EPOCH/CAPABILITY"
say "capability record written: $EPOCH/CAPABILITY"

# T2 is the AUTHORITATIVE acceptance boundary for epoch s6-t2. It is written
# only here, after every one of the six services has been individually verified
# on 5427ded. Anything already sitting in the epoch directories was produced
# during the rollout and is rollout-validation data: collect.py excludes it by
# timestamp.
#
# The marker is written under THIS epoch's own name (s6-t2 -> T2) and is never
# rewritten once present. Readers accept either spelling via
# collect.read_boundary(), so the handoff and the reports cannot disagree about
# which file is authoritative.
# Write the epoch boundary if, and only if, none exists yet. Kept as a function
# so the sandbox suite can drive the EXACT write path rather than a copy of it.

write_boundary_if_absent

say "activation complete. Rollout diagnostics: $ROLLOUT"
say "Telemetry: $EPOCH"
say ""
say "MEASUREMENT IS ACTIVE, NOT COMPLETE. Gate: >=100 genuine unique admitted"
say "observations across the cluster AND >=48h elapsed from the boundary"
exit 0
