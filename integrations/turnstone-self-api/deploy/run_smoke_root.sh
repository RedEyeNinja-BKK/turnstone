#!/usr/bin/env bash
# Root smoke gate for the self-management MCP bridge — READ-ONLY.
#
# Run as the OPERATOR (or any principal that can sudo) on turnstone-vm:
#   sudo bash integrations/turnstone-self-api/deploy/run_smoke_root.sh
#
# Stages SHA-256-verified bytes into a root-owned 0700 /run dir and runs the
# read-only smoke as root. Per commit b58daf3: never execute user-writable
# checkout code as root.
#
# Makes NO registration, NO reload, NO systemd/sudoers change, NO API writes.

set -euo pipefail

# --- pinned reviewed release candidate --------------------------------------
REPO_COMMIT=${REPO_COMMIT:-9f2096e00d9662076e4ecef2c7232f9d2c853f62}
SRC_DIR=${SRC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
STAGE=${STAGE:-/run/turnstone-self-mcp-smoke}
PY=${PY:-/opt/turnstone-venv/bin/python}

EXP_SERVER_SHA=6071797b82aef5cec75f9edaabb7d77f9e85b110e89afa73b53a2b192f451846
EXP_SMOKE_SHA=7ef06d9ad4f2b0819e5d5482a3d65c528bdf8e4842e5b4c3c07f5545a47923ef

if [ "$(id -u)" -ne 0 ]; then
  echo "REFUSING: must run as root (operator)" >&2; exit 2
fi

echo "=== 1. verify source hashes BEFORE staging ==="
actual_server=$(sha256sum "$SRC_DIR/server.py"    | awk '{print $1}')
actual_smoke=$(sha256sum  "$SRC_DIR/smoke_mcp.py" | awk '{print $1}')
echo "server.py     $actual_server"
echo "smoke_mcp.py  $actual_smoke"
[ "$actual_server" = "$EXP_SERVER_SHA" ] || { echo "REFUSING: server.py hash mismatch" >&2; exit 3; }
[ "$actual_smoke"  = "$EXP_SMOKE_SHA"  ] || { echo "REFUSING: smoke_mcp.py hash mismatch" >&2; exit 3; }
echo "baseline commit expectation: $REPO_COMMIT"

echo
echo "=== 2. stage into root-owned isolated dir (disposable) ==="
install -d -o root -g root -m 0700 "$STAGE"
install -o root -g root -m 0600 "$SRC_DIR/server.py"    "$STAGE/server.py"
install -o root -g root -m 0600 "$SRC_DIR/smoke_mcp.py" "$STAGE/smoke_mcp.py"
# The smoke locates server.py next to itself, so the staged pair must stay together.
ls -la "$STAGE"

echo
echo "=== 3. re-verify staged bytes match the reviewed hashes ==="
[ "$(sha256sum "$STAGE/server.py"    | awk '{print $1}')" = "$EXP_SERVER_SHA" ] || { echo "REFUSING: staged server.py mismatch" >&2; exit 3; }
[ "$(sha256sum "$STAGE/smoke_mcp.py" | awk '{print $1}')" = "$EXP_SMOKE_SHA"  ] || { echo "REFUSING: staged smoke mismatch" >&2; exit 3; }
echo "staged == reviewed: OK"

echo
echo "=== 4. compile check on staged copies ==="
"$PY" -m py_compile "$STAGE/server.py" "$STAGE/smoke_mcp.py" && echo "py_compile OK"

echo
echo "=== 5. read-only smoke (capture to a root-only receipt) ==="
OUT="$STAGE/receipt.txt"
"$PY" "$STAGE/smoke_mcp.py" 2>&1 | tee "$OUT"
rc=${PIPESTATUS[0]}
chmod 600 "$OUT"
echo "smoke exit code: $rc"
echo "receipt: $OUT  (contains NO token by construction)"

echo
echo "=== 6. validate the receipt (non-secret assertions + disclosure scan) ==="
"$PY" "$SRC_DIR/../receipt_check.py" "$OUT" || true
exit "$rc"
