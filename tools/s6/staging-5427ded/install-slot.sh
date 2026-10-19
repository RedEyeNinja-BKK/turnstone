#!/usr/bin/env bash
# Install (or verify) the S6-t2 sensor candidate as an immutable overlay slot.
#
# RUN AS ROOT. This script does NOT activate anything: it creates a root-owned,
# read-only copy of a verified staging tree, or verifies one that already
# exists. It never writes a systemd drop-in, never changes an environment,
# never restarts a service and never opens a measurement epoch.
#
# Usage:
#   install-slot.sh verify   # staging tree vs manifest + anchors (slot untouched)
#   install-slot.sh ensure   # IDEMPOTENT: install if absent, verify if present,
#                            # refuse if a present slot differs from the expected one
#   install-slot.sh install  # strict first install: refuses if the slot exists
#   install-slot.sh rollback # remove the slot (only if no drop-in references it)
#
# `ensure` is what the root handoff uses. A run that stopped on exit 5 (busy
# cluster) may leave the slot installed; the next run must not require the
# operator to skip a step by hand.
set -euo pipefail

SLOT_NAME="sensor-v187-5427ded"
STAGE="/var/lib/turnstone/staging/${SLOT_NAME}"
TARGET="/opt/turnstone-overlays/${SLOT_NAME}"
ART="/var/lib/turnstone/artifacts/shadow-sensor-recovery-20261017"
MANIFEST="$ART/s6/staging-5427ded/MANIFEST.sha256"
# The manifest's own digest. Pinning it means "verify this slot against THIS
# list of 444 hashes", not "trust whatever manifest is lying next to it".
EXPECT_MANIFEST_SHA="f785179fad1963d541c67bf22a22dd1916a58960bace195c5666c8ef1fbb267c"
# Composition anchor: the slot must be the live R2 overlay + exactly the 8
# sensor files.
R2="/opt/turnstone-overlays/openai-oauth-v187-r2"
# Change anchor: the slot must be the RUNNING cb7fece slot + exactly the 4
# files this candidate changes.
PREV="/opt/turnstone-overlays/sensor-v187-cb7fece"
ENTRIES=444
# Ownership the slot must have. Root by default and asserted as such; the
# variables exist so the sandbox suite can drive the SAME ownership check
# against a tree it owns, instead of skipping it.
EXPECT_OWNER=${S6_SLOT_OWNER:-root}
EXPECT_GROUP=${S6_SLOT_GROUP:-root}

usage() { echo "usage: $0 {verify|ensure|install|rollback}" >&2; exit 2; }
[ $# -eq 1 ] || usage

# Root is required only for the actions that WRITE. `verify` is a read-only
# comparison and is deliberately usable by the service account, so a reader can
# confirm the staged tree without borrowing root.
need_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root for: $1" >&2; exit 1; }
}

count_differences() {  # base tree other tree -> differing entry count
  local diff_output rc=0
  # diff exits 1 when the trees differ, which is the expected outcome. Under
  # `set -o pipefail` that status escapes the command substitution and aborts
  # the script before the count is inspected (the S5 defect, seen twice).
  diff_output=$(diff -r --exclude=__pycache__ -q "$1" "$2" 2>/dev/null) || rc=$?
  [ "$rc" -le 1 ] || { echo "cannot compare $1 with $2 (diff status $rc)" >&2; return 1; }
  if [ -n "$diff_output" ]; then printf '%s\n' "$diff_output" | wc -l; else echo 0; fi
}

verify_manifest_file() {
  [ -f "$MANIFEST" ] || { echo "manifest missing: $MANIFEST" >&2; exit 1; }
  local actual
  actual=$(sha256sum "$MANIFEST" | cut -d' ' -f1)
  [ "$actual" = "$EXPECT_MANIFEST_SHA" ] || {
    echo "manifest checksum mismatch" >&2
    echo "  expected $EXPECT_MANIFEST_SHA" >&2
    echo "  actual   $actual" >&2
    exit 1
  }
}

verify_capability() {  # tree
  local tree=$1
  PYTHONPATH="$tree:/opt/turnstone-venv/lib/python3.13/site-packages" \
    PYTHONDONTWRITEBYTECODE=1 /opt/turnstone-venv/bin/python -c '
import inspect, sys
from turnstone.core import sensor_lifecycle as L
from turnstone.core.session import _sensor_observe_substantive as seam
assert hasattr(L, "attachment_projection"), "no attachment projection"
assert "attachments" in inspect.signature(L.is_substantive_instruction).parameters, \
    "substance test does not accept attachments"
src = inspect.getsource(seam)
assert "attachments=attachments" in src, "send seam does not thread attachments"
assert "if not _sensor_lifecycle.ENABLED:" in src, "no disabled-cost gate"
mod = sys.modules["turnstone.core.sensor_lifecycle"].__file__
assert mod.startswith(sys.argv[1] + "/"), f"imported from {mod}"
' "$tree" || { echo "staged tree lacks the attachment seam" >&2; exit 1; }
}

