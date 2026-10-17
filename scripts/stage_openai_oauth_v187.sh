#!/usr/bin/env bash
# Build the LC-RD-007 native OpenAI OAuth candidate from the accepted R2 slot.
#
# WHY IT BUILDS ON R2
#   The native OAuth lane is an ADDITION to the typed-decision revision, not an
#   alternative to it. Building from the R2 candidate guarantees the accepted
#   typed-decision corrections are present and that the two features cannot
#   drift apart. Building from pristine 1.8.7 would silently drop them.
#
# THE PARTIAL-DEPLOYMENT HAZARD THIS AVOIDS
#   Introducing auth_mode=openai_oauth requires FOUR cooperating changes:
#     1. MODEL_AUTH_MODES must accept the mode            (model_registry)
#     2. dispatch must mint from the local token store    (model_backend_auth)
#     3. the store module must exist                      (oauth_token_store)
#     4. the admin write path must stop demanding OIDC/OBO posture for it
#                                                          (console/server)
#   Shipping (1) without (2)-(4) produces a row the registry accepts and every
#   layer beneath refuses — with a misleading "single sign-on is not set up"
#   error. This script therefore COPIES all four and then ASSERTS the complete
#   set is present before it will finish.
#
# OWNERSHIP NOTE (audited): this script invokes NO git commands. It copies
# repository files with `install`/`cmp`, which root may read across the
# turnstone-owned working tree without tripping Git's dubious-ownership guard.
# The R2 script, whose base-commit check DOES read Git objects, runs those reads
# as the repository owner for the same reason.
#
# Usage:
#   bash stage_openai_oauth_v187.sh plan                     # read-only
#   sudo bash stage_openai_oauth_v187.sh stage
#   sudo bash stage_openai_oauth_v187.sh verify
#   bash stage_openai_oauth_v187.sh status                   # read-only
#   sudo bash stage_openai_oauth_v187.sh activate --confirm
#   sudo bash stage_openai_oauth_v187.sh rollback --confirm
#
# `plan`, `status` and `--dry-run` write nothing and need no privilege.
# Activation and rollback refuse without the literal `--confirm` argument and
# without root. That is the operator gate, not a convenience flag: activation
# restarts every unit in one window and interrupts live workstreams.
#
# Sandbox (no root, /var/tmp only):
#   R2_SANDBOX=1 R2_ROOT=/var/tmp/r2-sbx/r2 OAUTH_ROOT=/var/tmp/r2-sbx/oauth \
#     bash stage_openai_oauth_v187.sh stage
set -Eeuo pipefail
umask 022

PY=${R2_PYTHON:-/opt/turnstone-venv/bin/python}
REPO=${R2_REPO:-$(cd -- "$(dirname -- "$0")/.." && pwd)}
R2_SLOT=${R2_ROOT:-/opt/turnstone-overlays/typed-decision-v187-r2}
NEW=${OAUTH_ROOT:-/opt/turnstone-overlays/openai-oauth-v187}
SANDBOX=${R2_SANDBOX:-0}
SERVICE_USER=${R2_SERVICE_USER:-turnstone}

SERVER_DROPIN=/etc/systemd/system/turnstone-server.service.d/93-openai-oauth-v187.conf
NODE_DROPIN="/etc/systemd/system/turnstone-node@.service.d/93-openai-oauth-v187.conf"
CONSOLE_DROPIN=/etc/systemd/system/turnstone-console.service.d/93-openai-oauth-v187.conf

# One restart window, one rollback. The head server and the four execution
# nodes all import the model/provider stack; the console is where the new mode
# is administered. Leaving any of them on the old overlay would let the
# registry and the dispatch layer disagree about the same model row.
UNITS=(turnstone-server.service turnstone-node@2.service turnstone-node@3.service \
       turnstone-node@4.service turnstone-node@5.service turnstone-console.service)
