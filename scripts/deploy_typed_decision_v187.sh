#!/usr/bin/env bash
# Reversible Turnstone 1.8.7 typed-decision package overlay.
# Only modifies the turnstone-server service at explicit activation.
# Never changes model rows, roles, user permissions or the original venv.
set -Eeuo pipefail
umask 077
SLOT=/opt/turnstone-overlays/typed-decision-v187
DROPIN=/etc/systemd/system/turnstone-server.service.d/90-typed-decision-v187.conf
BACKUP=/var/backups/turnstone/typed-decision-v187-20261016
PY=/opt/turnstone-venv/bin/python
die() { echo "REFUSED: $*" >&2; exit 1; }
usage() { die "Usage: sudo bash $0 {stage PATH_TO_TESTED_CANDIDATE|activate|status|rollback}"; }
[[ $# -ge 1 ]] || usage
[[ $EUID == 0 ]] || die "Run as root through sudo"
[[ -x "$PY" ]] || die "Missing $PY"
ver="$("$PY" -c 'import importlib.metadata as m; print(m.version("turnstone"))')"
[[ "$ver" == 1.8.7 ]] || die "Expected installed turnstone 1.8.7, found $ver"

status() {
  echo "Server: $(systemctl is-active turnstone-server.service || true)"
  local pid
  pid="$(systemctl show --value -p MainPID turnstone-server.service)"
  echo "MainPID: $pid"
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && [[ -r "/proc/$pid/environ" ]]; then
    if tr '\0' '\n' <"/proc/$pid/environ" | grep -Fxq "PYTHONPATH=$SLOT"; then
      echo "Candidate package: ACTIVE"
    else
      echo "Candidate package: INACTIVE (upstream venv)"
    fi
  fi
  curl -fsS --max-time 15 http://127.0.0.1:8080/health || true
  echo
}
runtime_rollback() {
  echo "Restoring original upstream service package path..." >&2
  rm -f -- "$DROPIN"
  systemctl daemon-reload
  systemctl restart turnstone-server.service || true
}
case "$1" in
  stage)
    [[ $# == 2 ]] || usage
    CANDIDATE="$(realpath -- "$2")"
    [[ -d "$CANDIDATE/turnstone" ]] || die "Missing candidate directory"
    [[ ! -e "$SLOT" && ! -e "$DROPIN" && ! -e "$BACKUP" ]] ||
      die "Slot, deployment override, or backup already exists; do not overwrite"
    grep -Fq '31 passed, 0 failed, 31 total' "$CANDIDATE/regression.log" ||
      die "Missing 31/31 regression receipt"
    grep -Fq 'MUTATION BATTERY: PASS' "$CANDIDATE/mutation.log" ||
      die "Missing successful mutation receipt"
    if systemctl show -p Environment --value turnstone-server.service | grep -q 'PYTHONPATH='; then
      die "Existing server PYTHONPATH; review before changing package path"
    fi
    install -d -o root -g root -m 0755 /opt/turnstone-overlays
    TMP="$(mktemp -d /opt/turnstone-overlays/.typed-decision-v187.XXXXXXXX)"
    trap 'if [[ -n "$TMP" && -d "$TMP" ]]; then rm -rf -- "$TMP"; fi' EXIT
    cp -a -- "$CANDIDATE/turnstone" "$TMP/turnstone"
    chown -R root:root "$TMP"
    chmod 0755 "$TMP"
    cmp "$CANDIDATE/turnstone/core/typed_decision.py" "$TMP/turnstone/core/typed_decision.py" ||
      die "Candidate adapter copy mismatch"
    cmp "$CANDIDATE/turnstone/core/output_guard_judge.py" "$TMP/turnstone/core/output_guard_judge.py" ||
      die "Candidate dispatcher copy mismatch"
    runuser -u turnstone -- env HOME=/var/lib/turnstone PYTHONPATH="$TMP" "$PY" -c \
      'import pathlib,turnstone.core.typed_decision as d,turnstone.core.output_guard_judge as g; assert pathlib.Path(d.__file__).parents[2] == pathlib.Path(g.__file__).parents[2]; print("Service-user imports OK")'
    install -d -o root -g root -m 0700 "$BACKUP"
    install -m 0600 /etc/turnstone/config.toml "$BACKUP/config.toml"
    systemctl cat turnstone-server.service > "$BACKUP/server-unit.before.txt"
    # Root-only backup, includes DB secrets. Never send/commit it.
    runuser -u postgres -- pg_dump -Fc -d turnstone > "$BACKUP/turnstone.before.pgcustom" ||
      die "PostgreSQL backup failed"
    chmod 0600 "$BACKUP/"*
    mv -- "$TMP" "$SLOT"
    TMP=""
    trap - EXIT
    echo "STAGED: $SLOT"
    echo "ROLLBACK MATERIAL: $BACKUP (root-only)"
    echo "No running service, registry, or user permissions changed."
    ;;
  activate)
    [[ $# == 1 ]] || usage
    [[ -d "$SLOT/turnstone" ]] || die "Run stage first"
    [[ -s "$BACKUP/turnstone.before.pgcustom" ]] || die "Missing database snapshot"
    [[ ! -e "$DROPIN" ]] || die "Deployment override already exists"
    if systemctl show -p Environment --value turnstone-server.service | grep -q 'PYTHONPATH='; then
      die "Existing server PYTHONPATH; refuse conflicting override"
    fi
    echo "This will restart only turnstone-server; existing live workstreams may be interrupted."
    read -r -p 'Type ACTIVATE to proceed: ' choice
    [[ "$choice" == ACTIVATE ]] || die "No operator confirmation"
    install -d -o root -g root -m 0755 /etc/systemd/system/turnstone-server.service.d
    printf '[Service]\nEnvironment="PYTHONPATH=%s"\n' "$SLOT" > "$DROPIN"
    chmod 0644 "$DROPIN"
    systemctl daemon-reload
    if ! systemctl restart turnstone-server.service; then
      runtime_rollback
      die "Restart failed"
    fi
    sleep 4
    pid="$(systemctl show --value -p MainPID turnstone-server.service)"
    if ! [[ "$pid" =~ ^[1-9][0-9]*$ ]] ||
      ! tr '\0' '\n' <"/proc/$pid/environ" | grep -Fxq "PYTHONPATH=$SLOT" ||
      ! curl -fsS --max-time 15 http://127.0.0.1:8080/health >/dev/null; then
      runtime_rollback
      die "Overlay activation failed verification; original service path restored"
    fi
    echo "OVERLAY ACTIVATED; the original upstream 1.8.7 installation is unchanged."
    status
    ;;
  rollback)
    [[ $# == 1 ]] || usage
    [[ -f "$DROPIN" ]] || die "No deployment override found"
    grep -Fxq "Environment=\"PYTHONPATH=$SLOT\"" "$DROPIN" ||
      die "Override does not match ours: refuse removal"
    echo "This restores original Python code only. It does not silently rewrite model settings."
    echo "When disabling the typed adapter, first disable judge.output_guard_llm in console."
    read -r -p 'Type ROLLBACK to proceed: ' choice
    [[ "$choice" == ROLLBACK ]] || die "No operator confirmation"
    runtime_rollback
    status
    ;;
  status)
    [[ $# == 1 ]] || usage
    status
    ;;
  *) usage ;;
esac
