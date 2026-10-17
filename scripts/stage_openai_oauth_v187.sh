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
# Revision 2: the first OAuth slot is immutable and already verified, but its
# bytes predate the fail-closed and endpoint-binding corrections. A new slot is
# staged rather than mutating that one.
NEW=${OAUTH_ROOT:-/opt/turnstone-overlays/openai-oauth-v187-r2}
SANDBOX=${R2_SANDBOX:-0}
SERVICE_USER=${R2_SERVICE_USER:-turnstone}

# The unit-configuration root is overridable ONLY inside the sandbox, so the
# partial-failure paths can be exercised against a stubbed systemctl without
# touching the host's real unit configuration.
SYSROOT=${ACTIVATION_SYSROOT:-/etc/systemd/system}
PROCROOT=${ACTIVATION_PROC_ROOT:-/proc}
SERVER_DROPIN="$SYSROOT/turnstone-server.service.d/94-openai-oauth-v187-r2.conf"
NODE_DROPIN="$SYSROOT/turnstone-node@.service.d/94-openai-oauth-v187-r2.conf"
CONSOLE_DROPIN="$SYSROOT/turnstone-console.service.d/94-openai-oauth-v187-r2.conf"

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

# root:root in production; the invoking identity in the sandbox, where the
# sysroot is a disposable directory the caller already owns.
if [[ "$SANDBOX" == "1" ]]; then
  OWNER=("$(id -u):$(id -g)")
else
  OWNER=("root:root")
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
  # The credential is bound to its endpoint by the CLIENT TRANSPORT as well as
  # by the dispatch check: without this file the staged registry passes
  # ``follow_redirects`` to a factory that does not accept it.
  "core/providers/__init__.py"
  "console/server.py"
)

# ``core/settings_registry.py`` is deliberately NOT copied whole. The repository
# version registers settings whose implementations are newer than this 1.8.7
# base (auto-continue, rerank pool/timeout); copying it would put rows in the
# Settings tab that do nothing in this slot. Only the one setting this package
# needs is patched in, at a fixed anchor, and the result is asserted.
patch_settings_registry() {
  local f="$NEW.tmp.$$/turnstone/core/settings_registry.py"
  local block="$WORKDIR/oauth-setting.txt"
  [[ -f "$f" ]] || die "settings_registry.py missing from the R2 base"
  if grep -q 'model.openai_oauth_store' "$f"; then
    die "settings_registry.py already defines model.openai_oauth_store"
  fi
  cat > "$block" <<'SETTING'
        SettingDef(
            "model.openai_oauth_store",
            "str",
            "",
            "Credential store for direct-account OpenAI OAuth model aliases",
            "model",
            help="Directory holding the coordinated OAuth refresh-token store used by "
            "auth_mode=openai_oauth model definitions. Empty uses the canonical "
            "service-runtime path /srv/turnstone/credentials/runtime/openai-siwc. "
            "One store owns the only refresh token for the account; pointing two "
            "aliases at different stores would race the provider into revoking it.",
        ),
SETTING
  awk -v bf="$block" '
    { L[NR] = $0 }
    END {
      n = NR; at = 0; seen = 0
      for (i = 1; i <= n; i++) {
        if (L[i] ~ /^[[:space:]]*"model\.auth_fail_closed",$/) seen = 1
        else if (seen && L[i] == "        ),") { at = i; break }
      }
      if (!at) exit 3
      for (i = 1; i <= n; i++) {
        print L[i]
        if (i == at) { while ((getline line < bf) > 0) print line; close(bf) }
      }
    }
  ' "$f" > "$f.new" || die "cannot locate the settings anchor in the R2 base"
  mv -- "$f.new" "$f"
  [[ "$(grep -c 'model.openai_oauth_store' "$f")" == "1" ]] \
    || die "patched settings_registry does not define the setting exactly once"
}

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
  patch_settings_registry
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

  grep -q 'model.openai_oauth_store' "$root/core/settings_registry.py" \
    || die "staged settings_registry lacks model.openai_oauth_store"
  # No setting may be exposed without an implementation in this tree. These
  # three exist in the repository but their code is newer than the 1.8.7 base;
  # copying the whole file would surface controls that do nothing.
  for inert in model.auto_continue_truncated tools.rerank_candidate_pool tools.rerank_timeout_cap_s; do
    if grep -q "\"$inert\"" "$root/core/settings_registry.py"; then
      die "staged settings_registry exposes the unimplemented setting $inert"
    fi
  done
  say "settings: model.openai_oauth_store only (no inert rows)"

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
           test_direct_oauth_request_profile test_combined_activation \
           test_activation_failure_paths; do
    install -m 0600 "$REPO/tests/$t.py" "$RUNDIR/tests/$t.py"
    if [[ "$SANDBOX" != "1" ]]; then
      chown "$SERVICE_USER:$SERVICE_USER" "$RUNDIR/tests/$t.py"
      # Prove the identity that will run it can actually READ it, rather than
      # inferring that from a successful chown.
      runuser -u "$SERVICE_USER" -- test -r "$RUNDIR/tests/$t.py" ||
        die "TEST_FILE_NOT_READABLE by $SERVICE_USER: $RUNDIR/tests/$t.py"
    fi
    ( cd "$RUNDIR" && "${runner[@]}" env PYTHONPATH="$NEW" TMPDIR="$RUNDIR" HOME="$RUNDIR" \
        R2_REPO="$REPO" "$PY" "$RUNDIR/tests/$t.py" ) > "$WORKDIR/$t.log" 2>&1 || rc=1
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
#
# FAILURE MODEL. `systemctl restart unitA unitB` is NOT a transaction: it can
# fail after unitA is already on the new code. Every failure path below is
# therefore explicit and each one converges to "previous code, verified":
#   * a drop-in write fails   -> unwritten ones removed, nothing restarted;
#   * a restart returns != 0  -> some units may be new, so roll back ALL;
#   * health/provenance/env   -> roll back ALL;
#   * rollback cannot prove   -> exit nonzero naming the unverified unit.
# A trap covers an unexpected exit after the first drop-in is written.
# ---------------------------------------------------------------------------

