#!/usr/bin/env bash
# Install and verify the bounded read-only Grafana runtime interface (Option B).
#
# This is the only step that needs an operator: it writes a root-owned sudoers
# drop-in, which by construction the Turnstone service identity cannot do for
# itself. Everything after installation -- identity, org, discovery and bounded
# queries -- runs from the service identity through the exact-argv grant.
#
# Verbs:
#   install     install files, audit directory and sudoers drop-in, then verify
#   check       read-only drift/permission check against this repository
#   verify      run the runtime interface as the service identity
#   query-smoke one bounded query against the first Prometheus datasource
#   status      show effective sudo grants for the service identity
#
# Nothing here removes, replaces or widens any existing grant. The uninstall
# path is deliberately not automated; deleting this drop-in is a manual,
# reviewed operation.
#
# Usage (as root):  scripts/install_grafana_runtime.sh install
set -euo pipefail

DEST=/opt/turnstone-integrations/grafana-readonly
SUDOERS_DEST=/etc/sudoers.d/turnstone-grafana-readonly
AUDIT_DIR=/var/log/turnstone-grafana
SERVICE_USER=turnstone
PYTHON=${GRAFANA_RUNTIME_PYTHON:-/opt/turnstone-venv/bin/python}
SRC=${GRAFANA_RUNTIME_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}

MODULE_SRC="$SRC/scripts/turnstone_grafana_runtime.py"
LAUNCHER_SRC="$SRC/scripts/launch-grafana-readonly.sh"
SUDOERS_SRC="$SRC/scripts/turnstone-grafana-readonly.sudoers"
LAUNCHER="$DEST/launch-grafana-readonly.sh"

die() { echo "REFUSED: $*" >&2; exit 2; }
note() { printf '  %s\n' "$*"; }
heading() { printf '\n%s\n' "$*"; }

require_root() { [ "$(id -u)" -eq 0 ] || die "must run as root"; }

require_sources() {
  local f
  for f in "$MODULE_SRC" "$LAUNCHER_SRC" "$SUDOERS_SRC"; do
    [ -f "$f" ] || die "missing source: $f"
  done
}

backup_if_present() {
  local path=$1
  [ -e "$path" ] || return 0
  local backup="${path}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "$path" "$backup"
  note "backup: $backup"
}

install_files() {
  install -d -o root -g root -m 0755 "$DEST"
  backup_if_present "$DEST/grafana_runtime.py"
  backup_if_present "$DEST/launch-grafana-readonly.sh"
  install -o root -g root -m 0755 "$LAUNCHER_SRC" "$DEST/launch-grafana-readonly.sh"
  install -o root -g root -m 0644 "$MODULE_SRC" "$DEST/grafana_runtime.py"
  install -d -o root -g "$SERVICE_USER" -m 0750 "$AUDIT_DIR"
  note "module:  $DEST/grafana_runtime.py"
  note "launcher:$DEST/launch-grafana-readonly.sh"
  note "audit:   $AUDIT_DIR"
}

install_sudoers() {
  local tmp
  tmp=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" EXIT
  install -o root -g root -m 0440 "$SUDOERS_SRC" "$tmp"
  if ! visudo -c -f "$tmp" >/dev/null; then
    die "sudoers syntax check failed; nothing was installed"
  fi
  backup_if_present "$SUDOERS_DEST"
  install -o root -g root -m 0440 "$tmp" "$SUDOERS_DEST"
  rm -f "$tmp"
  trap - EXIT
  note "sudoers: $SUDOERS_DEST"
}

run_as_service() {
  runuser -u "$SERVICE_USER" -- sudo -n "$LAUNCHER" "$@"
}

cmd_status() {
  require_root
  heading "Effective sudo grants for $SERVICE_USER"
  runuser -u "$SERVICE_USER" -- sudo -n -l 2>&1 | sed -n '/may run the following/,$p'
}

