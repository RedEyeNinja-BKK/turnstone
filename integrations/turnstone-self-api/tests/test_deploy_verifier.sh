#!/usr/bin/env bash
# LC-RD-003 — execution test for the deploy script's per-node verifier (section 6).
#
# Why this test exists: the verifier originally read the API response from
# stdin, but its Python program ALSO arrived on stdin via a heredoc. A heredoc
# redirect overrides a pipe, so json.load(sys.stdin) could never see the API
# JSON and the block aborted with `exit 8` even on a completely healthy
# payload. An isolated copy of the Python logic piped its JSON separately and
# therefore passed — it did not exercise the real invocation form.
#
# This test therefore RUNS THE REAL BLOCK, extracted verbatim from the deploy
# script by marker, so it cannot drift from what actually executes.
#
# It also covers the convergence contract (2026-10-17 production incident):
# `POST /reload` returns when reconcile finishes, NOT when a connect completes,
# and its added/removed/updated lists only mean "no config delta". A node whose
# MCP handshake is still in flight reports connected=false / tools=0 with
# has_error=false — indistinguishable from a real failure at a single sample.
# The verifier must therefore WAIT for convergence within a bounded window, and
# must still fail (bounded, never hang) when convergence never arrives.
#
# Usage:  bash tests/test_deploy_verifier.sh [path/to/deploy-self-mcp.sh]

set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT=${1:-$HERE/../deploy/deploy-self-mcp.sh}
PY=${PY:-}
if [ -z "$PY" ]; then
  for c in /opt/turnstone-venv/bin/python python3 python; do
    command -v "$c" >/dev/null 2>&1 && { PY=$c; break; }
  done
fi
[ -n "$PY" ] || { echo "FAIL: no python interpreter found" >&2; exit 2; }
[ -r "$SCRIPT" ] || { echo "FAIL: cannot read $SCRIPT" >&2; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# --- extract the real section-6 block, verbatim, by marker --------------------
# From the "=== 6. ASSERT" banner to just before the "=== 7. ASSERT" banner,
# trimming the trailing separator `echo` lines.
awk '
  /=== 6\. ASSERT/ { f = 1 }
  /=== 7\. ASSERT/ { f = 0 }
  f { print }
' "$SCRIPT" > "$TMP/block_raw.sh"

# drop trailing blank lines and bare `echo` separators
awk 'BEGIN{n=0} {l[n++]=$0} END{
  e=n; while (e>0 && (l[e-1]=="" || l[e-1]=="echo")) e--;
  for(i=0;i<e;i++) print l[i]
}' "$TMP/block_raw.sh" > "$TMP/block.sh"

if ! grep -q 'EXPECTED_TOOLS' "$TMP/block.sh"; then
  echo "FAIL: could not extract the verifier block from $SCRIPT" >&2; exit 2
fi

# --- structural guards on the extracted block --------------------------------
# The program must not read its payload from stdin (heredoc overrides the pipe).
# Match executable lines only — the explanatory comment above the fix names the
# old form.
if grep -v '^[[:space:]]*#' "$TMP/block.sh" | grep -q 'json\.load(sys\.stdin)'; then
  echo "FAIL: verifier still reads its payload from stdin (heredoc overrides the pipe)" >&2
  exit 1
fi
# The verifier must wait for convergence, not sample once.
if ! grep -q 'NODE_CONVERGE_TIMEOUT_S' "$TMP/block.sh"; then
  echo "FAIL: verifier has no bounded convergence wait (single-sample race)" >&2
  exit 1
fi

# --- fixtures -----------------------------------------------------------------
# `good`: every node connected with both tools.
# `disco`: the real 2026-10-17 incident shape — head-1/node-2/node-3 still
#          in-flight (connected=false, tools=0, has_error=false).
GOOD='{"servers":[{"name":"turnstone-self-api","server_id":"s1","transport":"stdio","enabled":true,"auto_approve":false,"status":{"console":{"connected":true,"tools":2,"has_error":false},"turnstone-head-1":{"connected":true,"tools":2,"has_error":false},"turnstone-node-2":{"connected":true,"tools":2,"has_error":false},"turnstone-node-3":{"connected":true,"tools":2,"has_error":false},"turnstone-node-4":{"connected":true,"tools":2,"has_error":false},"turnstone-node-5":{"connected":true,"tools":2,"has_error":false}}}]}'
DISCO='{"servers":[{"name":"turnstone-self-api","server_id":"s1","transport":"stdio","enabled":true,"auto_approve":false,"status":{"console":{"connected":true,"tools":2,"has_error":false},"turnstone-head-1":{"connected":false,"tools":0,"has_error":false},"turnstone-node-2":{"connected":false,"tools":0,"has_error":false},"turnstone-node-3":{"connected":false,"tools":0,"has_error":false},"turnstone-node-4":{"connected":true,"tools":2,"has_error":false},"turnstone-node-5":{"connected":true,"tools":2,"has_error":false}}}]}'
printf '%s' "$GOOD"  > "$TMP/good.json"
printf '%s' "$DISCO" > "$TMP/disco.json"