dropin_body() { printf '[Service]\nEnvironment="PYTHONPATH=%s"\n' "$NEW"; }

dropin_paths() { printf '%s\n' "$SERVER_DROPIN" "$NODE_DROPIN" "$CONSOLE_DROPIN"; }

unit_specs() {  # "unit|dropin" pairs, so one loop can serve both
  printf '%s\n' \
    "turnstone-server.service|$SERVER_DROPIN" \
    "turnstone-node@.service|$NODE_DROPIN" \
    "turnstone-console.service|$CONSOLE_DROPIN"
}

# The last-sorting drop-in that sets PYTHONPATH wins in systemd's merge order.
# Refuse to activate if something would silently override the 93- file.
higher_pythonpath_override() {
  local unit=$1 ours=$2 d base ours_base
  ours_base=$(basename "$ours")
  for d in "$SYSROOT/${unit}.d"/*.conf; do
    [[ -e "$d" ]] || continue
    [[ "$d" == "$ours" ]] && continue
    base=$(basename "$d")
    [[ "$base" > "$ours_base" ]] || continue
    grep -qE '^[[:space:]]*Environment=.*PYTHONPATH=' "$d" && { printf '%s\n' "$d"; return 0; }
  done
  return 1
}

# Any other drop-in that pins PYTHONPATH is a conflict to be understood, not
# silently overridden. The 92-* family is called out separately: it is the
# superseded R2-alone rollout, and leaving it in place would make rollback land
# on R2 instead of the anchor that is running today.
preflight_no_conflict() {
  local spec unit path d base hit conflicts=0
  while IFS='|' read -r unit path; do
    for d in "$SYSROOT/${unit}.d"/*.conf; do
      [[ -e "$d" ]] || continue
      [[ "$d" == "$path" ]] && continue
      base=$(basename "$d")
      case "$base" in
        92-*)
          die "superseded 92- drop-in present ($d); the combined package supersedes the R2-alone rollout" ;;
        93-openai-oauth*)
          die "superseded 93- OAuth drop-in present ($d); it predates the security corrections" ;;
      esac
      if grep -qE '^[[:space:]]*Environment=.*PYTHONPATH=' "$d"; then
        if [[ "$base" < "$(basename "$path")" ]]; then
          say "  note: earlier drop-in also sets PYTHONPATH: $d"
          conflicts=$((conflicts + 1))
        fi
      fi
    done
    # `|| true`: returning 1 is the normal "no override" answer, and under
    # `set -e` an assignment from a failing substitution would abort the run.
    hit=$(higher_pythonpath_override "$unit" "$path" || true)
    [[ -z "$hit" ]] || die "drop-in would be overridden by $hit"
  done < <(unit_specs)
  say "  earlier PYTHONPATH drop-ins (left in place, overridden): $conflicts"
}

configured_pythonpath() {  # effective value from unit config (no restart needed)
  systemctl show -p Environment --value "$1" 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^PYTHONPATH=//p' | tail -1
}

# Every Environment KEY a unit carried before the switch, minus PYTHONPATH. The
# switch must add nothing but PYTHONPATH and drop nothing at all.
environment_keys() {
  systemctl show -p Environment --value "$1" 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' \
    | grep -v '^PYTHONPATH$' | sort -u | tr '\n' ' '
}

proc_pythonpath() {
  [[ -r "$PROCROOT/$1/environ" ]] || return 1
  tr '\0' '\n' < "$PROCROOT/$1/environ" | sed -n 's/^PYTHONPATH=//p' | tail -1
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
  # The security corrections must be in the bytes that will run.
  grep -q 'def _require_direct_oauth_binding' "$NEW/turnstone/core/model_backend_auth.py" \
    || die "staged dispatch lacks the endpoint/resource binding check"
  grep -q 'follow_redirects=(' "$NEW/turnstone/core/model_registry.py" \
    || die "staged registry does not pin the direct-OAuth redirect policy"
  grep -q 'follow_redirects: bool | None = None' "$NEW/turnstone/core/providers/__init__.py" \
    || die "staged provider factory does not accept the redirect policy"
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
  say "Then: verify PID PYTHONPATH, unit state, /health and preserved env; otherwise roll back."
  say ""
  say "No model row is changed. auth_mode=openai_oauth stays unreachable until a"
  say "canary row is created deliberately (separate gate)."
}

# All-or-nothing drop-in write. Records what THIS run created so a mid-way
# failure can remove exactly that and nothing else.
write_dropins_atomically() {
  local path dir tmp written=()
  while read -r path; do
    dir=$(dirname "$path")
    if ! install -d -m 0755 "$dir"; then
      say "WRITE FAILED: cannot create $dir" >&2
      cleanup_partial_writes "${written[@]}"
      return 1
    fi
    if ! tmp=$(mktemp "$dir/.93-oauth.XXXXXXXX"); then
      say "WRITE FAILED: cannot create a temporary file in $dir" >&2
      cleanup_partial_writes "${written[@]}"
      return 1
    fi
    if ! { dropin_body > "$tmp" && chmod 0644 "$tmp" && chown "${OWNER[0]}" "$tmp" && mv -f "$tmp" "$path"; }; then
      rm -f "$tmp"
      say "WRITE FAILED: cannot install $path" >&2
      cleanup_partial_writes "${written[@]}"
      return 1
    fi
    written+=("$path")
    say "wrote: $path"
  done < <(dropin_paths)
  return 0
}

cleanup_partial_writes() {
  local p removed=0
  for p in "$@"; do
    [[ -e "$p" ]] || continue
    rm -f "$p"; removed=$((removed + 1))
    say "unwound partial write: $p" >&2
    rmdir "$(dirname "$p")" 2>/dev/null || true
  done
  say "partial writes unwound: $removed (nothing was restarted)" >&2
}

capture_receipt() {
  local stamp out p u pid cur envkeys manifest
  install -d -m 0755 "$RECEIPTS_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  manifest=$(cd "$NEW" && find . -name '*.py' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
  out="$RECEIPTS_DIR/combined-v187-$stamp.txt"
  {
    echo "operation=activate-combined-v187"
    echo "utc=$stamp"
    echo "slot=$NEW"
    echo "slot_python_manifest_sha256=$manifest"
    for u in "${UNITS[@]}"; do
      pid=$(systemctl show -p MainPID --value "$u")
      cur=$(proc_pythonpath "$pid" 2>/dev/null || true)
      envkeys=$(environment_keys "$u")
      echo "before unit=$u active=$(systemctl is-active "$u") pid=$pid pythonpath=${cur:-(none)}"
      echo "before unit=$u fragment=$(systemctl show -p FragmentPath --value "$u")"
      echo "before unit=$u env_keys=[${envkeys}]"
    done
    while read -r p; do
      if [[ -e "$p" ]]; then
        echo "before dropin=$p sha256=$(sha256sum "$p" | cut -d' ' -f1)"
      else
        echo "before dropin=$p absent"
      fi
    done < <(dropin_paths)
  } > "$out"
  chmod 0640 "$out"; chown "${OWNER[0]}" "$out"

  # Byte-exact copies of whatever the 93- paths held, so an operator can diff
  # after the fact even though the switch replaces the files.
  local bak="$RECEIPTS_DIR/dropin-backup-$stamp"
  install -d -m 0700 "$bak"
  while read -r p; do
    [[ -e "$p" ]] || continue
    install -m 0600 "$p" "$bak/$(echo "$p" | tr '/' '_')"
  done < <(dropin_paths)
  RECEIPT_FILE="$out"
  say "receipt: $out"
  say "backups: $bak"
}

# Every non-PYTHONPATH key a unit had before must still be present after.
verify_environment_preserved() {
  local u missing before after line
  local bad=0
  [[ -n "${RECEIPT_FILE:-}" && -r "$RECEIPT_FILE" ]] || {
    say "ENV UNPROVEN: no receipt to compare against" >&2; return 1; }
  # Compare each RUNNING instance (the receipt records instances, not the
  # template name the drop-in is filed under).
  for u in "${UNITS[@]}"; do
    # The BEFORE set is the one recorded before the switch; recomputing it now
    # would compare the post-restart state with itself and always pass.
    line=$(grep -F "before unit=$u env_keys=[" "$RECEIPT_FILE" | head -1 || true)
    [[ -n "$line" ]] || { say "ENV UNPROVEN: $u absent from the receipt" >&2; bad=1; continue; }
    before=${line#*env_keys=[}; before=${before%]}
    missing=""
    for key in $before; do
      after=$(systemctl show -p Environment --value "$u" 2>/dev/null | tr ' ' '\n' \
        | sed -n "s/^${key}=.*/${key}/p")
      [[ "$after" == "$key" ]] || missing="$missing $key"
    done
    if [[ -n "$missing" ]]; then
      say "ENV LOST on $u:$missing" >&2
      bad=1
    fi
  done
  (( bad == 0 ))
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
  # Name what failed instead of a bare timeout.
  for u in "${UNITS[@]}"; do
    pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)
    cur=$(proc_pythonpath "$pid" 2>/dev/null || true)
    [[ "$(systemctl is-active "$u")" == active ]] || say "  unhealthy: $u not active" >&2
    [[ "$cur" == "$want" ]] || say "  unhealthy: $u pythonpath=${cur:-(none)}" >&2
  done
  for p in "${HEALTH_PORTS[@]}"; do
    check_http "$p" || say "  unhealthy: no 2xx from port $p" >&2
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

