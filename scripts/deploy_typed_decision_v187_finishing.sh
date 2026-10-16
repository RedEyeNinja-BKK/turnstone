#!/usr/bin/env bash
# Second-pass Turnstone 1.8.7 typed-decision overlay.
# Keeps the already active first-pass overlay and 90-*.conf rollback anchor.
# stage: root-owned copy, root-only DB/config snapshot; no service changes.
# activate: explicit operator-go, install later-priority 91-*.conf, restart server.
# rollback: remove only 91-*.conf to restore first-pass overlay.
set -Eeuo pipefail
umask 077
PY=/opt/turnstone-venv/bin/python
OLD=/opt/turnstone-overlays/typed-decision-v187
NEW=/opt/turnstone-overlays/typed-decision-v187-finish
OLD_DROPIN=/etc/systemd/system/turnstone-server.service.d/90-typed-decision-v187.conf
NEW_DROPIN=/etc/systemd/system/turnstone-server.service.d/91-typed-decision-v187-finish.conf
BACKUP=/var/backups/turnstone/typed-decision-v187-finish-20261016
die() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
usage() { die "Usage: sudo bash $0 {stage PATH_TO_FINISH_CANDIDATE|activate|status|rollback}"; }
[[ $# -ge 1 && "$EUID" -eq 0 ]] || usage
[[ -x "$PY" ]] || die "No installed venv interpreter"
[[ "$("$PY" -c 'import importlib.metadata as m; print(m.version("turnstone"))')" == "1.8.7" ]] ||
  die "The installed package is not upstream Turnstone 1.8.7"
[[ -f "$OLD_DROPIN" ]] || die "First-pass rollback override is missing"
grep -Fxq "Environment=\"PYTHONPATH=$OLD\"" "$OLD_DROPIN" ||
  die "First-pass rollback override does not match expected contents"
[[ -f "$OLD/turnstone/core/typed_decision.py" ]] ||
  die "First-pass overlay missing, cannot guarantee rollback"

active_overlay() {
  local pid
  pid="$(systemctl show -p MainPID --value turnstone-server.service)"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | grep -Fx "PYTHONPATH=$1" >/dev/null
}
health_ok() {
  curl -fsS --max-time 15 http://127.0.0.1:8080/health >/dev/null
}
status() {
  echo "Server: $(systemctl is-active turnstone-server.service || true)"
  if active_overlay "$NEW"; then echo "Overlay: FINISH (new candidate)"
  elif active_overlay "$OLD"; then echo "Overlay: FIRST PASS (rollback anchor)"
  else echo "Overlay: UNKNOWN, inspect service before modifying"; fi
  if health_ok; then echo "Health: OK"; else echo "Health: FAILED"; fi
}

revert_to_first_pass() {
  echo "Removing ONLY second-pass override; retaining first-pass overlay" >&2
  rm -f -- "$NEW_DROPIN"
  systemctl daemon-reload
  systemctl restart turnstone-server.service || true
  sleep 3
}
case "$1" in
  status)
    [[ $# == 1 ]] || usage
    status
    ;;
  stage)
    [[ $# == 2 ]] || usage
    active_overlay "$OLD" || die "First-pass overlay is not running; stop and investigate"
    health_ok || die "Live server is not healthy"
    CANDIDATE="$(realpath -- "$2")"
    [[ -f "$CANDIDATE/turnstone/core/tool_advisory.py" ]] ||
      die "Candidate missing modified tool_advisory"
    [[ ! -e "$NEW" && ! -e "$NEW_DROPIN" && ! -e "$BACKUP" ]] ||
      die "Second-pass slot/override/backup already exists; refusing overwrite"
    grep -Fq '34 passed, 0 failed, 34 total' "$CANDIDATE/regression.log" ||
      die "34/34 standalone regression receipt missing"
    grep -Fq 'MUTATION BATTERY: PASS' "$CANDIDATE/mutation.log" ||
      die "10/10 mutation receipt missing"
    grep -Fq 'LIVE COMPARATIVE CALIBRATION: PASS' "$CANDIDATE/calibration.log" ||
      die "Live comparison not accepted: do not activate"
    TMP="$(mktemp -d /opt/turnstone-overlays/.typed-decision-v187-finish.XXXXXXXX)"
    trap 'if [[ -n "$TMP" && -d "$TMP" ]]; then rm -rf -- "$TMP"; fi' EXIT
    cp -a -- "$CANDIDATE/turnstone" "$TMP/turnstone"
    chown -R root:root "$TMP"
    chmod 0755 "$TMP"
    for path in typed_decision.py output_guard_judge.py tool_advisory.py; do
      cmp -- "$CANDIDATE/turnstone/core/$path" "$TMP/turnstone/core/$path" ||
        die "Copy mismatch: $path"
    done
    runuser -u turnstone -- env HOME=/var/lib/turnstone PYTHONPATH="$TMP" "$PY" -c \
      'import pathlib,turnstone.core.typed_decision as d,turnstone.core.output_guard_judge as g,turnstone.core.tool_advisory as a; x=pathlib.Path(d.__file__).resolve().parents[2]; assert all(pathlib.Path(m.__file__).resolve().parents[2]==x for m in (g,a)); print("Service-user imports OK")'
    install -d -o root -g root -m 0700 "$BACKUP"
    install -m 0600 /etc/turnstone/config.toml "$BACKUP/config.toml"
    systemctl cat turnstone-server.service > "$BACKUP/server-unit.before.txt"
    runuser -u postgres -- pg_dump -Fc -d turnstone > "$BACKUP/turnstone.before.pgcustom" ||
      die "Database backup failed"
    chmod 0600 "$BACKUP/"*
    mv -- "$TMP" "$NEW"
    TMP=""
    trap - EXIT
    echo "STAGED: $NEW"
    echo "Root-only backup: $BACKUP"
    echo "Live first-pass deployment unchanged. Nothing activated."
    ;;
  activate)
    [[ $# == 1 ]] || usage
    [[ -d "$NEW/turnstone" && -s "$BACKUP/turnstone.before.pgcustom" ]] ||
      die "Second-pass stage / backup missing"
    [[ ! -e "$NEW_DROPIN" ]] || die "Second-pass override exists already"
    active_overlay "$OLD" || die "First-pass overlay must be active immediately before activation"
    health_ok || die "Current server unhealthy"
    echo "This restarts ONLY turnstone-server and may interrupt live workstreams."
    read -r -p "Type ACTIVATE-FINISH to continue: " reply
    [[ "$reply" == "ACTIVATE-FINISH" ]] || die "No operator confirmation"
    printf '[Service]\nEnvironment="PYTHONPATH=%s"\n' "$NEW" > "$NEW_DROPIN"
    chmod 0644 "$NEW_DROPIN"
    systemctl daemon-reload
    if ! systemctl restart turnstone-server.service; then
      revert_to_first_pass
      die "Restart failed; tried restoring first-pass overlay"
    fi
    sleep 4
    if ! active_overlay "$NEW" || ! health_ok; then
      revert_to_first_pass
      die "Second-pass health/environment verification failed; restored old override"
    fi
    echo "SECOND-PASS OVERLAY ACTIVE. First-pass rollback anchor retained."
    status
    ;;
  rollback)
    [[ $# == 1 ]] || usage
    [[ -f "$NEW_DROPIN" ]] || die "No second-pass override to remove"
    grep -Fxq "Environment=\"PYTHONPATH=$NEW\"" "$NEW_DROPIN" ||
      die "Override content differs from expected; refusing removal"
    echo "Rollback preserves all model settings, admin rights, old overlay, and original venv."
    read -r -p "Type ROLLBACK-FINISH to continue: " reply
    [[ "$reply" == "ROLLBACK-FINISH" ]] || die "No operator confirmation"
    revert_to_first_pass
    active_overlay "$OLD" && health_ok ||
      die "Rollback restart did not restore healthy first-pass overlay; investigate"
    echo "FIRST-PASS OVERLAY RESTORED"
    status
    ;;
  *) usage ;;
esac