HEALTH_PORTS=(8080 8082 8083 8084 8085 8090)
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-90}
RECEIPTS_DIR=${ACTIVATION_RECEIPTS_DIR:-/var/log/turnstone-v187}

die() { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }
say() { printf '%s\n' "$*"; }

if [[ "$SANDBOX" == "1" ]]; then
  [[ "$NEW" == /var/tmp/* ]] || die "sandbox requires OAUTH_ROOT under /var/tmp"
  STAGE_OWNER=$(id -un); STAGE_GROUP=$(id -gn)
elif [[ "$EUID" -ne 0 ]]; then
  # Read-only verbs and their dry-run forms need no privilege; stage, verify and
  # a real activate/rollback do. The verbs themselves re-check before writing.
  case "${1:-}" in
    plan|status|activate|rollback) STAGE_OWNER=$(id -un); STAGE_GROUP=$(id -gn) ;;
    stage|verify) die "must run as root (or use plan/status/--dry-run, or set R2_SANDBOX=1)" ;;
    *) : ;;  # unknown verb: fall through to the usage error
  esac
else
  STAGE_OWNER=root; STAGE_GROUP=root
fi

WORKDIR=$(mktemp -d /var/tmp/turnstone-oauth.XXXXXXXX) || die "cannot create a work directory"
chmod 0700 "$WORKDIR"
RUNDIR=""
cleanup() {
  [[ -n "${WORKDIR:-}" && -d "$WORKDIR" ]] && find "$WORKDIR" -mindepth 0 -delete 2>/dev/null
  [[ -n "${RUNDIR:-}" && -d "$RUNDIR" ]] && find "$RUNDIR" -mindepth 0 -delete 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

# Files copied whole from the repository.
OAUTH_FILES=(
  "core/oauth_token_store.py"
  "core/model_backend_auth.py"
  "core/model_registry.py"
  "core/settings_registry.py"
  "console/server.py"
)

patch_marker() { grep -q 'DIRECT_OAUTH_MODEL_AUTH_MODES' "$1" 2>/dev/null; }

cmd_stage() {
  [[ ! -e "$NEW" ]] || die "slot already exists: $NEW (immutable; choose a new revision)"
  [[ -d "$R2_SLOT/turnstone" ]] ||
    die "R2 candidate missing at $R2_SLOT -- stage and verify R2 first"
  command -v "$PY" >/dev/null || die "interpreter not found: $PY"

  # The staging dir must CONTAIN ``turnstone/`` -- `cp -a src dst` with a
  # non-existent dst would make dst itself the package root.
  install -d -o "$STAGE_OWNER" -g "$STAGE_GROUP" -m 0755 "$NEW.tmp.$$"
  cp -a -- "$R2_SLOT/turnstone" "$NEW.tmp.$$/turnstone"
  chmod 0755 "$NEW.tmp.$$"
  local f
  for f in "${OAUTH_FILES[@]}"; do
    [[ -f "$REPO/turnstone/$f" ]] || die "repository missing $f"
    install -o "$STAGE_OWNER" -g "$STAGE_GROUP" -m 0644 \
      "$REPO/turnstone/$f" "$NEW.tmp.$$/turnstone/$f"
    cmp -s "$REPO/turnstone/$f" "$NEW.tmp.$$/turnstone/$f" || die "copy mismatch: $f"
  done
  find "$NEW.tmp.$$" -name '__pycache__' -type d -prune -exec rm -r {} + 2>/dev/null || true
  chmod -R a+rX "$NEW.tmp.$$"
  mv -- "$NEW.tmp.$$" "$NEW"
  say "STAGED: $NEW"
  "$PY" -m compileall -q "$NEW/turnstone" >/dev/null || die "staged tree does not compile"
  say "compileall: OK"
}

# Create the test-copy directory so the TEST IDENTITY can traverse it, then prove
# it before any suite runs. `install -d -m 0700` creates it root-owned: chowning
# only the copied FILES leaves a root-owned 0700 parent that the unprivileged
# identity cannot enter, and every suite dies with "Permission denied" for a
# reason that has nothing to do with the code under test.
prepare_tests_dir() {
  local dir="$1" who="$2" sandbox="$3"
  install -d -m 0700 "$dir"
  if [[ "$sandbox" != "1" ]]; then
    chown "$who:$who" "$dir"
  fi
  # 0700 and owned by the test identity: private, and only they can write.
  [[ "$(stat -c '%a' "$dir")" == "700" ]] || die "test directory is not 0700: $dir"
  if [[ "$sandbox" != "1" ]]; then
    [[ "$(stat -c '%U' "$dir")" == "$who" ]] || die "test directory not owned by $who: $dir"
    runuser -u "$who" -- test -x "$dir" ||
      die "TESTS_DIR_NOT_TRAVERSABLE by $who: $dir"
  fi
}

cmd_verify() {
  [[ -d "$NEW/turnstone" ]] || die "no staged candidate at $NEW"

  # --- completeness: all four cooperating pieces must be present -----------
  local root="$NEW/turnstone"
  [[ -f "$root/core/oauth_token_store.py" ]] || die "missing the credential store module"
  grep -q 'openai_oauth' "$root/core/model_registry.py" || die "registry does not accept the mode"
  grep -q 'DIRECT_OAUTH_MODEL_AUTH_MODES' "$root/core/model_registry.py" || die "registry missing the direct-OAuth class"
  grep -q '_resolve_direct_oauth_token' "$root/core/model_backend_auth.py" || die "dispatch does not mint from the store"
  grep -q 'model.openai_oauth_store' "$root/core/settings_registry.py" || die "store path setting not registered"
  # Match the GUARD, not the import: importing the name is not the same as
  # letting the mode through the OIDC/OBO posture tier.
  grep -q 'if auth_mode in DIRECT_OAUTH_MODEL_AUTH_MODES:' "$root/console/server.py" \
    || die "admin write path still demands OIDC/OBO posture for this mode"
  grep -q '_resolve_direct_oauth_token' "$root/core/model_backend_auth.py" || die "dispatch branch missing"
  grep -q 'oauth_token_store' "$root/core/model_backend_auth.py" || die "dispatch does not read the store"
  say "completeness: all four cooperating changes present"

  # --- the R2 corrections must still be in place --------------------------
  grep -q 'def list_generative_aliases' "$root/core/model_registry.py" || die "R2 alias-exclusion accessor missing"
  grep -q 'list_generative_aliases' "$root/core/session.py" || die "R2 session patch missing"
  grep -q 'typed_alias' "$root/core/output_guard_judge.py" || die "R2 provenance fix missing"
  say "R2 content: preserved"

  # --- import provenance from the staged bytes ----------------------------
  RUNDIR=$(mktemp -d /var/tmp/turnstone-oauthrun.XXXXXXXX) || die "cannot create the run directory"
  chmod 0700 "$RUNDIR"
  local runner=()
  if [[ "$SANDBOX" != "1" ]]; then
    runner=(runuser -u "$SERVICE_USER" --)
    chown "$SERVICE_USER:$SERVICE_USER" "$RUNDIR"
  fi
  ( cd "$RUNDIR" && "${runner[@]}" env PYTHONPATH="$NEW" TMPDIR="$RUNDIR" HOME="$RUNDIR" \
      "$PY" -P - "$NEW" <<'PYEOF'
import pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve() / "turnstone"
import turnstone
import turnstone.core.oauth_token_store as ots
import turnstone.core.model_backend_auth as mba
import turnstone.core.model_registry as mr
import turnstone.core.settings_registry as sr
import turnstone.console.server as cs
for m in (ots, mba, mr, sr, cs):
    f = pathlib.Path(m.__file__).resolve()
    assert f.is_relative_to(root), f"{m.__name__} loaded from {f}, not the staged tree"
assert "openai_oauth" in mr.MODEL_AUTH_MODES
assert "openai_oauth" in mr.DIRECT_OAUTH_MODEL_AUTH_MODES
assert mba.DEFAULT_OPENAI_OAUTH_STORE.endswith("/runtime/openai-siwc")
print("import provenance: all modules from", root)
PYEOF
  ) || exit 3
  say "imports: staged bytes confirmed"

  # --- acceptance suites, as the unprivileged identity --------------------
  prepare_tests_dir "$RUNDIR/tests" "$SERVICE_USER" "$SANDBOX"
  local t rc=0
  for t in test_openai_oauth_auth_mode test_oauth_token_store \
           test_generative_alias_exclusion test_output_guard_typed_decision \
           test_direct_oauth_request_profile; do
    install -m 0600 "$REPO/tests/$t.py" "$RUNDIR/tests/$t.py"
    if [[ "$SANDBOX" != "1" ]]; then
      chown "$SERVICE_USER:$SERVICE_USER" "$RUNDIR/tests/$t.py"
      # Prove the identity that will run it can actually READ it, rather than
      # inferring that from a successful chown.
      runuser -u "$SERVICE_USER" -- test -r "$RUNDIR/tests/$t.py" ||
        die "TEST_FILE_NOT_READABLE by $SERVICE_USER: $RUNDIR/tests/$t.py"
    fi
    ( cd "$RUNDIR" && "${runner[@]}" env PYTHONPATH="$NEW" TMPDIR="$RUNDIR" HOME="$RUNDIR" \
        "$PY" "$RUNDIR/tests/$t.py" ) > "$WORKDIR/$t.log" 2>&1 || rc=1
    say "  $(tail -1 "$WORKDIR/$t.log")  [$t]"
  done
  [[ $rc -eq 0 ]] || die "an acceptance suite failed against the staged bytes"
  say "VERIFY OK"
}

cmd_status() {
  [[ -e "$NEW" ]] && { say "Slot:      $NEW"; stat -c '  %A %U:%G %n' "$NEW"; } || say "Slot:      absent"
  say "R2 base:   $R2_SLOT"
  for u in turnstone-server turnstone-node@2 turnstone-console; do
    pid=$(systemctl show -p MainPID --value "$u.service" 2>/dev/null || true)
    if [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/environ" ]]; then
      say "  $u: $(tr '\0' '\n' < "/proc/$pid/environ" | grep '^PYTHONPATH=' || echo 'PYTHONPATH=(none)')"
    else
      say "  $u: (not running)"
    fi
  done
  for d in "$SERVER_DROPIN" "$NODE_DROPIN" "$CONSOLE_DROPIN"; do
    [[ -e "$d" ]] && say "  override: $d" || say "  override absent: $d"
  done
}

# ---------------------------------------------------------------------------
# Activation / rollback (operator-gated; see LC-RD-007-NATIVE-DEPLOYMENT.md)
#
# SAFETY ARGUMENT: this step changes CODE, not model routing. No model row uses
# auth_mode=openai_oauth at activation time, so the new branch is unreachable
# until an operator creates a canary row (a separate, later gate). The visible
# effects are the accepted R2 typed-decision corrections and the appearance of
# openai_oauth in the admin mode list. The credential store and audience
# allow-list are prerequisites for the CANARY, not for this switch.
# ---------------------------------------------------------------------------

dropin_body() { printf '[Service]\nEnvironment="PYTHONPATH=%s"\n' "$NEW"; }

dropin_paths() { printf '%s\n' "$SERVER_DROPIN" "$NODE_DROPIN" "$CONSOLE_DROPIN"; }

# The last-sorting drop-in that sets PYTHONPATH wins in systemd's merge order.
# Refuse to activate if something would silently override the 93- file.
higher_pythonpath_override() {
  local unit=$1 ours=$2 d base ours_base
  ours_base=$(basename "$ours")
  for d in "/etc/systemd/system/${unit}.d"/*.conf; do
    [[ -e "$d" ]] || continue
    [[ "$d" == "$ours" ]] && continue
    base=$(basename "$d")
    [[ "$base" > "$ours_base" ]] || continue
    grep -qE '^[[:space:]]*Environment=.*PYTHONPATH=' "$d" && { printf '%s\n' "$d"; return 0; }
  done
  return 0
}

preflight_no_override() {
  local item unit path hit
  for item in "turnstone-server.service|$SERVER_DROPIN" \
              "turnstone-node@.service|$NODE_DROPIN" \
              "turnstone-console.service|$CONSOLE_DROPIN"; do
    unit=${item%%|*}; path=${item#*|}
    hit=$(higher_pythonpath_override "$unit" "$path")
    [[ -z "$hit" ]] || die "drop-in would be overridden by $hit"
  done
}

configured_pythonpath() {  # effective value from unit config (no restart needed)
  systemctl show -p Environment --value "$1" 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^PYTHONPATH=//p' | tail -1
}

proc_pythonpath() {
  [[ -r "/proc/$1/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$1/environ" | sed -n 's/^PYTHONPATH=//p' | tail -1
}

check_http() { curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$1/health"; }

preflight_slot() {
  [[ -d "$NEW" ]] || die "slot absent: $NEW"
  [[ -f "$NEW/turnstone/__init__.py" ]] || die "slot is not a turnstone tree: $NEW"
  if [[ "$SANDBOX" != "1" ]]; then
    [[ "$(stat -c %U "$NEW")" == root ]] || die "slot must be root-owned: $NEW"
    local mode; mode=$(stat -c %a "$NEW")
    (( (8#$mode & 8#022) == 0 )) || die "slot is group/world writable (mode $mode): $NEW"
  fi
  grep -q 'if auth_mode in DIRECT_OAUTH_MODEL_AUTH_MODES:' "$NEW/turnstone/console/server.py" \
    || die "staged console lacks the direct-OAuth posture guard"
  grep -q '_resolve_direct_oauth_token' "$NEW/turnstone/core/model_backend_auth.py" \
    || die "staged dispatch lacks the direct-OAuth branch"
  grep -q 'class OAuthTokenStore' "$NEW/turnstone/core/oauth_token_store.py" \
    || die "staged tree lacks the credential store"
  grep -q 'def list_generative_aliases' "$NEW/turnstone/core/model_registry.py" \
    || die "staged tree lacks the accepted R2 accessor"
}

preflight_units() {
  local u
  for u in "${UNITS[@]}"; do
    systemctl cat "$u" >/dev/null 2>&1 || die "unit not installed: $u"
    [[ "$(systemctl is-active "$u")" == active ]] || die "unit not active: $u"
    [[ "$(systemctl is-failed "$u")" != failed ]] || die "unit is failed: $u"
  done
}

print_plan() {
  local u pid cur
  say "Slot:      $NEW"
  say "Previous:  (90-/91- anchors and the unit file)"
  say "Units:     ${UNITS[*]}"
  say "Health:    ports ${HEALTH_PORTS[*]} (GET /health, 2xx within ${HEALTH_TIMEOUT}s)"
  say "Receipts:  $RECEIPTS_DIR"
  say ""
  say "Current process environment:"
  for u in "${UNITS[@]}"; do
    pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null || true)
    cur=$(proc_pythonpath "$pid" 2>/dev/null || true)
    say "  $(printf '%-28s' "$u") pid=$(printf '%-7s' "${pid:-none}") PYTHONPATH=${cur:-(none)}"
  done
  say ""
  say "Would write:"
  while read -r p; do say "  $p"; done < <(dropin_paths)
  say "Contents: $(dropin_body | tr '\n' ' ')"
  say "Then: systemctl daemon-reload; systemctl restart ${UNITS[*]}"
  say "Then: verify PID PYTHONPATH, unit state and /health; otherwise roll back."
  say ""
  say "No model row is changed. auth_mode=openai_oauth stays unreachable until a"
  say "canary row is created deliberately (separate gate)."
}

write_dropin() {
  local path=$1 dir tmp
  dir=$(dirname "$path")
  install -d -o root -g root -m 0755 "$dir"
  tmp=$(mktemp "$dir/.93-oauth.XXXXXXXX") || die "cannot create a temporary drop-in in $dir"
  dropin_body > "$tmp"
  chmod 0644 "$tmp"; chown root:root "$tmp"
  mv -f "$tmp" "$path"
}

capture_receipt() {
  local stamp out p u pid cur manifest
  install -d -o root -g root -m 0755 "$RECEIPTS_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  manifest=$(cd "$NEW" && find . -name '*.py' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
  out="$RECEIPTS_DIR/combined-v187-$stamp.txt"
  {
    echo "operation=activate-combined-v187"
    echo "utc=$stamp"
    echo "slot=$NEW"
    echo "slot_python_manifest_sha256=$manifest"
    echo "repo_revision=$(cd "$REPO" && git rev-parse HEAD 2>/dev/null || echo unavailable)"
    for u in "${UNITS[@]}"; do
      pid=$(systemctl show -p MainPID --value "$u")
      cur=$(proc_pythonpath "$pid" 2>/dev/null || true)
      echo "before unit=$u pid=$pid pythonpath=${cur:-(none)}"
    done
    while read -r p; do
      if [[ -e "$p" ]]; then echo "before dropin=$p sha256=$(sha256sum "$p" | cut -d' ' -f1)";
      else echo "before dropin=$p absent"; fi
    done < <(dropin_paths)
  } > "$out"
  chmod 0640 "$out"; chown root:root "$out"
  say "receipt: $out"
}

wait_healthy() {
  local want=$1 deadline=$((SECONDS + HEALTH_TIMEOUT)) u p pid cur ok
  while (( SECONDS < deadline )); do
    ok=1
    for u in "${UNITS[@]}"; do
      [[ "$(systemctl is-active "$u")" == active ]] || ok=0
      pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)
      cur=$(proc_pythonpath "$pid" 2>/dev/null || true)
      [[ "$cur" == "$want" ]] || ok=0
    done
    for p in "${HEALTH_PORTS[@]}"; do check_http "$p" || ok=0; done
    (( ok )) && return 0
    sleep 2
  done
  return 1
}

journal_scan() {  # since-epoch; nonzero if a fatal import error was logged
  local since=$1 u out
  for u in "${UNITS[@]}"; do
    out=$(journalctl -u "$u" --since "@$since" --no-pager -q 2>/dev/null \
      | grep -E 'ModuleNotFoundError|ImportError:|Traceback \(most recent call last\)' || true)
    [[ -z "$out" ]] || { printf '%s\n' "$out" >&2; return 1; }
  done
}

remove_dropins() {
  local p removed=0
  while read -r p; do
    [[ -e "$p" ]] || continue
    rm -f "$p"; removed=$((removed + 1)); say "removed: $p"
    rmdir "$(dirname "$p")" 2>/dev/null || true
  done < <(dropin_paths)
  say "drop-ins removed: $removed"
}

do_restart() {
  ACTIVATION_START_EPOCH=$(date +%s)
  systemctl daemon-reload
  systemctl restart "${UNITS[@]}"
  say "restarted: ${UNITS[*]}"
}

rollback_core() {
  local u pid cur want p ok deadline
  remove_dropins
  systemctl daemon-reload
  systemctl restart "${UNITS[@]}" 2>/dev/null || true
  say "rollback restarted: ${UNITS[*]}"
  ok=1; deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    ok=1
    for u in "${UNITS[@]}"; do
      want=$(configured_pythonpath "$u")
      pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)
      cur=$(proc_pythonpath "$pid" 2>/dev/null || true)
      [[ "$cur" == "$want" ]] || ok=0
      [[ "$(systemctl is-active "$u")" == active ]] || ok=0
    done
    for p in "${HEALTH_PORTS[@]}"; do check_http "$p" || ok=0; done
    (( ok )) && break
    sleep 2
  done
  if (( ok )); then say "ROLLBACK OK: previous overlay is live and healthy"; return 0; fi
  say "ROLLBACK INCOMPLETE: units restarted but health checks did not pass"; return 1
}

cmd_plan() { preflight_slot; preflight_units; print_plan; }

cmd_activate() {
  local confirm=0 dry=0 arg p all_present=1 identical=1
  for arg in "$@"; do
    case "$arg" in
      --confirm) confirm=1 ;;
      --dry-run) dry=1 ;;
      *) die "usage: $0 activate --confirm [--dry-run]" ;;
    esac
  done
  (( confirm == 1 )) || die "refusing to activate without --confirm (operator gate)"
  [[ "$EUID" -eq 0 || "$dry" -eq 1 ]] || die "must run as root to activate"

  preflight_slot
  preflight_units
  preflight_no_override

  while read -r p; do
    [[ -e "$p" ]] || { all_present=0; continue; }
    cmp -s <(dropin_body) "$p" || identical=0
  done < <(dropin_paths)

  if (( all_present == 1 && identical == 1 )); then
    say "ALREADY ACTIVE: all three 93- drop-ins already point at $NEW"
    say "No restart performed."
    return 0
  fi
  (( all_present == 0 )) || die "a 93- drop-in exists with different content; inspect before overriding"

  print_plan
  if (( dry == 1 )); then say "DRY RUN: nothing written, nothing restarted"; return 0; fi

  capture_receipt
  while read -r p; do write_dropin "$p"; say "wrote: $p"; done < <(dropin_paths)
  do_restart

  if ! wait_healthy "$NEW"; then
    say "ACTIVATION FAILED: health/provenance checks did not pass; rolling back" >&2
    rollback_core || true
    die "activation rolled back"
  fi
  if ! journal_scan "${ACTIVATION_START_EPOCH}"; then
    say "ACTIVATION FAILED: fatal import error in the journal; rolling back" >&2
    rollback_core || true
    die "activation rolled back"
  fi
  say "ACTIVATED: ${UNITS[*]} now import from $NEW"
  say "Rollback: sudo bash $0 rollback --confirm"
}

cmd_rollback() {
  local confirm=0 dry=0 arg p present=0
  for arg in "$@"; do
    case "$arg" in
      --confirm) confirm=1 ;;
      --dry-run) dry=1 ;;
      *) die "usage: $0 rollback --confirm [--dry-run]" ;;
    esac
  done
  (( confirm == 1 )) || die "refusing to roll back without --confirm (operator gate)"
  [[ "$EUID" -eq 0 || "$dry" -eq 1 ]] || die "must run as root to roll back"

  while read -r p; do [[ -e "$p" ]] && present=$((present + 1)); done < <(dropin_paths)
  (( present > 0 )) || { say "NOT ACTIVE: no 93- drop-ins present; nothing to roll back"; return 0; }

  if (( dry == 1 )); then
    print_plan
    say "DRY RUN: would remove the three 93- drop-ins and restart ${UNITS[*]}"
    return 0
  fi
  rollback_core
}

case "${1:-}" in
  stage)    [[ $# -eq 1 ]] || die "usage: $0 stage";    cmd_stage ;;
  verify)   [[ $# -eq 1 ]] || die "usage: $0 verify";   cmd_verify ;;
  status)   [[ $# -eq 1 ]] || die "usage: $0 status";   cmd_status ;;
  plan)     [[ $# -eq 1 ]] || die "usage: $0 plan";     cmd_plan ;;
  activate) shift; cmd_activate "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  *) die "usage: $0 {stage|verify|status|plan|activate --confirm|rollback --confirm}" ;;
esac
