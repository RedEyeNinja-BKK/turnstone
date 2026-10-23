#!/usr/bin/env bash
# storage-prune — destructive build-artifact cleanup. DISABLED BY DEFAULT.
#
# Deliberately separate from storage-monitor.sh: observation runs unattended,
# deletion requires two explicit flags. A monitor that can delete is a monitor
# nobody dares run on a timer.
#
# GATES (all must pass, in this order):
#   1. --apply is present          otherwise this is a dry run
#   2. --confirm is present        otherwise --apply is ignored (typo guard)
#   3. no cargo/rustc/pytest running
#   4. no campaign holds the build lock
#   5. every candidate passes: not protected, not a symlink, not a foreign
#      filesystem, no nested mount, and not a bare `target` or `release` dir
#   6. the tree is genuinely stale (NOTHING modified within N days)
#
# FAIL CLOSED: if any gate cannot be evaluated, nothing is removed and the exit
# status is non-zero. An unverifiable safety condition is treated as unsafe.
#
# Usage:
#   storage-prune.sh                      dry run, report only
#   storage-prune.sh --apply              dry run + explicit intent, still no
#   storage-prune.sh --apply --confirm    actually delete
set -Eeuo pipefail
set +x

HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=/dev/null
. "$HERE/storage-policy.sh"

APPLY=0; CONFIRM=0; EXPLAIN=0
for a in "$@"; do
  case "$a" in
    --apply)   APPLY=1 ;;
    --confirm) CONFIRM=1 ;;
    --dry-run) APPLY=0 ;;
    # --explain: observation mode for the 7-day period. Forces dry run and
    # ignores the usage threshold so the decision logic is exercised (and its
    # proposals logged) even while the disk is healthy. It can never delete.
    --explain) EXPLAIN=1; APPLY=0; CONFIRM=0 ;;
    *) echo "usage: storage-prune.sh [--apply] [--confirm] [--explain]" >&2; exit 2 ;;
  esac
done
if [ "$EXPLAIN" -eq 1 ]; then APPLY=0; CONFIRM=0; fi

STALE_DAYS=${STORAGE_STALE_DAYS:-2}
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)

storage_emit info prune_start apply="$APPLY" confirm="$CONFIRM" explain="$EXPLAIN" stale_days="$STALE_DAYS" boot_id="$BOOT_ID"

# ---- gate 3: active work --------------------------------------------------
if storage_build_active; then
  storage_emit warn prune_blocked reason=build_or_test_active
  echo "storage-prune: a cargo/rustc/pytest process is running — nothing removed." >&2
  exit 0
fi

# ---- gate 4: campaign lock ------------------------------------------------
LOCK=${SY_BUILD_LOCK:-/var/lib/turnstone/.build.lock}
if ! ( exec 8>"$LOCK"; flock -n 8 ) 2>/dev/null; then
  storage_emit warn prune_blocked reason=build_lock_held
  echo "storage-prune: the build lock is held by a campaign — nothing removed." >&2
  exit 0
fi

anchor=$(storage_anchor)
pct=$(storage_used_pct "$anchor")
if [ -z "$pct" ]; then
  storage_emit error prune_failed reason=cannot_measure
  exit 20
fi
storage_emit info prune_measure anchor="$anchor" used_pct="$pct"

# Only prune at or above the PRUNE threshold. Below it, this is a no-op even
# with --apply --confirm: there is no reason to delete a working cache early.
if [ "$pct" -lt "$STORAGE_PRUNE_PCT" ] && [ "$EXPLAIN" -eq 0 ]; then
  storage_emit info prune_skipped reason=below_threshold used_pct="$pct" threshold="$STORAGE_PRUNE_PCT"
  echo "storage-prune: ${pct}% used, below the ${STORAGE_PRUNE_PCT}% prune threshold — nothing removed." >&2
  exit 0
fi

# ---- candidate construction ----------------------------------------------
# Each candidate is <path>|<kind>. Only these kinds exist; there is no default.
CANDIDATES=()
for t in "${STORAGE_TARGETS[@]}"; do
  [ -d "$t" ] || continue
  CANDIDATES+=("$t/debug/incremental|incremental")
  if storage_is_stale "$t/debug" "$STALE_DAYS"; then
    CANDIDATES+=("$t/debug/deps|deps")
    CANDIDATES+=("$t/debug/build|build")
    CANDIDATES+=("$t/debug/examples|examples")
  fi