# --- harness ------------------------------------------------------------------
# Drives the real block with a stub api() that answers from a scripted sequence.
# Pattern lines are: `good`, `disco`, or a path to a JSON file. The last line
# repeats once the sequence is exhausted. NODE_POLL_S=1 keeps it fast; the
# timeout guards against a hypothetical unbounded loop wedging the suite.
run_case() { # <pattern (newline separated)> [converge_timeout_s]
  printf '%s\n' "$1" > "$TMP/pattern"
  : > "$TMP/n"
  TMP="$TMP" PY="$PY" CT="${2:-1}" timeout 40 bash -c '
    set -uo pipefail
    DRY_RUN=0
    SERVER_NAME=turnstone-self-api
    EXPECTED_TOOLS=2
    EXPECTED_NODES=(console turnstone-head-1 turnstone-node-2 turnstone-node-3 turnstone-node-4 turnstone-node-5)
    SERVERS_URL="http://127.0.0.1:8090/v1/api/admin/mcp-servers"
    NODE_POLL_S=1
    NODE_CONVERGE_TIMEOUT_S="$CT"
    api() {
      local n idx val
      n=$(cat "$TMP/n" 2>/dev/null || echo 0)
      n=$((n + 1)); printf "%s" "$n" > "$TMP/n"
      idx=$((n - 1))
      val=$(sed -n "$((idx + 1))p" "$TMP/pattern")
      [ -n "$val" ] || val=$(tail -1 "$TMP/pattern")
      case "$val" in
        good)  cat "$TMP/good.json" ;;
        disco) cat "$TMP/disco.json" ;;
        *)     cat "$val" ;;
      esac
    }
    source "$TMP/block.sh"
  ' >"$TMP/out.log" 2>&1
}

mkjson() { printf '%s' "$1" > "$TMP/case.json"; }

pass=0; fail=0

check() { # <label> <expected-exit> <actual-exit>
  if [ "$2" -eq "$3" ]; then
    printf '  [PASS] %-46s exit=%s\n' "$1" "$3"; pass=$((pass + 1))
  else
    printf '  [FAIL] %-46s exit=%s (expected %s)\n' "$1" "$3" "$2"; fail=$((fail + 1))
    sed 's/^/         | /' "$TMP/out.log" | tail -5
  fi
}

echo "=== positive: healthy payload -> the verifier must ACCEPT (exit 0) ==="
run_case good; rc=$?
check "healthy payload accepted" 0 "$rc"
if [ "$rc" -eq 0 ]; then
  if grep -q '^PASS: 6 nodes connected with 2 tools each$' "$TMP/out.log"; then
    printf '  [PASS] %-46s\n' "healthy payload reports PASS summary"; pass=$((pass + 1))
  else
    printf '  [FAIL] %-46s (no PASS summary line)\n' "healthy payload reports PASS"; fail=$((fail + 1))
  fi
fi

echo
echo "=== convergence: must WAIT for in-flight handshakes, then accept ==="
# Disconnected samples followed by a connected one — mirrors the production
# incident, where the three slow nodes finished their handshake shortly after
# the (too early) assertion sampled them.
run_case "$(printf 'disco\ngood')" 10; check "waits: disco then good -> 0" 0 "$?"
run_case "$(printf 'disco\ndisco\ngood')" 10; check "waits: disco x2 then good -> 0" 0 "$?"

echo
echo "=== bounded: never converges -> must FAIL (exit 8), never hang ==="
run_case disco 3; check "never converges (bounded) -> 8" 8 "$?"

echo
echo "=== negative: each unhealthy payload -> the verifier must REJECT (exit 8) ==="
mkjson "$(printf '%s' "$GOOD" | sed 's/"turnstone-node-4":{"connected":true/"turnstone-node-4":{"connected":false/')"
run_case "$TMP/case.json"; check "node-4 disconnected" 8 "$?"

mkjson "$(printf '%s' "$GOOD" | sed 's/,"turnstone-node-5":{[^}]*}//')"
run_case "$TMP/case.json"; check "node-5 reported no status" 8 "$?"

mkjson "$(printf '%s' "$GOOD" | sed 's/"turnstone-head-1":{"connected":true,"tools":2/"turnstone-head-1":{"connected":true,"tools":1/')"
run_case "$TMP/case.json"; check "head reports wrong tool count" 8 "$?"

mkjson "$(printf '%s' "$GOOD" | sed 's/"auto_approve":false/"auto_approve":true/')"
run_case "$TMP/case.json"; check "row has auto_approve=true" 8 "$?"

mkjson "$(printf '%s' "$GOOD" | sed 's/"enabled":true/"enabled":false/')"
run_case "$TMP/case.json"; check "row is disabled" 8 "$?"

mkjson '{"servers":[]}'
run_case "$TMP/case.json"; check "no matching server row" 8 "$?"

echo
echo "=== fails closed: response channel unreadable/invalid -> REJECT (exit 8) ==="
: > "$TMP/empty.json"
run_case "$TMP/empty.json"; check "empty response body" 8 "$?"

mkjson 'not json at all'
run_case "$TMP/case.json"; check "non-JSON response body" 8 "$?"

run_case "$TMP/does-not-exist.json"; check "response channel unreadable" 8 "$?"

echo
echo "=== summary ==="
printf '  passed=%d  failed=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
echo "  ALL VERIFIER CASES PASS"
