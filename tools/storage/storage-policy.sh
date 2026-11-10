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
#
# REQUIRED targets: a missing entry is a measurement failure, never a zero and
# never a fallback to /. A monitor that quietly measured / on behalf of an
# absent build tree reported "healthy" for a host that had lost its target,
# which is the defect this list is shaped to prevent.
if [ -z "${STORAGE_TARGETS+x}" ]; then STORAGE_TARGETS=(
  /var/lib/turnstone/sytarget
)
fi

# OPTIONAL targets: measured when present, but their absence is expected. A
# recovered release tree can be removed legitimately, so it must not make the
# monitor fail for ever.
if [ -z "${STORAGE_OPTIONAL_TARGETS+x}" ]; then STORAGE_OPTIONAL_TARGETS=(
  /var/lib/turnstone/switchyard-upstream/recovered-6de85da1/target
)
fi

# A caller that overrides these through the environment (the test suite does)
# can only pass one string. Split it so a multi-path override behaves like the
# array it replaces. Build paths do not contain spaces.
#
# `declare -p` decides whether the name is already an array: under `set -u`,
# `${#name[@]}` on a plain scalar raises "unbound variable", so it cannot be
# used as the test.
storage_normalise_targets() {
  local name=$1 decl
  local -n targets_ref=$name
  decl=$(declare -p "$name" 2>/dev/null) || return 0
  case "$decl" in
    "declare -a"*|"declare -A"*) return 0 ;;
  esac
  [ -n "${targets_ref:-}" ] || return 0
  local -a split_targets
  read -r -a split_targets <<<"${targets_ref}"
  targets_ref=("${split_targets[@]}")
}
storage_normalise_targets STORAGE_TARGETS
storage_normalise_targets STORAGE_OPTIONAL_TARGETS

# Build roots that live on the build SSD. Not in STORAGE_TARGETS by default
# because the SSD may be unmounted; they are discovered by storage_target_list
# only when the mount is genuinely a mounted filesystem.
STORAGE_SSD_MOUNT=${STORAGE_SSD_MOUNT:-/mnt/turnstone-build-ssd}
STORAGE_SSD_TARGET_ROOT=${STORAGE_SSD_TARGET_ROOT:-$STORAGE_SSD_MOUNT/targets}

# True only when the build SSD is an actually mounted filesystem.
#
# A directory that merely exists under the root filesystem is not a mount: a
# plain `[ -d ]` test treated one as an SSD, so root-filesystem bytes were
# counted as build output and a missing mount looked healthy. The check is an
# exact mount-point match against the kernel's own table plus a device
# comparison with /, and it fails closed when either cannot be read.
#
# findmnt --first-only is deliberately not used: on a systemd automount it
# returns the autofs trigger rather than the real mount.
storage_ssd_mounted() {
  local mount=${STORAGE_SSD_MOUNT:-/mnt/turnstone-build-ssd} rootdev dev
  [ -d "$mount" ] || return 1
  awk -v want="$mount" '
    { p = $5; gsub(/\\040/, " ", p); gsub(/\\011/, "\t", p); gsub(/\\012/, "\n", p)
      if (p == want) found = 1 }
    END { exit(found ? 0 : 1) }
  ' /proc/self/mountinfo 2>/dev/null || return 1
  rootdev=$(stat -c %d / 2>/dev/null) || return 1
  dev=$(stat -c %d "$mount" 2>/dev/null) || return 1
  # A bind mount of a root-filesystem directory has the same device id, and its
  # contents are not separable from the root filesystem.
  [ "$dev" != "$rootdev" ]
}

# Required roots that are absent. Callers must treat a non-empty result as a
# measurement failure rather than proceeding with whatever remains.
storage_missing_required_targets() {
  local t
  for t in "${STORAGE_TARGETS[@]}"; do
    [ -e "$t" ] || printf '%s\n' "$t"
  done
}

# True when a path is one of the configured targets (required or optional), so
# a legitimate optional root is never reported as an unexpected build root.
storage_target_is_known() {
  local t
  for t in "${STORAGE_TARGETS[@]}" "${STORAGE_OPTIONAL_TARGETS[@]}"; do
    [ "$t" = "$1" ] && return 0
  done
  return 1
}

