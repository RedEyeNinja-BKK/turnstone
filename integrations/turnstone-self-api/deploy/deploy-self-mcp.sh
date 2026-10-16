#!/usr/bin/env bash
# LC-RD-003 — deploy the reviewed self-management MCP bridge (OPERATOR-RUN, root).
#
# Run as the operator on turnstone-vm, ONLY after deploy/run_smoke_root.sh has
# produced a VALID receipt:
#
#   sudo bash integrations/turnstone-self-api/deploy/deploy-self-mcp.sh --dry-run
#   sudo bash integrations/turnstone-self-api/deploy/deploy-self-mcp.sh
#
# Installs root-owned code + the exact-argv launcher, registers the MCP row
# (stdio, auto_approve=false, no secrets in env), activates the self-unload
# guard, reloads, then ASSERTS per-node connected status, the tool count, and a
# live stdio handshake (both tool names + their schemas + six authenticated
# reads). Any failure exits non-zero.
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
SERVERS_URL=${SERVERS_URL:-$CONSOLE/v1/api/admin/mcp-servers}
RELOAD_URL=${RELOAD_URL:-$SERVERS_URL/reload}
SERVER_NAME=turnstone-self-api
# Console pseudo-node plus the head and the four workers.
EXPECTED_NODES=(console turnstone-head-1 turnstone-node-2 turnstone-node-3 turnstone-node-4 turnstone-node-5)
EXPECTED_TOOLS=2

RECEIPT_CHECK="$SRC_DIR/receipt_check.py"
[ -r "$RECEIPT_CHECK" ] || { echo "REFUSING: receipt_check.py not found at $RECEIPT_CHECK" >&2; exit 2; }

# A dry run performs no privileged action, so it is safe to preview unprivileged.
if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" = 0 ]; then
  echo "REFUSING: run as root (operator)" >&2; exit 2
fi

run() { if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

echo "=== 0. gate: smoke receipt must exist and be VALID ==="
RECEIPT="$STAGE/receipt.txt"
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would run: $PY $RECEIPT_CHECK $RECEIPT   (must exit 0)"
else
  [ -r "$RECEIPT" ] || { echo "REFUSING: no smoke receipt at $RECEIPT — run run_smoke_root.sh first" >&2; exit 3; }
  if ! "$PY" "$RECEIPT_CHECK" "$RECEIPT"; then
    echo "REFUSING: smoke receipt INVALID — deployment aborted" >&2
    exit 3
  fi
fi

echo
echo "=== 1. install root-owned code (non-writable by the service user) ==="
run "install -d -o root -g root -m 0755 '$ADMIN_DIR'"
run "install -o root -g root -m 0644 '$SRC_DIR/server.py' '$ADMIN_DIR/server.py'"
run "install -o root -g root -m 0644 '$SRC_DIR/smoke_mcp.py' '$ADMIN_DIR/smoke_mcp.py'"
run "install -o root -g root -m 0644 '$RECEIPT_CHECK' '$ADMIN_DIR/receipt_check.py'"
run "install -o root -g root -m 0755 '$SRC_DIR/deploy/launch-self-mcp.sh' '$ADMIN_DIR/launch-self-mcp.sh'"

echo
echo "=== 2. exact-argv sudoers entry (no arguments accepted, no generic root shell) ==="
TMP_SUDO=$(mktemp); trap 'rm -f "$TMP_SUDO"' EXIT
sed "s#/opt/turnstone-integrations/self-admin/launch-self-mcp.sh#$ADMIN_DIR/launch-self-mcp.sh#" \
  "$SRC_DIR/deploy/sudoers-turnstone-self-mcp" > "$TMP_SUDO"
if [ "$DRY_RUN" = 0 ]; then
  visudo -c -f "$TMP_SUDO" >/dev/null || { echo "REFUSING: sudoers syntax invalid" >&2; exit 4; }
  install -o root -g root -m 0440 "$TMP_SUDO" /etc/sudoers.d/turnstone-self-mcp
  echo "  installed /etc/sudoers.d/turnstone-self-mcp (validated)"
else
  echo "  [dry-run] would validate and install to /etc/sudoers.d/turnstone-self-mcp"
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
TOKEN=""
if [ "$DRY_RUN" = 0 ]; then
  TOKEN=$(cat "$TOKEN_FILE") || { echo "REFUSING: reading $TOKEN_FILE failed" >&2; exit 5; }
  [ -n "$TOKEN" ] || { echo "REFUSING: $TOKEN_FILE is empty" >&2; exit 5; }
fi

# Every call asserts a 2xx; a non-2xx (or curl failure) aborts the deployment.
api() { # method url [body]
  local method="$1" url="$2" body="${3:-}" out code
  out=$(mktemp)
  if [ -n "$body" ]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d "$body" "$url") || { echo "REFUSING: curl failed: $method $url" >&2; rm -f "$out"; return 1; }
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer $TOKEN" "$url") || { echo "REFUSING: curl failed: $method $url" >&2; rm -f "$out"; return 1; }
  fi
  if [ "$code" -lt 200 ] || [ "$code" -ge 300 ]; then
    echo "REFUSING: $method $url -> HTTP $code: $(head -c 300 "$out")" >&2
    rm -f "$out"; return 1
  fi
  cat "$out"; rm -f "$out"
}

