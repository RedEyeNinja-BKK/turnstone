#!/usr/bin/env bash
# Complete the single-writer handoff to the canonical runtime OAuth store.
#
# WHY THIS IS A SCRIPT AND NOT A SERIES OF TYPED COMMANDS
#   Two stores hold ONE ChatGPT OAuth grant. A rotating refresh token is
#   invalidated the moment a new one is issued, so if both stores are ever
#   usable, whichever refreshes second is silently revoked. The dangerous step
#   is not "copy a file" -- it is choosing the single writer and proving that
#   the other path is gone. That has to be one ordered, verified operation.
#
# ORDER OF OPERATIONS (do not reorder)
#   1. activation of the OAuth code          (operator GO, separate)
#   2. THIS handoff                          (single writer established)
#   3. registry rows -> openai_oauth         (scripts/migrate_openai_routes.py)
#
# Usage:
#   bash stage_siwc_handoff.sh check            # read-only
#   sudo bash stage_siwc_handoff.sh apply --confirm
#   bash stage_siwc_handoff.sh verify
set -Eeuo pipefail

RUNTIME=${SIWC_RUNTIME_ROOT:-/srv/turnstone/credentials/runtime/openai-siwc}
ORIGINAL=${SIWC_ORIGINAL_ROOT:-/var/lib/turnstone/github/siwc-creds}
# Every consumer that can WRITE a refresh. A reader that only reads an access
# token still forces a refresh on expiry, so it counts.
CONSUMER_REF="scripts/siwc_direct_oauth_probe.py"
REPO=${R2_REPO:-$(cd -- "$(dirname -- "$0")/.." && pwd)}

die() { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }
say() { printf '%s\n' "$*"; }

state_of() {  # dir -> one line of non-secret facts
  local d=$1 f="$1/credentials.json"
  [[ -f "$f" ]] || { printf 'absent'; return 0; }
  /opt/turnstone-venv/bin/python - "$f" <<'PY'
import hashlib, json, pathlib, sys, time
p = pathlib.Path(sys.argv[1]); raw = p.read_bytes(); d = json.loads(raw)
print(
    f"sha256={hashlib.sha256(raw).hexdigest()[:16]} "
    f"sub={str(d.get('sub',''))[:12]} "
    f"expires_in={int(d.get('expires_at',0))-int(time.time())}s "
    f"rt_len={len(str(d.get('refresh_token','')))} "
    f"state={d.get('state','(none)')} "
    f"mtime={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p.stat().st_mtime))}"
)
PY
}

check_age() {  # report whether a refresh has happened since the last measurement
  local f="$1/credentials.json"
  [[ -f "$f" ]] || return 0
  local now mt age
  now=$(date +%s); mt=$(stat -c %Y "$f"); age=$((now - mt))
  say "  age since last write: ${age}s"
}

