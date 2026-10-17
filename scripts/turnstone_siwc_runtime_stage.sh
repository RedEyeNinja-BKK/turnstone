#!/usr/bin/env bash
# Stage the service-runtime credential subtree for the native OpenAI OAuth
# (SIWC) integration.
#
# WHY A SEPARATE SUBTREE
#   The canonical root /srv/turnstone/credentials keeps root-only categories
#   (api/, oauth/, providers/, grafana/, bootstrap/) at 0700 root:root, because a
#   root-owned launcher consumes them.  This integration is different: the
#   processes that must mint an access token -- turnstone-server,
#   turnstone-node@2..5 and turnstone-console -- run as the unprivileged
#   `turnstone` user.  Putting the credential under oauth/ would therefore make
#   it unreadable, and weakening oauth/ is explicitly forbidden.
#
#   runtime/ is the sanctioned additive pattern: root owns the parent, the
#   service owns its own private subtree.  It is the same reasoning that makes
#   github/*.pat group-readable while api/ stays root-only -- the mode follows
#   the consumer's identity, not a single house style.
#
# ACCESS BOUNDARIES (verified before staging; never widened in place)
#   /srv/turnstone/credentials            root:turnstone 0750  (unchanged)
#   /srv/turnstone/credentials/runtime    root:turnstone 0750
#   .../runtime/openai-siwc               turnstone:turnstone 0700
#   .../runtime/openai-siwc/credentials.json        0600 turnstone
#   .../runtime/openai-siwc/host_id                 0600 turnstone
#   /srv/turnstone/credentials/manifest.yaml        root-only (unchanged)
#
# WHAT THIS SCRIPT WILL NOT DO
#   * It never refreshes a token.  Copying a credential must not rotate it.
#   * It never prints, hashes or otherwise reveals credential contents.
#   * It never moves or deletes the working store; that store remains the
#     rollback anchor.
#   * It never changes a mode or owner that already exists.
#
# Usage:
#   sudo bash turnstone_siwc_runtime_stage.sh preflight
#   sudo bash turnstone_siwc_runtime_stage.sh stage
#   sudo bash turnstone_siwc_runtime_stage.sh status
#   bash turnstone_siwc_runtime_stage.sh cutover      # refuses: activation boundary
#
# Sandbox testing (no root, never touches the real store):
#   SIWC_STAGE_SANDBOX=1 SIWC_CANONICAL_ROOT=/var/tmp/x SIWC_SOURCE_STORE=/var/tmp/y ...
set -Eeuo pipefail
umask 077

CANONICAL_ROOT=${SIWC_CANONICAL_ROOT:-/srv/turnstone/credentials}
SOURCE_STORE=${SIWC_SOURCE_STORE:-/var/lib/turnstone/github/siwc-creds}
RUNTIME_DIR="$CANONICAL_ROOT/runtime"
TARGET="$RUNTIME_DIR/openai-siwc"
SERVICE_USER=${SIWC_SERVICE_USER:-turnstone}
SANDBOX=${SIWC_STAGE_SANDBOX:-0}

die() { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }
say() { printf '%s\n' "$*"; }