SERVER_ID=""
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would GET $SERVERS_URL, create '$SERVER_NAME' if absent, then write self_server_id"
else
  LIST=$(api GET "$SERVERS_URL") || exit 6
  SERVER_ID=$(printf '%s' "$LIST" | "$PY" -c "
import json,sys
rows=[s for s in json.load(sys.stdin).get('servers',[]) if s.get('name')=='$SERVER_NAME']
print(rows[0]['server_id'] if rows else '')")
  if [ -n "$SERVER_ID" ]; then
    echo "  row already exists: $SERVER_ID (not duplicating)"
  else
    BODY=$("$PY" - "$ADMIN_DIR" <<'PYEOF'
import json,sys
admin=sys.argv[1]
print(json.dumps({"name":"turnstone-self-api","transport":"stdio",
 "command":"/usr/bin/sudo","args":["-n", admin+"/launch-self-mcp.sh"],
 "auto_approve":False,"enabled":True}))
PYEOF
)
    CREATED=$(api POST "$SERVERS_URL" "$BODY") || exit 6
    SERVER_ID=$(printf '%s' "$CREATED" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('server_id',''))") || exit 6
    [ -n "$SERVER_ID" ] || { echo "REFUSING: create returned no server_id" >&2; exit 6; }
    echo "  created server_id: $SERVER_ID"
  fi

  echo
  echo "=== 4. activate the self-unload guard (root-owned id file) ==="
  printf '%s' "$SERVER_ID" > "$ADMIN_DIR/self_server_id"
  chown root:root "$ADMIN_DIR/self_server_id"
  chmod 0644 "$ADMIN_DIR/self_server_id"
  echo "  wrote $ADMIN_DIR/self_server_id"
fi

echo
echo "=== 5. reload MCP pools ==="
if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] POST $RELOAD_URL"; else api POST "$RELOAD_URL" '{}' || exit 7; fi

