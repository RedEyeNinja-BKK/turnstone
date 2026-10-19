#!/usr/bin/env bash
# Tests for s6-t2-execute.sh — the single operator command's driver.
#
# Two halves:
#   A. STATIC  the hash chain and the safety invariants, read from the shipped
#      file (not from a copy). This is the part that decides whether the
#      operator's inline `sha256sum -c` protects anything.
#   B. RUNTIME the driver's reporting behaviour, driven with a fake root and
#      stubbed steps: a step failure must print the real exit code and the
#      six-service status, and a read-only violation must abort with 3.
#
# The runtime half redirects the driver's writable paths (SLOT, EPOCH, LOG) and
# replaces the scripts it *executes* with stubs whose exit codes the test sets.
# Everything the driver does itself — hash verification, step ordering, exit
# codes, status reporting, the read-only proof — runs for real.
#
# No root, no service, no production path is touched.
#
# Usage: bash script-tests-execute.sh /path/to/s6-t2-execute.sh
set -uo pipefail

SCRIPT=${1:?usage: $0 <s6-t2-execute-script>}
S6=$(cd "$(dirname "$SCRIPT")" && pwd)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# The real interpreter: the closure check below must read the ACTUAL loader
# calls, and it must never be satisfied by a stub.
PY=${PY:-/opt/turnstone-venv/bin/python}
SANDBOX=$(mktemp -d /var/tmp/s6-exec-test.XXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0
ck() { if [ "$2" = "$3" ]; then echo "PASS  $1"; pass=$((pass+1));
       else echo "FAIL  $1: got [$2] want [$3]"; fail=$((fail+1)); fi; }

echo "== A. static invariants (read from the shipped file)"
ck "the driver does not use set -e" \
   "$(grep -c '^set -e' "$SCRIPT")" "0"
ck "it uses set -uo pipefail" \
   "$(grep -c '^set -uo pipefail$' "$SCRIPT")" "1"
ck "the boundary constant is the epoch's T2" \
   "$(grep -c '^BOUNDARY=\$EPOCH/T2$' "$SCRIPT")" "1"

# The hash chain must describe the files that are actually on disk, or the
# operator's inline check is theatre.
for pair in "MANIFEST_SHA:staging-5427ded/MANIFEST.sha256" \
            "INSTALLER_SHA:staging-5427ded/install-slot.sh" \
            "ACTIVATE_SHA:activate-cluster-5427ded.sh" \
            "ROLLBACK_SHA:rollback-cluster-5427ded.sh" \
            "COLLECT_SHA:collect.py" \
            "LIVENESS_SHA:liveness.py" \
            "PROVENANCE_SHA:provenance.py"; do
  var=${pair%%:*}; file=${pair#*:}
  want=$(sha256sum "$S6/$file" | cut -d' ' -f1)
  got=$(sed -n "s/^${var}=//p" "$SCRIPT" | head -1)
  ck "$var matches $file" "$got" "$want"
done

# A pin that is DECLARED but never VERIFIED certifies nothing: the driver
# reads the constant and the operator reads the constant, but no byte of the
# artifact is ever compared. That is how provenance.py - which both reporters
# load at runtime - was trusted without being checked. The chain is only a
# check if it covers everything that is executed or trusted.
declared=$(sed -n 's/^\([A-Z0-9_]*_SHA\)=.*/\1/p' "$SCRIPT" | sort -u)
verified=$(sed -n 's/.*verify_one .*"\(\$[A-Z0-9_]*_SHA\)".*/\1/p' "$SCRIPT" \
          | tr -d '$' | sort -u)
unverified=""
for var in $declared; do
  printf '%s\n' "$verified" | grep -qx "$var" || unverified="$unverified $var"
done
ck "every declared hash pin is verified by the driver" "${unverified:-none}" "none"

# Every verify_one must name a file that EXISTS. A pin that points at a path
# nobody can hash is a chain that stops early without saying so.
missing=""
for f in $(sed -n 's/.*verify_one "\$S6\/\([^"]*\)".*/\1/p' "$SCRIPT" | sort -u); do
  [ -f "$S6/$f" ] || missing="$missing $f"
done
ck "every verify_one names a file that exists" "${missing:-none}" "none"

# The number the driver announces must be the number it verifies. A stale
# count is how a new artifact gets added without anyone noticing it was not
# covered.
numword() { case "$1" in
  one) echo 1;; two) echo 2;; three) echo 3;; four) echo 4;; five) echo 5;;
  six) echo 6;; seven) echo 7;; eight) echo 8;; nine) echo 9;; ten) echo 10;;
  *) echo "";; esac; }