# Return every target root to measure: the configured roots that exist plus any
# live per-worktree target directory on the build SSD. The SSD entry is
# discovered only when the device is genuinely mounted.
storage_target_list() {
  local t w
  for t in "${STORAGE_TARGETS[@]}" "${STORAGE_OPTIONAL_TARGETS[@]}"; do
    [ -e "$t" ] && printf '%s\n' "$t"
  done
  if storage_ssd_mounted; then
    for w in "$STORAGE_SSD_TARGET_ROOT"/*/; do
      [ -d "$w" ] && printf '%s\n' "$w"
    done
  fi
}

# Roots that are NOT expected to exist but must be detected if they reappear:
# an ad-hoc CARGO_TARGET_DIR is exactly how 43 GiB appeared before.
#
# Overridable by environment, like STORAGE_TARGETS and STORAGE_PYTEST_GLOBS, so
# the detection path can be exercised against a fixture. The defaults are what
# production uses; a cron/systemd run never sets this.
if [ -z "${STORAGE_UNEXPECTED_PATTERNS+x}" ]; then STORAGE_UNEXPECTED_PATTERNS=(
  /var/tmp/sytarget
  /var/tmp/sytarget-*
  /var/tmp/*target-integration
); fi

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
  # The live credentials directory is under /srv, not /var/lib/turnstone.
  /srv/turnstone/credentials
  /var/lib/turnstone/github
  /var/lib/turnstone/source
  /var/lib/turnstone/switchyard-upstream/recovered-6de85da1/.git
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
  # A durable tool install that happens to live in /var/tmp. Sourced by
  # ~/.profile; removing it would silently break agent tooling.
  /var/tmp/uv0918
)

# Additive only. A caller may ADD paths to the never-delete set (the test suite
# does this to put a fixture under a protected prefix); it can never shrink the
# set, so no environment variable can remove a protection. STORAGE_PROTECTED
# itself is deliberately not overridable for that reason.
if [ -n "${STORAGE_PROTECTED_EXTRA:-}" ]; then
  read -r -a _storage_protected_extra <<<"$STORAGE_PROTECTED_EXTRA"
  STORAGE_PROTECTED+=("${_storage_protected_extra[@]}")
  unset _storage_protected_extra
fi

# Alert only on paths this host is expected to have. The deployed v1 asserted
# every entry in one list and produced meaningless noise for paths that never
# existed here - which is how a real finding gets ignored.
# Consumed by storage-monitor.sh (missing-protected report), so it is unused
# within this library file only — not dead configuration.
# shellcheck disable=SC2034
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

# Structured diagnostic that must never contaminate a measurement's stdout.
# Always stderr plus the event log, in both plain and JSON modes, because a
# function whose stdout is a number has no other lawful place to report why it
# failed.
storage_emit_err() {
  local level=$1 event=$2; shift 2
  local line kv
  line="$(date -u +%FT%TZ) level=$level event=$event"
  for kv in "$@"; do line+=" $kv"; done
  printf '%s\n' "$line" >&2
  { printf '%s\n' "$line" >>"$(storage_log_file)"; } 2>/dev/null || true
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

# Filesystem anchor: the first target that exists.
#
# Prints nothing and returns 1 when no target exists. Defaulting to / made a
# missing build tree indistinguishable from a healthy host: the monitor
# measured the root filesystem's usage and reported OK.
storage_anchor() {
  local t
  for t in $(storage_target_list); do
    if [ -e "$t" ]; then printf '%s' "$t"; return 0; fi
  done
  return 1
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

# Three-way path validation for a destructive operation.
#
# The earlier two-way form returned "safe" when realpath or stat failed, so a
# broken resolution or an unreadable parent was read as permission to delete.
# Validation failure is now its own answer, and every caller refuses removal on
# it.
#
#   0  safe        resolvable, not a symlink, on the root filesystem, no nested mount
#   1  unsafe      a symlink, a relocated path, a foreign filesystem or a nested mount
#   2  unvalidated the checks themselves failed — treat as unsafe
storage_path_safety() {
  local p=$1 real dev rootdev mounts
  rootdev=$(stat -c %d / 2>/dev/null) || return 2
  [ -L "$p" ] && return 1
  real=$(realpath -e -- "$p" 2>/dev/null) || return 2
  [ "$real" = "$p" ] || return 1
  dev=$(stat -c %d "$p" 2>/dev/null) || return 2
  [ "$dev" = "$rootdev" ] || return 1
  # A nested mount point inside a target would be measured but not freed.
  mounts=$(findmnt -rn -o TARGET 2>/dev/null) || return 2
  if printf '%s\n' "$mounts" |
     awk -v q="$p" 'index($0,q"/")==1{found=1} END{exit !found}'; then
    return 1
  fi
  return 0
}

# Kept for callers that only ask the yes/no question. "Could not validate" is
# reported as unsafe, so this can never authorise a removal.
storage_is_unsafe_path() {
  local rc=0
  storage_path_safety "$1" || rc=$?
  [ "$rc" -ne 0 ]
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
#
# Returns three answers, because "the scan failed" is not "the tree is stale":
#   0  stale              nothing under the tree was written within the window
#   1  active             a fresh file exists
#   2  undeterminable     the scan itself failed (unreadable tree, I/O error)
# A caller making a destructive decision must treat 2 as ineligible. find is
# stopped with -quit rather than piped to head, so a SIGPIPE on the pipe cannot
# masquerade as a scan failure.
storage_is_stale() {
  local p=$1 days=${2:-2} fresh rc=0
  [ -d "$p" ] || return 1
  fresh=$(find "$p" -type f -newermt "-${days} days" -print -quit 2>/dev/null) || rc=$?
  # A fresh file is positive evidence, whatever the exit status says.
  [ -n "$fresh" ] && return 1
  [ "$rc" -ne 0 ] && return 2
  return 0
}

# storage_size_mib — measure one path in MiB, failing closed.
#
# Prints exactly one integer on stdout and returns 0 on success.
#
# Exit codes:
#   0  measured successfully
#   2  path does not exist (distinct from "measures zero")
#   3  du could not scan the path (permission error, missing mount, etc.)
#   4  du produced output that is not a single clean integer
#
# Never prints a partial size followed by a fallback "0": that was the defect
# that let an inaccessible directory report as zero usage with an OK verdict.
# An unmeasurable path is an error, not zero usage.
storage_size_mib() {
  local path=$1 raw line count
  [ -e "$path" ] || { storage_emit_err error size_missing path="$path"; return 2; }
  raw=$(du -xs --block-size=1M -- "$path" 2>/dev/null) || { storage_emit_err error size_unreadable path="$path"; return 3; }
  # du prints "size\tpath"; keep only the size field.
  line=$(printf '%s\n' "$raw" | cut -f1)
  count=$(printf '%s\n' "$line" | grep -c .)
  [ "$count" -eq 1 ] || { storage_emit_err error size_malformed path="$path" lines="$count"; return 4; }
  case "$line" in
    *[!0-9]*) storage_emit_err error size_malformed path="$path" value="$line"; return 4 ;;
  esac
  printf '%s\n' "$line"
}
