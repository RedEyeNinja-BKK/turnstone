# shellcheck shell=bash
# storage-policy.sh — shared policy for the storage monitor and the pruner.
#
# Sourced, not executed. Keeping the policy in one place is deliberate: the
# previous incident happened partly because the watcher and the reality on disk
# disagreed (it watched 1 of 5 trees, and globbed /tmp while TMPDIR was
# /var/tmp). Two scripts with two copies of the policy would recreate that.

# Every Rust target root on this host. One explicit entry per tree — a glob
# here would silently adopt the next stray /var/tmp/sytarget-* a session
# invents, which is the behaviour we are trying to stop.
if [ -z "${STORAGE_TARGETS+x}" ]; then STORAGE_TARGETS=(
  /var/lib/turnstone/sytarget
  /var/lib/turnstone/switchyard-upstream/recovered-6de85da1/target
)
fi

# Roots that are NOT expected to exist but must be detected if they reappear:
# an ad-hoc CARGO_TARGET_DIR is exactly how 43 GiB appeared before.
STORAGE_UNEXPECTED_PATTERNS=(
  /var/tmp/sytarget
  /var/tmp/sytarget-*
  /var/tmp/*target-integration
)

# pytest sandboxes. BOTH locations: /tmp is a 1.9 GiB tmpfs and TMPDIR is
# /var/tmp, so a single-location glob misses everything that matters.
if [ -z "${STORAGE_PYTEST_GLOBS+x}" ]; then STORAGE_PYTEST_GLOBS=(
  /var/tmp/pytest-of-*
  /tmp/pytest-of-*
)
fi

# Thresholds, on the filesystem that holds the targets.
STORAGE_WARN_PCT=${STORAGE_WARN_PCT:-70}
STORAGE_PRUNE_PCT=${STORAGE_PRUNE_PCT:-85}
STORAGE_HARD_PCT=${STORAGE_HARD_PCT:-92}

# Must survive every branch, including a future edit that widens a glob.
# Losing any of these is worse than a full disk.
STORAGE_PROTECTED=(
  /var/lib/turnstone/backups
  /var/lib/turnstone/artifacts
  /var/lib/turnstone/staging
  /var/lib/turnstone/canary
  /var/lib/turnstone/sensor-runtime
  /var/lib/turnstone/credentials
  /var/lib/turnstone/github
  /var/lib/turnstone/source
  /var/lib/turnstone/switchyard-upstream/recovered-6de85da1/.git
  /var/lib/turnstone/switchyard-upstream/recovered-6de85da1/recovered
  /var/lib/turnstone/switchyard-upstream/recovered-6de85da1/target/release
  /var/lib/turnstone/integrations
  /var/lib/turnstone/builds
  /var/lib/turnstone/.rustup
  /var/lib/turnstone/.cargo
  /opt/turnstone-overlays
  /opt/turnstone-venv
  /etc/turnstone
  /srv/turnstone
  /var/lib/postgresql
  /var/tmp/sy-integration
  /var/tmp/syrepo
  /var/tmp/sensor-audit
  /var/tmp/tmp0a8g4oud
  /var/tmp/t2.bundle
  /var/tmp/lc-rd-003-clone-backup
  /var/tmp/lc-rd-003-restore-test
)

# Alert only on paths this host is expected to have. The deployed v1 asserted
# every entry in one list and produced meaningless noise for paths that never
# existed here - which is how a real finding gets ignored.
STORAGE_REQUIRED=(
  /var/lib/turnstone/backups
  /var/lib/turnstone/artifacts
  /var/lib/turnstone/github
  /var/lib/turnstone/staging
  /var/lib/turnstone/canary
  /var/lib/turnstone/sensor-runtime
  /opt/turnstone-overlays
  /etc/turnstone
  /srv/turnstone/credentials
  /var/lib/postgresql
)

storage_log_file() {
  printf '%s' "${STORAGE_LOG:-/var/lib/turnstone/storage-events.log}"
}

# Structured event line: timestamp, level, event, then key=value pairs.
# Both scripts emit the same shape so one log can be parsed or grepped.
storage_emit() {
  local level=$1 event=$2; shift 2
  local line
  line="$(date -u +%FT%TZ) level=$level event=$event"
  local kv
  for kv in "$@"; do line+=" $kv"; done
  # In JSON mode the caller is a machine parser, so events go to stderr and
  # stdout carries only the JSON document. Mixing them silently breaks parsing.
  if [ "${STORAGE_JSON:-0}" -eq 1 ]; then
    printf '%s\n' "$line" >&2
  else
    printf '%s\n' "$line"
  fi
  { printf '%s\n' "$line" >>"$(storage_log_file)"; } 2>/dev/null || true
}

# Filesystem holding the first existing target, else /.
storage_anchor() {
  local t
  for t in "${STORAGE_TARGETS[@]}"; do
    if [ -e "$t" ]; then printf '%s' "$t"; return 0; fi
  done
  printf '/'
}

storage_used_pct() {
  df -P "$1" 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}'
}

storage_free_gib() {
  df -Pk "$1" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}'
}

# A build or test process may be writing underneath any target. This is the
# safety interlock for every destructive path.
# Is a build or test process running?
#
# `pgrep -f` on a substring is NOT safe here: it matches any process whose
# command line merely mentions the word, including the consumer of this very
# monitor (a JSON parser referencing a "pytest" field produced a false "build
# active"). The check looks at argv instead: the executable base name, or a
# `python -m pytest` style invocation, and nothing else.
storage_build_active() {
  local pid cmd a0 a1
  pgrep -x cargo >/dev/null 2>&1 && return 0
  pgrep -x rustc >/dev/null 2>&1 && return 0
  for pid in /proc/[0-9]*; do
    [ -r "$pid/cmdline" ] || continue
    cmd=$(tr '\0' '\n' < "$pid/cmdline" 2>/dev/null) || continue
    [ -n "$cmd" ] || continue
    a0=$(printf '%s\n' "$cmd" | sed -n '1p')
    a1=$(printf '%s\n' "$cmd" | sed -n '2p')
    case "${a0##*/}" in
      cargo|rustc|rustup|pytest) return 0 ;;
    esac
    # python -m pytest / python -m cargo ...
    case "${a0##*/}" in
      python|python3|python3.*)
        [ "$a1" = "-m" ] || continue
        case "$(printf '%s\n' "$cmd" | sed -n '3p')" in
          pytest|cargo|rustup) return 0 ;;
        esac ;;
    esac
  done
  return 1
}