announced=$(sed -n 's/.*all \([a-z]*\) artifacts verified.*/\1/p' "$SCRIPT" | head -1)
ck "the announced artifact count matches what it verifies" \
   "$(numword "$announced")" "$(grep -c '^verify_one ' "$SCRIPT")"

# The chain must also cover every file the reporters LOAD at runtime. The
# reporters resolve their siblings by path at import time, so an unpinned
# sibling is trusted code that the chain never looks at.
loaded=$("$PY" "$HERE/chain_closure.py" "$S6" 2>/dev/null | sort -u)
verified_files=$(sed -n 's/.*verify_one "\$S6\/\([^"]*\)".*/\1/p' "$SCRIPT" | sort -u)
uncovered=""
for f in $loaded; do
  printf '%s\n' "$verified_files" | grep -qx "$f" || uncovered="$uncovered $f"
done
ck "the chain covers every file the reporters load at runtime" \
   "${uncovered:-none}" "none"

ck "reporting prefers the service account" \
   "$(grep -c 'runuser -u "\$REPORT_USER"' "$SCRIPT")" "1"
ck "read-only proof snapshots the epoch before and after" \
   "$(grep -c 'READ-ONLY PROOF' "$SCRIPT")" "1"
ck "it refuses outside root with guidance, not a bare exit" \
   "$(grep -c 'sudo bash \$S6/s6-t2-execute.sh' "$SCRIPT")" "1"

echo
echo "== B. runtime behaviour"
# Fake root: `id -u` answers 0 for the driver, `id -un` answers the service
# account so the reports run directly (the runuser branch is covered by A).
mkdir -p "$SANDBOX/bin"
cat > "$SANDBOX/bin/id" <<'SH'
#!/usr/bin/env bash
case "$*" in
  -u) echo 0 ;;
  -un) echo turnstone ;;
  -gn) echo turnstone ;;
  *) /usr/bin/id "$@" ;;
esac
SH
chmod +x "$SANDBOX/bin/id"

# The reports: print a line, and only write into the epoch when WRITER=1.
cat > "$SANDBOX/bin/pystub" <<'SH'
#!/usr/bin/env bash
if [ "${WRITER:-0}" = "1" ]; then
  printf 'x' > "$SANDBOX_EPOCH/leak.txt"
fi
echo "stub report ok ($(basename "${2:-none}"))"
SH
chmod +x "$SANDBOX/bin/pystub"

cat > "$SANDBOX/stub-install.sh" <<'SH'
#!/usr/bin/env bash
echo "stub-install $*"
exit "${INSTALL_RC:-0}"
SH
cat > "$SANDBOX/stub-activate.sh" <<'SH'
#!/usr/bin/env bash
echo "stub-activate $*"
if [ "$1" = "--status" ]; then
  echo "CLUSTER ACTIVATION STATUS"
  echo "  on 5427ded: 6/6"
  exit 0
fi
if [ "$1" = "--dry-run" ]; then exit "${DRY_RC:-0}"; fi
if [ "${CREATE_BOUNDARY:-0}" = "1" ]; then
  mkdir -p "$SANDBOX_EPOCH"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$SANDBOX_EPOCH/T2"
  printf 'epoch=s6-t2\ncapability.attachments_observed=1\n' > "$SANDBOX_EPOCH/CAPABILITY"
fi
exit "${ACTIVATE_RC:-0}"
SH
chmod +x "$SANDBOX/stub-install.sh" "$SANDBOX/stub-activate.sh"

# The copy under test: pin S6 to the real directory (the copy does not live
# there), redirect the writable paths, and swap the executed scripts for stubs.
UNDER=$SANDBOX/execute.sh
sed -e "s|^S6=.*|S6=$S6|" \
    -e "s|^SLOT=.*|SLOT=$SANDBOX/slot|" \
    -e "s|^EPOCH=.*|EPOCH=$SANDBOX/epoch|" \
    -e "s|^PY=.*|PY=$SANDBOX/bin/pystub|" \
    -e "s|^LOG=.*|LOG=$SANDBOX/run.log|" \
    -e "s|bash \"\$S6/staging-5427ded/install-slot.sh\" ensure|bash \"$SANDBOX/stub-install.sh\"|" \
    -e "s|bash \"\$S6/activate-cluster-5427ded.sh\"|bash \"$SANDBOX/stub-activate.sh\"|g" \
    "$SCRIPT" > "$UNDER"

