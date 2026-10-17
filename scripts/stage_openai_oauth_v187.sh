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
# Usage (root):
#   sudo bash stage_openai_oauth_v187.sh stage
#   sudo bash stage_openai_oauth_v187.sh verify
#   sudo bash stage_openai_oauth_v187.sh status
#   sudo bash stage_openai_oauth_v187.sh activate     # refuses: operator gate
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

die() { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }
say() { printf '%s\n' "$*"; }

if [[ "$SANDBOX" == "1" ]]; then
  [[ "$NEW" == /var/tmp/* ]] || die "sandbox requires OAUTH_ROOT under /var/tmp"
  STAGE_OWNER=$(id -un); STAGE_GROUP=$(id -gn)
else
  [[ "$EUID" -eq 0 ]] || die "must run as root (or set R2_SANDBOX=1 with OAUTH_ROOT under /var/tmp)"
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
  install -d -m 0700 "$RUNDIR/tests"
  local t rc=0
  for t in test_openai_oauth_auth_mode test_oauth_token_store \
           test_generative_alias_exclusion test_output_guard_typed_decision \
           test_direct_oauth_request_profile; do
    install -m 0600 "$REPO/tests/$t.py" "$RUNDIR/tests/$t.py"
    if [[ "$SANDBOX" != "1" ]]; then chown "$SERVICE_USER:$SERVICE_USER" "$RUNDIR/tests/$t.py"; fi
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

cmd_activate() {
  cat >&2 <<'MSG'
REFUSED: activation is an operator-gated production boundary.

This package introduces a NEW model auth mode and a credential source. It must
not be activated before:
  * the SIWC runtime store is live at the canonical path,
  * a fresh-workstream canary has run against it, and
  * the operator approves a restart window for server, nodes and console.

The exact command set is in LC-RD-007-NATIVE-DEPLOYMENT.md.
MSG
  exit 3
}

case "${1:-}" in
  stage)    [[ $# -eq 1 ]] || die "usage: $0 stage";    cmd_stage ;;
  verify)   [[ $# -eq 1 ]] || die "usage: $0 verify";   cmd_verify ;;
  status)   [[ $# -eq 1 ]] || die "usage: $0 status";   cmd_status ;;
  activate) cmd_activate ;;
  *) die "usage: $0 {stage|verify|status|activate}" ;;
esac
