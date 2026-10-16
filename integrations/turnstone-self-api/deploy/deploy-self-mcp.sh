#!/usr/bin/env bash
# LC-RD-003 — deploy the reviewed self-management MCP bridge (OPERATOR-RUN, root).
#
# Run as the operator on turnstone-vm, ONLY after deploy/run_smoke_root.sh has
# produced a VALID receipt:
#
#   sudo bash integrations/turnstone-self-api/deploy/deploy-self-mcp.sh --dry-run
#   sudo bash integrations/turnstone-self-api/deploy/deploy-self-mcp.sh
#
# Installs root-owned code, the exact-argv launcher, the sudoers entry, then
# registers the MCP row (stdio, auto_approve=false, no secrets in env), writes
# the self_server_id file so the self-unload guard is active, reloads, and
# verifies discovery on the console and every node.
#
# Rollback is printed at the end. Existing credentials, roles, personas,
# policies and services are not modified.

set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

SRC_DIR=${SRC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
ADMIN_DIR=${ADMIN_DIR:-/opt/turnstone-integrations/self-admin}
STAGE=${STAGE:-/run/turnstone-self-mcp-smoke}
PY=${PY:-/opt/turnstone-venv/bin/python}
TOKEN_FILE=${TOKEN_FILE:-/srv/turnstone/credentials/api/turnstone_admin_token}
CONSOLE=${CONSOLE:-http://127.0.0.1:8090}
RELOAD_URL=${RELOAD_URL:-$CONSOLE/v1/api/admin/mcp-servers/reload}
SERVERS_URL=${SERVERS_URL:-$CONSOLE/v1/api/admin/mcp-servers}
SERVER_NAME=turnstone-self-api

run() { if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

# A dry run performs no privileged action, so it is safe to preview unprivileged.
if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" = 0 ]; then
  echo "REFUSING: run as root (operator)" >&2; exit 2
fi

echo "=== 0. gate: smoke receipt must exist and be valid ==="
RECEIPT="$STAGE/receipt.txt"
if [ "$DRY_RUN" = 0 ]; then
  [ -r "$RECEIPT" ] || { echo "REFUSING: no smoke receipt at $RECEIPT — run run_smoke_root.sh first" >&2; exit 3; }
  "$PY" "$SRC_DIR/../receipt_check.py" "$RECEIPT" || { echo "REFUSING: smoke receipt invalid" >&2; exit 3; }
else
  echo "  [dry-run] would verify $RECEIPT with receipt_check.py"
fi

echo
echo "=== 1. install root-owned code (non-writable by the service user) ==="
run "install -d -o root -g root -m 0755 '$ADMIN_DIR'"
run "install -o root -g root -m 0644 '$SRC_DIR/server.py' '$ADMIN_DIR/server.py'"
run "install -o root -g root -m 0755 '$SRC_DIR/deploy/launch-self-mcp.sh' '$ADMIN_DIR/launch-self-mcp.sh'"

echo
echo "=== 2. exact-argv sudoers entry (no generic root shell) ==="
TMP_SUDO=$(mktemp)
trap 'rm -f "$TMP_SUDO"' EXIT
sed "s#/opt/turnstone-integrations/self-admin/launch-self-mcp.sh#$ADMIN_DIR/launch-self-mcp.sh#" \
  "$SRC_DIR/deploy/sudoers-turnstone-self-mcp" > "$TMP_SUDO"
if [ "$DRY_RUN" = 0 ]; then
  install -o root -g root -m 0440 "$TMP_SUDO" /etc/sudoers.d/turnstone-self-mcp
  visudo -c -f /etc/sudoers.d/turnstone-self-mcp || { echo "REFUSING: sudoers syntax invalid" >&2; exit 4; }
else
  echo "  [dry-run] would install to /etc/sudoers.d/turnstone-self-mcp and run visudo -c"
fi

echo
echo "=== 3. register the MCP row (stdio, auto_approve=false, no env secrets) ==="
if [ ! -r "$TOKEN_FILE" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] token file not readable here ($TOKEN_FILE) — would read it as root"
  else
    echo "REFUSING: cannot read $TOKEN_FILE" >&2; exit 5
  fi
fi
TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null || true)
api() { # method url [body]
  if [ -n "${3:-}" ]; then
    curl -sS -X "$1" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$3" "$2"
  else
    curl -sS -X "$1" -H "Authorization: Bearer $TOKEN" "$2"
  fi
}

if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would GET $SERVERS_URL, create '$SERVER_NAME' if absent, then PUT its id into env"
else
  EXISTING=$(api GET "$SERVERS_URL" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
rows=[s for s in d.get('servers',[]) if s.get('name')=='$SERVER_NAME']
print(rows[0]['server_id'] if rows else '')" )
  if [ -n "$EXISTING" ]; then
    SERVER_ID="$EXISTING"; echo "  row already exists: $SERVER_ID (not duplicating)"
  else
    BODY=$("$PY" - "$ADMIN_DIR" <<'PYEOF'
import json,sys
admin=sys.argv[1]
print(json.dumps({
  "name":"turnstone-self-api","transport":"stdio",
  "command":"/usr/bin/sudo",
  "args":["-n", admin+"/launch-self-mcp.sh"],
  "auto_approve":False,"enabled":True}))
PYEOF
)
    CREATED=$(api POST "$SERVERS_URL" "$BODY")
    SERVER_ID=$(printf '%s' "$CREATED" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('server_id',''))")
    echo "  created server_id: $SERVER_ID"
  fi
  [ -n "$SERVER_ID" ] || { echo "REFUSING: no server_id" >&2; exit 6; }

  echo "=== 4. activate the self-unload guard (root-owned id file) ==="
  printf '%s' "$SERVER_ID" > "$ADMIN_DIR/self_server_id"
  chown root:root "$ADMIN_DIR/self_server_id"
  chmod 0644 "$ADMIN_DIR/self_server_id"

  echo "=== 5. reload ==="
  api POST "$RELOAD_URL" '{}' | head -c 500; echo
fi

echo
echo "=== 6. verify discovery (console + every node) ==="
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would GET $SERVERS_URL and assert connected + 2 tools"
else
  api GET "$SERVERS_URL" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
for s in d.get('servers',[]):
    st=s.get('status') or {}
    print(f\"  {s.get('name'):28s} id={s.get('server_id')} enabled={s.get('enabled')} \"
          f\"auto_approve={s.get('auto_approve')} connected={st.get('connected')} tools={st.get('tools')}\")
"
fi

echo
echo "=== rollback (printed for the record) ==="
cat <<EOF
  # disable:      PUT $SERVERS_URL/<server_id>  {"enabled": false}   then POST $RELOAD_URL
  # remove row:   DELETE $SERVERS_URL/<server_id>                    then POST $RELOAD_URL
  # remove files: rm -f /etc/sudoers.d/turnstone-self-mcp && rm -rf $ADMIN_DIR
  # (existing credentials, roles, personas, policies and services are untouched)
EOF
echo
echo "DONE. Record: server_id above, node connectivity, and a receipt from a real"
echo "workstream call (self_guard should read 'active:<id> (file)')."