rollback_core() {
  local u pid cur want p ok deadline
  trap - EXIT INT TERM
  remove_dropins
  systemctl daemon-reload
  if ! systemctl restart "${UNITS[@]}"; then
    say "ROLLBACK INCOMPLETE: restart returned nonzero" >&2
    return 1
  fi
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
  if (( ok )); then
    while read -r p; do
      [[ ! -e "$p" ]] || { say "ROLLBACK INCOMPLETE: $p still present" >&2; return 1; }
    done < <(dropin_paths)
    say "ROLLBACK OK: previous overlay is live, healthy, and no 93- drop-in remains"
    return 0
  fi
  for u in "${UNITS[@]}"; do
    pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)
    say "  $u active=$(systemctl is-active "$u") pythonpath=$(proc_pythonpath "$pid" 2>/dev/null || echo '(none)')" >&2
  done
  say "ROLLBACK INCOMPLETE: units restarted but verification did not pass" >&2
  return 1
}

on_unexpected_exit() {
  local rc=$?
  if (( rc != 0 )) && [[ "${ACTIVATION_WROTE:-0}" == "1" ]]; then
    say "UNEXPECTED EXIT ($rc) after the drop-ins were written; rolling back" >&2
    rollback_core || true
  fi
  exit "$rc"
}