echo
echo "=== 6. ASSERT per-node registration: connected + $EXPECTED_TOOLS tools ==="
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would assert nodes: ${EXPECTED_NODES[*]}"
else
  # The reload returns when reconcile finishes, NOT when a connect completes,
  # and its added/removed/updated lists only mean "no config delta" — neither
  # establishes connection success. A node whose MCP handshake is still in
  # flight reports connected=false / tools=0 with has_error=false. A single
  # sample here therefore failed on whichever nodes were slowest to handshake
  # (observed 2026-10-17: head-1/node-2/node-3 at exit 8 while their bridges
  # were alive and reported connected seconds later). Poll a bounded window.
  #
  # The verifier program arrives on stdin (heredoc) so the API response cannot
  # share that channel: a heredoc redirect overrides the pipe, which made
  # json.load(sys.stdin) fail even on a healthy payload. Deliver the response
  # on fd 3 instead, keeping the program and the payload separate.
  NODE_CONVERGE_TIMEOUT_S=${NODE_CONVERGE_TIMEOUT_S:-120}
  NODE_POLL_S=${NODE_POLL_S:-5}
  [ "$NODE_POLL_S" -ge 1 ] || NODE_POLL_S=1
  MAX_ATTEMPTS=$(( NODE_CONVERGE_TIMEOUT_S / NODE_POLL_S + 1 ))
  attempt=0
  while :; do
    attempt=$(( attempt + 1 ))
    rc=0
    out=$("$PY" - "$SERVER_NAME" "$EXPECTED_TOOLS" "${EXPECTED_NODES[@]}" 3< <(api GET "$SERVERS_URL") <<'PYEOF' 2>&1
import json, sys

name, want_tools = sys.argv[1], int(sys.argv[2])
expected_nodes = sys.argv[3:]
try:
    payload = json.load(open(3))
except OSError as exc:
    print(f"FAIL: could not read the API response on fd 3: {exc}")
    raise SystemExit(2)
except json.JSONDecodeError as exc:
    print(f"FAIL: API response was not valid JSON: {exc}")
    raise SystemExit(2)
rows = [s for s in payload.get("servers", []) if s.get("name") == name]
if not rows:
    print(f"FAIL: no server row named {name!r}")
    raise SystemExit(1)
row = rows[0]
status = row.get("status") or {}

print(f"  row: id={row.get('server_id')} transport={row.get('transport')} "
      f"enabled={row.get('enabled')} auto_approve={row.get('auto_approve')}")
problems = []
if row.get("transport") != "stdio":
    problems.append(f"transport is {row.get('transport')!r}, expected 'stdio'")
if row.get("enabled") is not True:
    problems.append("enabled is not true")
if row.get("auto_approve") is not False:
    problems.append("auto_approve is not false")

for node in expected_nodes:
    st = status.get(node)
    if st is None:
        problems.append(f"{node}: no status reported (node did not report; may need a reload)")
        continue
    connected = bool(st.get("connected"))
    tools = st.get("tools")
    has_error = bool(st.get("has_error"))
    print(f"  {node:18s} connected={connected} tools={tools} has_error={has_error}")
    if not connected:
        problems.append(f"{node}: not connected")
    if tools != want_tools:
        problems.append(f"{node}: tools={tools}, expected {want_tools}")
    if has_error:
        problems.append(f"{node}: reported an error")

if problems:
    print("FAIL: per-node assertions failed:")
    for p in problems:
        print("   -", p)
    raise SystemExit(1)
print(f"PASS: {len(expected_nodes)} nodes connected with {want_tools} tools each")
PYEOF
    ) || rc=$?

    case "$rc" in
      0) printf '%s\n' "$out"; break ;;
      2)
        # Transport / auth / parse failure: retrying cannot help.
        printf '%s\n' "$out"
        echo "FAIL: could not read per-node MCP status from the API" >&2
        exit 8
        ;;
    esac

    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
      printf '%s\n' "$out"
      echo "FAIL: nodes did not all connect within ${NODE_CONVERGE_TIMEOUT_S}s" >&2
      exit 8
    fi
    echo "  attempt ${attempt}/${MAX_ATTEMPTS}: not all nodes connected yet — re-checking in ${NODE_POLL_S}s"
    sleep "$NODE_POLL_S"
  done
fi

echo
echo "=== 7. ASSERT live handshake against the INSTALLED bytes (names + schemas) ==="
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would run the smoke against $ADMIN_DIR and validate its receipt"
else
  OUT=$(mktemp)
  if ! "$PY" "$ADMIN_DIR/smoke_mcp.py" 2>&1 | tee "$OUT"; then
    echo "FAIL: installed-bytes smoke failed" >&2; rm -f "$OUT"; exit 9
  fi
  # smoke_mcp.py resolves server.py beside itself, so it exercises the INSTALLED copy.
  if ! "$PY" "$ADMIN_DIR/receipt_check.py" "$OUT"; then
    echo "FAIL: installed-bytes receipt invalid" >&2; rm -f "$OUT"; exit 9
  fi
  rm -f "$OUT"
fi

echo
echo "=== rollback (for the record) ==="
cat <<EOF
  # disable:      PUT $SERVERS_URL/${SERVER_ID:-<server_id>}  {"enabled": false}   then POST $RELOAD_URL
  # remove row:   DELETE $SERVERS_URL/${SERVER_ID:-<server_id>}                    then POST $RELOAD_URL
  # remove files: rm -f /etc/sudoers.d/turnstone-self-mcp && rm -rf $ADMIN_DIR
  # (existing credentials, roles, personas, policies and services are untouched)
EOF
echo
echo "DONE. server_id=${SERVER_ID:-<dry-run>}  Record the assertions above as the acceptance receipt."