# ---- guardrails ------------------------------------------------------------
if [[ "$SANDBOX" == "1" ]]; then
  [[ "$CANONICAL_ROOT" == /var/tmp/* ]] ||
    die "sandbox mode requires SIWC_CANONICAL_ROOT under /var/tmp"
  [[ "$SOURCE_STORE" == /var/tmp/* ]] ||
    die "sandbox mode requires SIWC_SOURCE_STORE under /var/tmp"
  # A sandbox cannot chown to root; use the invoking identity so the same code
  # path (and therefore the same permissions logic) is exercised.
  PARENT_OWNER=$(id -un)
  PARENT_GROUP=$(id -gn)
else
  [[ "$EUID" -eq 0 ]] || die "must run as root (or set SIWC_STAGE_SANDBOX=1 for a /var/tmp test)"
  PARENT_OWNER=root
  PARENT_GROUP="$SERVICE_USER"
fi

require_source() {
  [[ -d "$SOURCE_STORE" ]] || die "source store missing: $SOURCE_STORE"
  [[ -f "$SOURCE_STORE/credentials.json" ]] || die "source credentials.json missing"
  [[ -f "$SOURCE_STORE/host_id" ]] || die "source host_id missing"
  # The credential must belong to the service user, not to root: a root-owned
  # copy would be unreadable to the process that must use it.
  if [[ "$SANDBOX" != "1" ]]; then
    local owner
    owner=$(stat -c '%U' "$SOURCE_STORE/credentials.json")
    [[ "$owner" == "$SERVICE_USER" ]] ||
      die "source credential owned by '$owner', expected '$SERVICE_USER'"
  fi
}

show_tree() {
  local path="$1"
  [[ -e "$path" ]] || { say "  (absent) $path"; return 0; }
  stat -c '  %A %U:%G %n' "$path"
}

cmd_preflight() {
  say "Canonical root : $CANONICAL_ROOT"
  require_source
  [[ -d "$CANONICAL_ROOT" ]] || die "canonical root missing: $CANONICAL_ROOT"

  # The parent must be the documented root:turnstone 0750 anchor.
  local parent_owner parent_mode parent_group
  parent_owner=$(stat -c '%U' "$CANONICAL_ROOT")
  parent_group=$(stat -c '%G' "$CANONICAL_ROOT")
  parent_mode=$(stat -c '%a' "$CANONICAL_ROOT")
  say "Parent owner   : $parent_owner:$parent_group $parent_mode"
  if [[ "$SANDBOX" != "1" ]]; then
    [[ "$parent_owner" == "root" && "$parent_group" == "$SERVICE_USER" && "$parent_mode" == "750" ]] ||
      die "canonical root is not root:$SERVICE_USER 0750; refusing to stage under a different policy"
    # oauth/ must stay root-only: this script must never be the reason it changes.
    if [[ -d "$CANONICAL_ROOT/oauth" ]]; then
      [[ "$(stat -c '%a' "$CANONICAL_ROOT/oauth")" == "700" ]] ||
        say "WARNING: oauth/ is not 0700; this script will not change it, investigate separately"
    fi
  fi

  if [[ -e "$TARGET" ]]; then
    say "Target         : EXISTS"
    show_tree "$TARGET"
    [[ -f "$TARGET/credentials.json" ]] &&
      die "target already holds a credential; refusing to overwrite an authoritative store"
  else
    say "Target         : absent (ready to stage)"
  fi
  say "Source store   : $SOURCE_STORE (preserved; not moved, not refreshed)"
  say "PREFLIGHT OK"
}

cmd_stage() {
  cmd_preflight >/dev/null
  install -d -o "$PARENT_OWNER" -g "$PARENT_GROUP" -m 0750 "$RUNTIME_DIR"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$TARGET"

  # Copy, never move: the working store remains the rollback anchor until the
  # operator verifies the cutover.
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 \
    "$SOURCE_STORE/credentials.json" "$TARGET/credentials.json"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 \
    "$SOURCE_STORE/host_id" "$TARGET/host_id"

  # Byte-exact copy check without ever printing content.
  cmp -s "$SOURCE_STORE/credentials.json" "$TARGET/credentials.json" ||
    die "copy verification failed for credentials.json"
  cmp -s "$SOURCE_STORE/host_id" "$TARGET/host_id" ||
    die "copy verification failed for host_id"

  # Non-secret provenance record for the inventory. No token, no account secret.
  {
    printf 'kind: siwc-runtime-credential\n'
    printf 'registration: direct-chatgpt-oauth\n'
    printf 'consumers: turnstone-server, turnstone-node@2-5, turnstone-console\n'
    printf 'refresh: coordinated (flock + re-read under lock + atomic replace)\n'
    printf 'source_store: %s\n' "$SOURCE_STORE"
    printf 'staged_at_utc: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'note: non-secret metadata only; no credential material is recorded here\n'
  } > "$TARGET/README"
  chown "$SERVICE_USER:$SERVICE_USER" "$TARGET/README"
  chmod 0640 "$TARGET/README"

  say "STAGED (inactive; no consumer points here yet)"
  show_tree "$RUNTIME_DIR"
  show_tree "$TARGET"
  stat -c '  %A %U:%G %s bytes %n' "$TARGET/credentials.json" "$TARGET/host_id" "$TARGET/README"
  say ""
  say "The working store at $SOURCE_STORE is UNCHANGED and still authoritative."
  say "No token was refreshed."
}

cmd_status() {
  say "Canonical root:"; show_tree "$CANONICAL_ROOT"
  say "Runtime subtree:"; show_tree "$RUNTIME_DIR"
  show_tree "$TARGET"
  for f in credentials.json host_id README; do
    [[ -e "$TARGET/$f" ]] && stat -c '  %A %U:%G %s bytes %n' "$TARGET/$f"
  done
  if [[ -f "$TARGET/credentials.json" && -f "$SOURCE_STORE/credentials.json" ]]; then
    if cmp -s "$TARGET/credentials.json" "$SOURCE_STORE/credentials.json"; then
      say "Runtime copy : IDENTICAL to the working store"
    else
      say "Runtime copy : DIFFERS from the working store (a refresh happened in one of them)"
      say "               Do NOT refresh against both copies. Decide which is authoritative."
    fi
  fi
  say "Working store: $SOURCE_STORE (preserved)"
}

cmd_cutover() {
  cat >&2 <<'MSG'
REFUSED: cutover is a production activation boundary and is not automated here.

Cutover would repoint every consumer at the runtime store, and the native
openai-oauth provider does not exist yet -- so there is nothing to repoint.
Completing it requires, in order:

  1. the provider/auth_mode wiring, so a consumer can actually mint a token;
  2. a verified canary workstream using the runtime store;
  3. an operator-approved window in which the consumers are restarted once.

Until then the working store remains authoritative. No credential was touched.
MSG
  exit 3
}

case "${1:-}" in
  preflight) [[ $# -eq 1 ]] || die "usage: $0 preflight"; cmd_preflight ;;
  stage)     [[ $# -eq 1 ]] || die "usage: $0 stage";     cmd_stage ;;
  status)    [[ $# -eq 1 ]] || die "usage: $0 status";    cmd_status ;;
  cutover)   cmd_cutover ;;
  *) die "usage: $0 {preflight|stage|status|cutover}" ;;
esac