verify_stage() {
  verify_manifest_file
  [ -d "$STAGE" ] || { echo "staging tree missing: $STAGE" >&2; exit 1; }
  ( cd "$STAGE" && sha256sum --quiet -c "$MANIFEST" ) \
    || { echo "staging tree does not match its manifest" >&2; exit 1; }
  local count
  count=$(wc -l < "$MANIFEST")
  [ "$count" -eq "$ENTRIES" ] || {
    echo "manifest has $count entries, expected $ENTRIES" >&2; exit 1; }

  local vs_r2 vs_prev
  vs_r2=$(count_differences "$R2" "$STAGE")
  vs_prev=$(count_differences "$PREV" "$STAGE")
  [ "$vs_r2" -eq 8 ] || {
    echo "expected 8 differences from the R2 overlay, found $vs_r2" >&2; exit 1; }
  [ "$vs_prev" -eq 4 ] || {
    echo "expected 4 differences from the cb7fece predecessor, found $vs_prev" >&2
    diff -r --exclude=__pycache__ -q "$PREV" "$STAGE" >&2 || true
    exit 1; }
  verify_capability "$STAGE"

  echo "verify: manifest OK ($count entries, sha256 $EXPECT_MANIFEST_SHA)"
  echo "verify: R2 + 8 sensor files (composition anchor)"
  echo "verify: cb7fece + 4 files (change anchor)"
  echo "verify: attachment seam present in the staged bytes"
}

# Verify an INSTALLED slot: content, then the immutability posture a slot is
# supposed to have. A slot that verifies its manifest but is world-writable is
# not immutable and must not be trusted.
verify_installed_slot() {
  local target=$1
  [ -d "$target" ] || { echo "slot missing: $target" >&2; return 1; }
  ( cd "$target" && sha256sum --quiet -c "$MANIFEST" ) \
    || { echo "installed slot does not match the expected manifest" >&2; return 1; }

  local bad_owner bad_dirs bad_files
  bad_owner=$(find "$target" \( ! -user "$EXPECT_OWNER" -o ! -group "$EXPECT_GROUP" \) -print | wc -l)
  bad_dirs=$(find "$target" -type d ! -perm 0555 -print | wc -l)
  bad_files=$(find "$target" -type f ! -perm 0444 -print | wc -l)
  if [ "$bad_owner" -ne 0 ] || [ "$bad_dirs" -ne 0 ] || [ "$bad_files" -ne 0 ]; then
    echo "installed slot is not immutable:" >&2
    echo "  entries not $EXPECT_OWNER:$EXPECT_GROUP : $bad_owner" >&2
    echo "  directories not mode 0555    : $bad_dirs" >&2
    echo "  files not mode 0444          : $bad_files" >&2
    find "$target" \( ! -user "$EXPECT_OWNER" -o ! -group "$EXPECT_GROUP" -o \
         \( -type d ! -perm 0555 \) -o \( -type f ! -perm 0444 \) \) \
         -print 2>/dev/null | head -20 >&2
    return 1
  fi

  local files
  files=$(find "$target" -type f | wc -l)
  echo "verify: installed slot matches the manifest ($files files)"
  echo "verify: $EXPECT_OWNER:$EXPECT_GROUP, dirs 0555, files 0444"
}

install_slot() {
  verify_stage
  if [ -e "$TARGET" ]; then
    echo "slot already exists: $TARGET (immutable slots are never overwritten)" >&2
    exit 1
  fi
  cp -a "$STAGE" "$TARGET"
  find "$TARGET" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  find "$TARGET" -name '*.pyc' -delete 2>/dev/null || true
  chown -R "$EXPECT_OWNER:$EXPECT_GROUP" "$TARGET"
  find "$TARGET" -type d -exec chmod 0555 {} +
  find "$TARGET" -type f -exec chmod 0444 {} +
  if ! verify_installed_slot "$TARGET"; then
    echo "installed slot failed its own verification; removing" >&2
    chmod -R u+w "$TARGET"; rm -rf "$TARGET"; exit 1
  fi
  echo "installed immutable slot: $TARGET"
  echo "NOT activated. No drop-in, no env change, no restart, no epoch."
}

ensure_slot() {
  verify_manifest_file
  if [ ! -e "$TARGET" ]; then
    echo "slot absent -> installing"
    install_slot
    return 0
  fi

  echo "slot present -> verifying the INSTALLED bytes (no write)"
  if verify_installed_slot "$TARGET"; then
    echo "existing slot is the expected one; continuing without changes"
    return 0
  fi

  echo "" >&2
  echo "REFUSING: $TARGET exists but is not the expected slot." >&2
  echo "  Nothing was overwritten. A different slot is either a newer build or" >&2
  echo "  local drift; both need a human decision, not an automatic replace." >&2
  echo "  Inspect it:" >&2
  echo "    ( cd $TARGET && sha256sum -c $MANIFEST | head )" >&2
  echo "  To replace it deliberately, remove it first:" >&2
  echo "    $0 rollback" >&2
  exit 1
}

case "$1" in
  verify)  verify_stage ;;
  ensure)  need_root ensure; ensure_slot ;;
  install) need_root install; install_slot ;;
  rollback)
    need_root rollback
    if [ -e "$TARGET" ]; then
      if grep -Rqs "$SLOT_NAME" /etc/systemd/system/turnstone-*.service.d/ 2>/dev/null; then
        echo "slot is referenced by a systemd drop-in; remove that first" >&2
        exit 1
      fi
      chmod -R u+w "$TARGET"
      rm -rf "$TARGET"
      echo "removed $TARGET"
    else
      echo "nothing to remove: $TARGET"
    fi
    ;;
  *) usage ;;
esac