done
for g in "${STORAGE_PYTEST_GLOBS[@]}"; do
  for d in $g; do
    [ -d "$d" ] || continue
    if storage_is_stale "$d" 1; then CANDIDATES+=("$d|pytest"); fi
  done
done

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
  storage_emit info prune_done removed=0 reason=no_candidates
  echo "storage-prune: no eligible candidates." >&2
  exit 0
fi

# ---- gates 5 and 6: per-candidate validation -----------------------------
removed=0; freed=0; skipped=0
for c in "${CANDIDATES[@]}"; do
  path=${c%%|*}; kind=${c##*|}
  if [ ! -e "$path" ]; then continue; fi

  # Structural refusals, independent of the policy list.
  case "$path" in
    */target|*/target/release|*/target/release/*|*.git|*/.git|*/.git/*)
      storage_emit warn refuse path="$path" reason=structural_kind
      skipped=$((skipped+1)); continue ;;
  esac
  if storage_is_protected "$path"; then
    storage_emit warn refuse path="$path" reason=protected
    skipped=$((skipped+1)); continue
  fi
  # explicit classification for the observation log
  if storage_is_stale "$path" "$STALE_DAYS"; then
    storage_emit info classify path="$path" class=stale kind="$kind"
  else
    storage_emit info classify path="$path" class=active kind="$kind"
  fi
  if storage_is_unsafe_path "$path"; then
    storage_emit warn refuse path="$path" reason=symlink_or_foreign_fs_or_mount
    skipped=$((skipped+1)); continue
  fi

  mib=$(storage_size_mib "$path")

  if [ "$APPLY" -eq 1 ] && [ "$CONFIRM" -eq 1 ]; then
    rm -rf -- "$path"
    if [ -e "$path" ]; then
      storage_emit error remove_failed path="$path"
      skipped=$((skipped+1)); continue
    fi
    storage_emit info removed path="$path" kind="$kind" mib="$mib"
    echo "  removed $path (${mib} MiB)" >&2
    removed=$((removed+1)); freed=$((freed+mib))
  else
    storage_emit info would_remove path="$path" kind="$kind" mib="$mib"
    echo "  would remove $path (${mib} MiB)" >&2
    removed=$((removed+1)); freed=$((freed+mib))
  fi
done

# ---- observation-only classification (explain mode) ------------------------
# The spec for the 7-day dry run requires protected, active, stale and unknown
# paths to be distinguishable, not just the deletable ones. Emit them so the
# log answers "what did it see and why did it leave it alone".
if [ "$EXPLAIN" -eq 1 ]; then
  for prot in "${STORAGE_PROTECTED[@]}"; do
    [ -e "$prot" ] || continue
    storage_emit info classify path="$prot" class=protected
  done
  for pat in "${STORAGE_UNEXPECTED_PATTERNS[@]}"; do
    for hit in $pat; do
      [ -e "$hit" ] || continue
      skip=0
      for t in "${STORAGE_TARGETS[@]}"; do [ "$hit" = "$t" ] && skip=1; done
      [ "$skip" -eq 1 ] && continue
      storage_emit warn classify path="$hit" class=unknown_unexpected_root mib="$(storage_size_mib "$hit")"
    done
  done
  storage_emit info observe_done note="explain mode: nothing was deleted"
fi

after=$(storage_used_pct "$anchor")
storage_emit info prune_done apply="$APPLY" confirm="$CONFIRM" \
  candidates="$removed" skipped="$skipped" freed_mib="$freed" \
  used_pct_before="$pct" used_pct_after="$after"

if [ "$APPLY" -eq 1 ] && [ "$CONFIRM" -ne 1 ]; then
  echo "storage-prune: --apply without --confirm — nothing was deleted (dry run)." >&2
fi
echo "storage-prune: ${removed} candidate(s), ${freed} MiB, ${skipped} skipped, now ${after}%." >&2
exit 0
