#!/usr/bin/env bash
# NEGATIVE CONTROL for the driver's hash chain.
#
# script-tests-execute.sh proves the chain is consistent. This proves that
# proof actually BITES. A chain check that cannot fail is theatre, and one
# that only covers the files someone remembered to list is worse than
# theatre: it certifies an artifact it never looked at.
#
# Each case below mutates a COPY of the driver in a disposable sandbox and
# requires the static half of script-tests-execute.sh to report a specific
# failure. The shipped driver is never modified.
#
# The cases:
#   positive   the unmutated driver passes the static half
#   pins       each of the seven pins, corrupted one at a time
#   tamper     a pinned artifact whose BYTES drifted under a correct pin
#   unverified a pin declared but never verified (the provenance.py class)
#   missing    a verify_one for a file that is not there
#   count      the driver's announced artifact count drifting from reality
#
# No root, no service, no write outside /var/tmp.
#
# Usage: bash script-tests-execute-chain.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
S6="$(cd "$HERE/.." && pwd)"
PROOF="$HERE/script-tests-execute.sh"
SANDBOX=$(mktemp -d /var/tmp/s6-chain-control.XXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0
ck() { if [ "$2" = "$3" ]; then echo "PASS  $1"; pass=$((pass+1));
       else echo "FAIL  $1: got [$2] want [$3]"; fail=$((fail+1)); fi; }

# A minimal mirror: the driver plus every file it pins. The proof resolves
# the chain relative to the directory of the script under test, so the mirror
# must hold the real bytes for the chain to be checkable at all.
mirror() {  # dest
  local dest=$1
  mkdir -p "$dest/staging-5427ded"
  cp "$S6/s6-t2-execute.sh" "$dest/"
  cp "$S6/collect.py" "$S6/liveness.py" "$S6/provenance.py" "$dest/"
  cp "$S6/activate-cluster-5427ded.sh" "$S6/rollback-cluster-5427ded.sh" "$dest/"
  cp "$S6/staging-5427ded/MANIFEST.sha256" \
     "$S6/staging-5427ded/install-slot.sh" "$dest/staging-5427ded/"
}

# The static half only: the runtime half is exercised by script-tests-execute.sh
# itself and would report the same mutation twice, for a different reason.
static_half() {  # driver-path
  bash "$PROOF" "$1" 2>&1 | sed -n '/^== A\. static invariants/,/^== B\./p'
}

# The static half must report this failure. The wanted text is matched
# INSIDE a FAIL line: the check labels carry their own prefix, so a literal
# "FAIL  <text>" match would miss a real catch.
expect_fail() {  # label driver-path expected-substring
  local label=$1 drv=$2 want=$3 out
  out=$(static_half "$drv")
  if printf '%s\n' "$out" | grep '^FAIL' | grep -qF "$want"; then
    echo "PASS  $label"; pass=$((pass+1))
  else
    echo "FAIL  $label: the static half did not report [$want]"
    fail=$((fail+1))
    printf '%s\n' "$out" | sed 's/^/          /'
  fi
}

echo "== positive control: the shipped driver passes the static half"
M=$SANDBOX/positive; mirror "$M"
out=$(static_half "$M/s6-t2-execute.sh")
ck "the unmutated driver reports no static failure" \
   "$(printf '%s\n' "$out" | grep -c '^FAIL')" "0"
ck "the static half checked at least seven artifacts" \
   "$([ "$(printf '%s\n' "$out" | grep -cE '^PASS  [A-Z0-9_]+_SHA matches ')" \
        -ge 7 ] && echo yes || echo no)" "yes"

echo
echo "== each pin, corrupted one at a time"
for var in MANIFEST_SHA INSTALLER_SHA ACTIVATE_SHA ROLLBACK_SHA \
           COLLECT_SHA LIVENESS_SHA PROVENANCE_SHA; do
  M=$SANDBOX/pin-$var; mirror "$M"
  sed -i "s|^${var}=.*|${var}=0000000000000000000000000000000000000000000000000000000000000000|" \
    "$M/s6-t2-execute.sh"
  expect_fail "a corrupted $var is caught" "$M/s6-t2-execute.sh" \
    "$var matches "
done

echo
echo "== a pinned artifact whose bytes drifted under a correct pin"
M=$SANDBOX/tampered; mirror "$M"
printf '\n# tampered\n' >> "$M/provenance.py"
expect_fail "a tampered provenance.py is caught" "$M/s6-t2-execute.sh" \
  "PROVENANCE_SHA matches provenance.py"

echo
echo "== a pin declared but never verified"
M=$SANDBOX/unverified; mirror "$M"
sed -i 's|^PROVENANCE_SHA=.*|&\
EXTRA_SHA=1111111111111111111111111111111111111111111111111111111111111111|' \
  "$M/s6-t2-execute.sh"
expect_fail "a declared but unverified pin is caught" "$M/s6-t2-execute.sh" \
  "every declared hash pin is verified by the driver"

echo
echo "== a verify_one for a file that is not there"
M=$SANDBOX/missing; mirror "$M"
sed -i '/^verify_one "\$S6\/provenance.py"/i verify_one "$S6/not-here.py" "$PROVENANCE_SHA"' \
  "$M/s6-t2-execute.sh"
expect_fail "a verify_one for a missing file is caught" "$M/s6-t2-execute.sh" \
  "names a file that exists"

echo
echo "== the announced artifact count drifting from reality"
M=$SANDBOX/count; mirror "$M"
sed -i 's/all seven artifacts verified/all six artifacts verified/' \
  "$M/s6-t2-execute.sh"
expect_fail "a stale artifact count is caught" "$M/s6-t2-execute.sh" \
  "announced artifact count matches what it verifies"

echo
echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