cmd_check() {
  say "Runtime store:  $RUNTIME"
  [[ -d "$RUNTIME" ]] && say "  $(state_of "$RUNTIME")" || say "  absent"
  check_age "$RUNTIME"
  say "Original store: $ORIGINAL"
  [[ -d "$ORIGINAL" ]] && say "  $(state_of "$ORIGINAL")" || say "  absent"
  check_age "$ORIGINAL"
  say ""

  local a b
  a=$(state_of "$RUNTIME"); b=$(state_of "$ORIGINAL")
  if [[ "${a%% *}" == "${b%% *}" ]]; then
    say "CONSISTENT: both stores hold the same bytes."
  else
    say "DIVERGED: the stores no longer match."
    say "  A refresh has happened since the last measurement. The store whose mtime"
    say "  is newer is the writer; the other copy is now a DEAD credential (its"
    say "  refresh token was retired upstream). Do not merge them."
  fi
  say ""

  say "Writers that could refresh:"
  local found=0
  for pid in /proc/[0-9]*; do
    [[ -r "$pid/cmdline" ]] || continue
    # Match on a store PATH or the probe, not the bare word "siwc": this
    # script's own name contains it and would appear to be a writer.
    if tr '\0' ' ' < "$pid/cmdline" 2>/dev/null \
         | grep -qE 'oauth_probe|siwc-creds|openai-siwc'; then
      say "  RUNNING: ${pid#/proc/} $(tr '\0' ' ' < "$pid/cmdline" | cut -c1-100)"
      found=1
    fi
  done
  (( found )) || say "  none running"
  if grep -q '/var/lib/turnstone/github/siwc-creds' "$REPO/$CONSUMER_REF" 2>/dev/null; then
    say "  $CONSUMER_REF still DEFAULTS to the original store (SIWC_STORE_DIR overrides)"
  fi
  for f in "$ORIGINAL"/*.lock "$RUNTIME"/*.lock; do
    [[ -e "$f" ]] || continue
    say "  lock file present: $f"
  done
}

cmd_apply() {
  local confirm=0 arg
  for arg in "$@"; do
    case "$arg" in
      --confirm) confirm=1 ;;
      *) die "usage: $0 apply --confirm" ;;
    esac
  done
  (( confirm == 1 )) || die "refusing to hand off without --confirm"
  [[ "$EUID" -eq 0 ]] || die "must run as root (the original store may be root-adjacent)"

  [[ -d "$RUNTIME" && -f "$RUNTIME/credentials.json" ]] || die "runtime store missing: $RUNTIME"
  [[ -d "$ORIGINAL" && -f "$ORIGINAL/credentials.json" ]] || die "original store missing: $ORIGINAL"

  local a b
  a=$(state_of "$RUNTIME"); b=$(state_of "$ORIGINAL")
  say "runtime:  $a"
  say "original: $b"

  if [[ "${a%% *}" != "${b%% *}" ]]; then
    die "stores have DIVERGED; do not merge them. Copy the NEWER writer's file over the runtime store by hand, then re-run check."
  fi
  say "byte-identical: OK"

  # A running refresher would race the rename.
  if for pid in /proc/[0-9]*; do
    [[ "$pid" == "/proc/$$" ]] && continue
    [[ -r "$pid/cmdline" ]] || continue
    tr '\0' ' ' < "$pid/cmdline" 2>/dev/null \
      | grep -qE 'oauth_probe|siwc-creds|openai-siwc' && echo "$pid"
  done | grep -q .; then
    die "a refresh-capable process is running; stop it first"
  fi

  local stamp archive
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  archive="${ORIGINAL}.archived-${stamp}"
  [[ -e "$archive" ]] && die "archive target already exists: $archive"

  # Rename, never delete: the bytes stay as a historical reference, and a
  # renamed path can no longer be refreshed by anything that still points at it.
  mv -- "$ORIGINAL" "$archive"
  say "archived: $ORIGINAL -> $archive"

  [[ ! -e "$ORIGINAL" ]] || die "original store still present after rename"
  say "single writer: $RUNTIME is now the only refreshable path"
  say ""
  say "Follow-up that must be done before the first direct-model request:"
  say "  set SIWC_STORE_DIR=$RUNTIME (or edit the default) for $CONSUMER_REF,"
  say "  or leave that probe unused. It must never point at the archive."
}

cmd_verify() {
  [[ -d "$RUNTIME" ]] || die "runtime store missing"
  [[ -f "$RUNTIME/credentials.json" ]] || die "runtime credentials missing"
  say "runtime: $(state_of "$RUNTIME")"
  local archives
  archives=$(find "$(dirname "$ORIGINAL")" -maxdepth 1 -name "$(basename "$ORIGINAL").archived-*" 2>/dev/null | sort)
  if [[ -z "$archives" ]]; then
    say "original store still in place at $ORIGINAL (handoff not done)"
  else
    say "archived copies (inactive, never refresh these):"
    while read -r d; do say "  $d"; done <<< "$archives"
  fi
  if [[ -e "$ORIGINAL" ]]; then
    say "VERIFY: NOT HANDED OFF"
    return 1
  fi
  local holders=0
  for pid in /proc/[0-9]*; do
    [[ -r "$pid/cmdline" ]] || continue
    if tr '\0' ' ' < "$pid/cmdline" 2>/dev/null | grep -q "$(basename "$ORIGINAL")"; then
      say "  WARNING: ${pid#/proc/} still references the archived path"
      holders=1
    fi
  done
  (( holders )) || say "no process references the archived path"
  say "VERIFY: single writer is $RUNTIME"
}

case "${1:-}" in
  check)  cmd_check ;;
  apply)  shift; cmd_apply "$@" ;;
  verify) cmd_verify ;;
  *) die "usage: $0 {check|apply --confirm|verify}" ;;
esac