# The sysroot/process-root overrides exist so the failure paths can be tested
# against a stub. They may only ever point INTO the sandbox.
require_sandbox_paths() {
  [[ "$SANDBOX" != "1" ]] && return 0
  [[ "$SYSROOT" == /var/tmp/* ]] || die "sandbox requires ACTIVATION_SYSROOT under /var/tmp"
  [[ "$PROCROOT" == /var/tmp/* ]] || die "sandbox requires ACTIVATION_PROC_ROOT under /var/tmp"
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
  [[ "$EUID" -eq 0 || "$dry" -eq 1 || "$SANDBOX" == "1" ]] || die "must run as root to activate"

  require_sandbox_paths
  preflight_slot
  preflight_units
  preflight_no_conflict

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
  ACTIVATION_WROTE=1
  trap on_unexpected_exit EXIT INT TERM

  if ! write_dropins_atomically; then
    ACTIVATION_WROTE=0; trap - EXIT INT TERM
    die "activation aborted during the write phase; nothing was restarted"
  fi

  ACTIVATION_START_EPOCH=$(date +%s)
  systemctl daemon-reload
  if ! systemctl restart "${UNITS[@]}"; then
    say "ACTIVATION FAILED: restart returned nonzero; some units may already be new" >&2
    rollback_core || true
    ACTIVATION_WROTE=0; trap - EXIT INT TERM
    die "activation rolled back"
  fi
  say "restarted: ${UNITS[*]}"

  local failure=""
  if ! wait_healthy "$NEW"; then
    failure="health or provenance checks did not pass"
  elif ! verify_environment_preserved; then
    failure="a unit lost one of its existing environment variables"
  elif ! journal_scan "${ACTIVATION_START_EPOCH}"; then
    failure="fatal import error in the journal"
  fi

  if [[ -n "$failure" ]]; then
    say "ACTIVATION FAILED: $failure; rolling back" >&2
    rollback_core || true
    ACTIVATION_WROTE=0; trap - EXIT INT TERM
    die "activation rolled back"
  fi

  ACTIVATION_WROTE=0; trap - EXIT INT TERM
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
  [[ "$EUID" -eq 0 || "$dry" -eq 1 || "$SANDBOX" == "1" ]] || die "must run as root to roll back"

  require_sandbox_paths
  while read -r p; do [[ -e "$p" ]] && present=$((present + 1)); done < <(dropin_paths)
  (( present > 0 )) || { say "NOT ACTIVE: no 93- drop-ins present; nothing to roll back"; return 0; }

  if (( dry == 1 )); then
    print_plan
    say "DRY RUN: would remove the three 93- drop-ins and restart ${UNITS[*]}"
    return 0
  fi

  rollback_core || die "rollback did not reach a verified state; intervene manually"
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
