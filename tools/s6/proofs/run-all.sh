#!/usr/bin/env bash
# Run the S6 measurement proofs and the deployment-driver proofs.
#
# Everything here is pure logic or runs in a disposable sandbox under
# /var/tmp. Nothing here installs a slot, restarts a service, writes an
# epoch, or touches production telemetry.
#
# ONE file, two trees. The branch keeps the proofs in `tools/s6/proofs/` and
# the driver's own tests in `tools/s6/driver/`; the deployed artifact keeps
# both in `s6/evidence/`. This runner resolves the layout it finds, so the
# source and the artifact run the SAME proof set from the SAME bytes - the
# two copies are byte-identical, and a proof added on one side cannot be
# missing on the other.
#
# Usage: bash tools/s6/proofs/run-all.sh
#        bash s6/evidence/script-tests-proofs.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S6="$(cd "$HERE/.." && pwd)"
PY=${PY:-/opt/turnstone-venv/bin/python}

total_pass=0
total_fail=0
rc=0

for proof in liveness_proof.py collector_proof.py t1_exclusion_proof.py \
             epoch_selection_proof.py watch_condition_proof.py \
             watch_lifetime_proof.py provenance_proof.py \
             gate_provenance_proof.py boundary_marker_proof.py \
             rate_basis_proof.py provenance_required_proof.py \
             matching_cardinality_proof.py \
             source_artifact_proof.py \
             handoff_digest_proof.py \
             staleness_control.py; do
  echo "=== $proof"
  out=$("$PY" "$HERE/$proof" 2>&1)
  code=$?
  echo "$out" | grep -E '^(FAIL|---|pass=)' || true
  line=$(echo "$out" | grep -E '^pass=' | tail -1)
  p=${line#pass=}; p=${p%% *}
  f=${line#*fail=}
  total_pass=$((total_pass + ${p:-0}))
  total_fail=$((total_fail + ${f:-0}))
  if [ "$code" -ne 0 ]; then
    echo "  -> $proof EXITED $code"
    echo "$out" | grep -E '^FAIL' | head -20
    rc=1
  fi
done

echo
echo "=== the deployment driver's own proofs"
for driver_proof in script-tests-execute.sh script-tests-execute-chain.sh; do
  echo "--- $driver_proof"
  # The driver's tests live in `driver/` on the branch and in `evidence/` in
  # the deployed artifact. Whichever tree this runner is placed in, the test
  # is found next to it or one level up.
  if [ -f "$S6/driver/$driver_proof" ]; then
    driver_path="$S6/driver/$driver_proof"
  elif [ -f "$HERE/$driver_proof" ]; then
    driver_path="$HERE/$driver_proof"
  else
    echo "  -> $driver_proof NOT FOUND"
    rc=1
    continue
  fi
  out=$(bash "$driver_path" "$S6/s6-t2-execute.sh" 2>&1)
  code=$?
  echo "$out" | grep -E '^(FAIL|pass=)' || true
  line=$(echo "$out" | grep -E '^pass=' | tail -1)
  p=${line#pass=}; p=${p%% *}
  f=${line#*fail=}
  total_pass=$((total_pass + ${p:-0}))
  total_fail=$((total_fail + ${f:-0}))
  if [ "$code" -ne 0 ]; then
    echo "  -> $driver_proof EXITED $code"
    echo "$out" | grep -E '^FAIL' | head -20
    rc=1
  fi
done

echo
echo "=== proofs: pass=$total_pass fail=$total_fail"
[ "$rc" -eq 0 ] || echo "PROOF FAILURE"
exit "$rc"