# True when a path is a symlink or sits under a mount point that is not the
# root filesystem. Either condition makes `du` and `rm` mean something other
# than what the operator approved.
storage_is_unsafe_path() {
  local p=$1 real dev rootdev
  [ -L "$p" ] && return 0
  real=$(realpath -e -- "$p" 2>/dev/null) || return 1
  [ "$real" = "$p" ] || return 0
  rootdev=$(stat -c %d / 2>/dev/null) || return 1
  dev=$(stat -c %d "$p" 2>/dev/null) || return 1
  [ "$dev" = "$rootdev" ] || return 0
  # A nested mount point inside a target would be measured but not freed.
  if findmnt -rn -o TARGET 2>/dev/null | awk -v q="$p" 'index($0,q"/")==1{found=1} END{exit !found}'; then
    return 0
  fi
  return 1
}

storage_is_protected() {
  local p=$1 prot
  for prot in "${STORAGE_PROTECTED[@]}"; do
    case "$p" in
      "$prot"|"$prot"/*) return 0 ;;
    esac
    case "$prot" in
      "$p"/*) return 0 ;;   # p is a parent of something protected
    esac
  done
  return 1
}

# Staleness, corrected. The earlier draft tested `find -mtime +N | head -1`,
# which is true when ANY old file exists — so a tree with one stale byte and a
# thousand freshly-written files counted as stale. Stale must mean NOTHING has
# been touched recently.
storage_is_stale() {
  local p=$1 days=${2:-2}
  [ -d "$p" ] || return 1
  if [ -z "$(find "$p" -type f -newermt "-${days} days" 2>/dev/null | head -1)" ]; then
    return 0
  fi
  return 1
}

storage_size_mib() {
  du -xs --block-size=1M "$1" 2>/dev/null | cut -f1 || echo 0
}
