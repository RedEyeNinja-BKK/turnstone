#!/usr/bin/env bash
# rehearse.sh — run S6 proofs against the canonical evidence corpus.
#
# WHY THIS EXISTS
#   263 byte-identical copies of the S6 evidence corpus accumulated under
#   /var/tmp/tmp*/s6/ (3.4 GiB) because each rehearsal copied the corpus into a
#   fresh `mktemp -d` and never removed it. Every copy also carried a
#   .turnstone.db plus a 4 MB WAL.
#
#   The canonical corpus is:
#       /var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6
#   It is a strict superset of those copies and every diverging file in it is
#   newer. Proofs read it IN PLACE. Isolation is opt-in, campaign-scoped, and
#   removed by trap.
#
# USAGE
#   tools/s6/rehearse.sh                 # in place, read-only (default)
#   tools/s6/rehearse.sh --isolated      # one scratch copy, removed on exit
#   tools/s6/rehearse.sh --list          # show what would run
set -Eeuo pipefail

CANONICAL=${S6_EVIDENCE:-/var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017/s6}
MODE=inplace
case "${1:-}" in
  --isolated) MODE=isolated ;;
  --list)     MODE=list ;;
  "")         ;;
  *) echo "usage: rehearse.sh [--isolated|--list]" >&2; exit 2 ;;
esac

[ -d "$CANONICAL" ] || { echo "FATAL: canonical evidence corpus missing: $CANONICAL" >&2; exit 1; }

WORK=""
cleanup() {
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then
    rm -rf -- "$WORK"
    echo "rehearse: removed scratch copy $WORK" >&2
  fi
}
# Trap on normal exit, on failure, and on the signals bash can catch. SIGKILL
# cannot be trapped; that is why --isolated scratch lives under the managed
# area and is also covered by the storage monitor's stale-artifact report.
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM HUP

PROOFS=(
  provenance_proof.py
  chain_closure.py
  collector_proof.py
  epoch_selection_proof.py
  gate_provenance_proof.py
  handoff_digest_proof.py
  liveness_proof.py
  matching_cardinality_proof.py
  provenance_required_proof.py
  rate_basis_proof.py
  source_artifact_proof.py
  staleness_control.py
  t1_exclusion_proof.py
  watch_condition_proof.py
  watch_lifetime_proof.py
)

run_one() {
  local root=$1 p=$2 path="$root/$2"
  if [ ! -f "$path" ]; then
    echo "  SKIP $p (not present)" >&2
    return 0
  fi
  echo "  RUN  $p" >&2
  ( cd "$root" && python3 "$p" ) || echo "  FAIL $p" >&2
}

case "$MODE" in
  list)
    for p in "${PROOFS[@]}"; do echo "  ${p}"; done
    ;;
  inplace)
    echo "rehearse: in place against $CANONICAL (nothing is copied)" >&2
    for p in "${PROOFS[@]}"; do run_one "$CANONICAL" "$p"; done
    ;;
  isolated)
    # One copy, in the managed area (not /var/tmp), removed on exit.
    BASE=${S6_SCRATCH_BASE:-/var/lib/turnstone/tmp}
    mkdir -p "$BASE"
    WORK=$(mktemp -d "$BASE/s6-rehearse-XXXXXX")
    echo "rehearse: scratch copy at $WORK (removed on exit)" >&2
    cp -a "$CANONICAL/." "$WORK/"
    for p in "${PROOFS[@]}"; do run_one "$WORK" "$p"; done
    ;;
esac