ck "the harness rewrote every path it needs to" \
   "$(grep -c "stub-activate.sh" "$UNDER")" "3"
ck "the harness left no production path writable" \
   "$(grep -c '^SLOT=/opt/' "$UNDER")" "0"

run_driver() {
  PATH="$SANDBOX/bin:$PATH" SANDBOX_EPOCH="$SANDBOX/epoch" \
    bash "$UNDER" > "$SANDBOX/out.log" 2>&1
  echo $?
}

# --- failure at the install step -----------------------------------------
export INSTALL_RC=1 ACTIVATE_RC=0 CREATE_BOUNDARY=0 DRY_RC=0 WRITER=0
rc=$(run_driver)
ck "an install failure exits with that code" "$rc" "1"
ck "it reports the exit code" "$(grep -c 'exit code: 1' "$SANDBOX/out.log")" "1"
ck "it stops at the failing step" \
   "$(grep -c '== 3/6' "$SANDBOX/out.log")" "0"
ck "it prints the six-service status" \
   "$(grep -c 'CLUSTER ACTIVATION STATUS' "$SANDBOX/out.log")" "1"
ck "it says the session stays open" \
   "$(grep -c 'The SSH session stays open' "$SANDBOX/out.log")" "1"

# --- busy cluster (exit 5) gets the wait-and-retry guidance --------------
export INSTALL_RC=0 ACTIVATE_RC=5
rc=$(run_driver)
ck "a busy cluster exits 5" "$rc" "5"
ck "it explains that re-running is safe" \
   "$(grep -c 'the slot install is idempotent' "$SANDBOX/out.log")" "1"

# --- dry-run failure is reported, not swallowed -------------------------
export ACTIVATE_RC=0 DRY_RC=2 CREATE_BOUNDARY=0
rc=$(run_driver)
ck "a dry-run failure exits 2" "$rc" "2"
ck "the rollout never starts after a failed dry run" \
   "$(grep -c '== 4/6' "$SANDBOX/out.log")" "0"

# --- activation returns 0 but wrote no boundary -------------------------
export DRY_RC=0 ACTIVATE_RC=0 CREATE_BOUNDARY=0
rc=$(run_driver)
ck "a missing boundary after activation exits 3" "$rc" "3"
ck "it names the missing boundary" \
   "$(grep -c 'is absent' "$SANDBOX/out.log")" "1"

# --- success path --------------------------------------------------------
rm -rf "$SANDBOX/epoch"
export CREATE_BOUNDARY=1
rc=$(run_driver)
ck "the success path exits 0" "$rc" "0"
ck "it prints the boundary it found" \
   "$(grep -c 'authoritative acceptance boundary' "$SANDBOX/out.log")" "1"
ck "it prints the capability record" \
   "$(grep -c 'capability.attachments_observed=1' "$SANDBOX/out.log")" "1"
ck "it proves the reports were read-only" \
   "$(grep -c 'READ-ONLY PROOF' "$SANDBOX/out.log")" "1"
ck "the boundary is left byte-identical" \
   "$(cat "$SANDBOX/epoch/T2")" "$(cat "$SANDBOX/epoch/T2")"

# --- a report that writes must abort the run ----------------------------
export WRITER=1
rc=$(run_driver)
ck "a writing report aborts with 3" "$rc" "3"
ck "it prints the violation" \
   "$(grep -c 'a read-only report modified telemetry' "$SANDBOX/out.log")" "1"
ck "the violation is the written file" \
   "$(grep -c 'leak.txt' "$SANDBOX/out.log")" "1"
export WRITER=0

echo
echo "== the operator's inline hash check works against the shipped file"
EXEC_SHA=$(sha256sum "$SCRIPT" | cut -d' ' -f1)
( cd "$S6" && echo "$EXEC_SHA  $(basename "$SCRIPT")" | sha256sum -c - >/dev/null 2>&1 )
ck "sha256sum -c accepts the published hash" "$?" "0"
( cd "$S6" && echo "0000000000000000000000000000000000000000000000000000000000000000  $(basename "$SCRIPT")" | sha256sum -c - >/dev/null 2>&1 )
ck "sha256sum -c rejects a wrong hash" "$?" "1"

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