cmd_check() {
  require_root
  require_sources
  local failures=0
  heading "Drift check"
  if [ -f "$DEST/grafana_runtime.py" ]; then
    if cmp -s "$MODULE_SRC" "$DEST/grafana_runtime.py"; then
      note "module matches repository"
    else
      note "DRIFT: deployed module differs from repository"; failures=$((failures + 1))
    fi
  else
    note "MISSING: $DEST/grafana_runtime.py"; failures=$((failures + 1))
  fi
  if [ -x "$LAUNCHER" ]; then
    note "launcher present and executable"
  else
    note "MISSING: $LAUNCHER"; failures=$((failures + 1))
  fi
  if [ -f "$SUDOERS_DEST" ]; then
    if visudo -c -f "$SUDOERS_DEST" >/dev/null; then
      note "sudoers parses"
    else
      note "BROKEN: sudoers does not parse"; failures=$((failures + 1))
    fi
  else
    note "MISSING: $SUDOERS_DEST"; failures=$((failures + 1))
  fi
  if runuser -u "$SERVICE_USER" -- sudo -n -l 2>/dev/null |
      grep -q 'launch-grafana-readonly.sh identity'; then
    note "grant visible to $SERVICE_USER"
  else
    note "GRANT NOT VISIBLE to $SERVICE_USER"; failures=$((failures + 1))
  fi
  heading "Result"
  if [ "$failures" -eq 0 ]; then
    note "OK"
    return 0
  fi
  note "$failures check(s) failed"
  return 1
}

cmd_verify() {
  require_root
  [ -x "$LAUNCHER" ] || die "launcher not installed"
  local rc=0
  heading "identity"
  run_as_service identity || rc=1
  heading "org"
  run_as_service org || rc=1
  heading "health"
  run_as_service health || rc=1
  heading "datasources"
  run_as_service datasources || rc=1
  return "$rc"
}

cmd_query_smoke() {
  require_root
  [ -x "$LAUNCHER" ] || die "launcher not installed"
  local uid
  # Parse with the deployed module's own selector: one implementation, so the
  # installer cannot drift from the CLI's response shape again.
  uid=$(run_as_service datasources | "$PYTHON" -I -B -c '
import json, sys
sys.path.insert(0, sys.argv[1])
import grafana_runtime
try:
    print(grafana_runtime.select_uid(json.load(sys.stdin)))
except Exception:
    sys.exit(1)
' "$DEST")
  if [ -z "${uid:-}" ]; then
    note "no Prometheus datasource found; bounded query not attempted"
    return 1
  fi
  heading "bounded query (datasource $uid, vector(1), instant, 5m)"
  local out
  out=$(printf '%s' "{\"datasource_uid\":\"$uid\",\"expr\":\"vector(1)\",\"kind\":\"instant\",\"minutes\":5}" |
    run_as_service query) || { printf '%s\n' "$out"; return 1; }
  printf '%s\n' "$out"
  printf '%s' "$out" | "$PYTHON" -I -c '
import json, sys
try:
    body = json.load(sys.stdin)
except Exception:
    sys.exit(1)
results = (body.get("data") or {})
points = sum(int((r or {}).get("points") or 0) for r in results.values())
sys.exit(0 if body.get("status") == 200 and points > 0 else 1)
' || { note "bounded query returned no data points"; return 1; }
  note "bounded query returned data points"
}

cmd_install() {
  require_root
  require_sources
  [ -x "$PYTHON" ] || die "interpreter not executable: $PYTHON"
  heading "Install"
  install_files
  install_sudoers
  cmd_check || return 1
  heading "Verify"
  if cmd_verify; then
    heading "Bounded query smoke"
    if cmd_query_smoke; then
      note "INSTALLED AND VERIFIED"
    else
      note "INSTALLED, but the bounded query did not return data"
      return 1
    fi
  else
    note "INSTALLED, but verification reported an error (see above)"
    return 1
  fi
}

main() {
  case "${1:-}" in
    install) cmd_install ;;
    check) cmd_check ;;
    verify) cmd_verify ;;
    query-smoke) cmd_query_smoke ;;
    status) cmd_status ;;
    *) die "usage: $0 {install|check|verify|query-smoke|status}" ;;
  esac
}

main "$@"
