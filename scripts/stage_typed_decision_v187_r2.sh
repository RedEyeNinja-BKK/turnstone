#!/usr/bin/env bash
# Build the immutable typed-decision R2 candidate from PRISTINE 1.8.7 bytes.
#
# WHY A NEW SLOT
#   The active overlay /opt/turnstone-overlays/typed-decision-v187-finish is
#   live and is the rollback anchor. It must never be edited in place, and the
#   earlier stage script correctly refuses because its slot is occupied. R2
#   therefore lands in a new slot with a later 92-* drop-in.
#
# WHY SOME FILES ARE COPIED AND ONE IS PATCHED
#   The repository tree is 1.8.4-based. Copying its core/session.py over the
#   1.8.7 implementation would silently regress every 1.8.7 change in that
#   28k-line module. So:
#     * model_registry.py, output_guard_judge.py, cli.py, console/server.py are
#       applied as whole files ONLY after proving the repository base is
#       byte-identical to pristine 1.8.7; otherwise the script refuses.
#     * core/session.py is PATCHED line-by-line in the 1.8.7 source, asserting
#       exactly 5 replacements and 0 remaining.
#
# WHY THE CONSOLE IS INCLUDED
#   MEASURED: turnstone-console.service sets no PYTHONPATH and its
#   GET /v1/api/models builds the workstream picker from
#   storage.list_model_definitions() -- it imports neither ChatSession nor cli,
#   so it needs this one handler fix, not the whole typed-decision lane. Because
#   a PYTHONPATH overlay shadows the entire package, the console must point at a
#   complete package tree; it can therefore share the R2 slot.
#
# Usage (root):
#   sudo bash stage_typed_decision_v187_r2.sh stage
#   sudo bash stage_typed_decision_v187_r2.sh verify
#   sudo bash stage_typed_decision_v187_r2.sh status
#   sudo bash stage_typed_decision_v187_r2.sh activate    # refuses: operator gate
#
# Sandbox (no root, /var/tmp only) -- exercises the identical code path:
#   SIWC_STAGE_SANDBOX=1 R2_ROOT=/var/tmp/r2 bash stage_typed_decision_v187_r2.sh stage
set -Eeuo pipefail
umask 022

PY=${R2_PYTHON:-/opt/turnstone-venv/bin/python}
REPO=${R2_REPO:-$(cd -- "$(dirname -- "$0")/.." && pwd)}
NEW=${R2_ROOT:-/opt/turnstone-overlays/typed-decision-v187-r2}
SERVER_DROPIN=/etc/systemd/system/turnstone-server.service.d/92-typed-decision-v187-r2.conf
NODE_DROPIN="/etc/systemd/system/turnstone-node@.service.d/92-typed-decision-v187-r2.conf"
CONSOLE_DROPIN=/etc/systemd/system/turnstone-console.service.d/92-typed-decision-v187-r2.conf
SANDBOX=${R2_SANDBOX:-0}
SERVICE_USER=${R2_SERVICE_USER:-turnstone}
# Commit BEFORE the R2 corrections: its tree must be byte-identical to pristine
# 1.8.7 for the files we overlay. Comparing today's HEAD would be wrong -- HEAD
# carries the very changes we are staging.
BASE_COMMIT=${R2_BASE_COMMIT:-e28b5b15}
# The candidate supersedes the ACTIVE overlay, so that tree -- not pristine --
# is the correct base for files the overlay already carries. For
# core/output_guard_judge.py the repository base equals the OVERLAY (it carries
# the typed-decision patch) and differs from pristine; comparing pristine there
# would refuse a legitimately compatible file.
BASE_TREE=${R2_BASE_TREE:-/opt/turnstone-overlays/typed-decision-v187-finish/turnstone}

die() { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }

# A predictable path under a shared /var/tmp is a symlink and cross-user
# overwrite hazard for a root-run script. Everything transient lives in a
# private mktemp directory, removed on every exit path.
WORKDIR=$(mktemp -d /var/tmp/turnstone-r2.XXXXXXXX) || die "cannot create a private work directory"
chmod 0700 "$WORKDIR"
# The service identity needs its own tree. Placing a chowned subdirectory INSIDE
# a 0700 root-owned parent does NOT work: the unprivileged user then lacks the
# execute bit on the parent and cannot traverse in, so every staged test would
# fail for a permission reason unrelated to the code under test.
RUNDIR=""
cleanup() {
  [[ -n "${WORKDIR:-}" && -d "$WORKDIR" ]] && find "$WORKDIR" -mindepth 0 -delete 2>/dev/null
  [[ -n "${RUNDIR:-}" && -d "$RUNDIR" ]] && find "$RUNDIR" -mindepth 0 -delete 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM
say() { printf '%s\n' "$*"; }

if [[ "$SANDBOX" == "1" ]]; then
  [[ "$NEW" == /var/tmp/* ]] || die "sandbox requires R2_ROOT under /var/tmp"
  STAGE_OWNER=$(id -un); STAGE_GROUP=$(id -gn)
else
  [[ "$EUID" -eq 0 ]] || die "must run as root (or set R2_SANDBOX=1 with R2_ROOT under /var/tmp)"
  STAGE_OWNER=root; STAGE_GROUP=root
fi

# ``-I`` is REQUIRED: without it, CWD (and this repository) lands on sys.path,
# so ``import turnstone`` resolves to the 1.8.4 working tree instead of the
# installed 1.8.7 package and the candidate would be built from the wrong bytes.
SITE=$("$PY" -I -c 'import pathlib, turnstone; print(pathlib.Path(turnstone.__file__).resolve().parent)' 2>/dev/null) \
  || die "cannot resolve the installed turnstone package"
[[ -f "$SITE/core/session.py" ]] || die "installed package incomplete: $SITE"
case "$SITE" in
  */lib/python*/site-packages/turnstone) ;;
  *) die "resolved package is not the installed site-packages copy: $SITE" ;;
esac

version=$("$PY" -c 'import importlib.metadata as m; print(m.version("turnstone"))' 2>/dev/null || echo "?")
[[ "$version" == "1.8.7" ]] || die "installed package is $version, expected upstream 1.8.7"

# Files applied whole. Each MUST have a byte-identical base in repository HEAD.
FULL_FILES=(
  "core/output_guard_judge.py"
  "cli.py"
)

check_full_file_bases() {
  local f
  for f in "${FULL_FILES[@]}"; do
    [[ -f "$REPO/turnstone/$f" ]] || die "repository missing $f"
    git -C "$REPO" show "$BASE_COMMIT:turnstone/$f" > "$WORKDIR/base" 2>/dev/null \
      || die "cannot read $BASE_COMMIT:turnstone/$f"
    cmp -s "$WORKDIR/base" "$BASE_TREE/$f" ||
      die "base mismatch for $f: repository base $BASE_COMMIT differs from $BASE_TREE; \
refusing to overlay a divergent module"
  done
}

patch_console_picker() {
  # R2 needs exactly one Console change: skip typed-decision rows when building
  # the workstream-creation picker (GET /v1/api/models reads the DB directly and
  # never consults ModelRegistry). The repository copy of this file ALSO carries
  # the LC-RD-007 direct-OAuth posture guard, which belongs to the separate
  # package -- so this file is patched, not copied.
  local target="$1"
  "$PY" - "$target" <<'PYEOF'
import pathlib, sys
target = pathlib.Path(sys.argv[1])
text = target.read_text(encoding="utf-8")

if "openai_oauth" in text or "DIRECT_OAUTH_MODEL_AUTH_MODES" in text:
    raise SystemExit(
        "refusing: the 1.8.7 console already references the direct-OAuth feature; "
        "this revision must not ship a partial implementation of it"
    )
if "is_typed_decision" in text:
    raise SystemExit("refusing: the typed-decision picker guard is already present")

anchor1 = (
    '            "effort_ladder": [],\n'
    '        }\n'
    '        try:\n'
)
add1 = (
    '            "effort_ladder": [],\n'
    '        }\n'
    '        # A row declaring ``supports_typed_decision`` is a decision backend:\n'
    '        # it answers with a typed decision object and generates no assistant\n'
    '        # text, so it cannot back a workstream. It is excluded from this\n'
    '        # creation picker only -- it stays in the registry, in the Models\n'
    '        # admin tab, and is still executed by the Output Guard.\n'
    '        is_typed_decision = False\n'
    '        try:\n'
)
assert text.count(anchor1) == 1, "anchor1"
text = text.replace(anchor1, add1, 1)

anchor2 = (
    '                if isinstance(parsed, dict):\n'
    '                    caps = parsed\n'
)
add2 = (
    '                if isinstance(parsed, dict):\n'
    '                    caps = parsed\n'
    '                    is_typed_decision = bool(caps.get("supports_typed_decision"))\n'
)
assert text.count(anchor2) == 1, "anchor2"
text = text.replace(anchor2, add2, 1)

anchor3 = '        models.append(entry)\n'
add3 = (
    '        if is_typed_decision:\n'
    '            continue\n'
    '        models.append(entry)\n'
)
assert text.count(anchor3) == 1, "anchor3"
text = text.replace(anchor3, add3, 1)
target.write_text(text, encoding="utf-8")
print("console/server.py patched: typed rows excluded from the creation picker")
PYEOF
}

patch_model_registry() {
  # R2 is a TYPED-DECISION revision. The repository's model_registry.py also
  # carries the LC-RD-007 openai_oauth auth mode, and the R2 candidate does NOT
  # ship the token store, the dispatch branch or the settings entry that mode
  # needs. Copying the whole file would load a registry that ACCEPTS
  # auth_mode=openai_oauth while every layer underneath refuses it -- a
  # partially deployed feature, which is worse than an absent one.
  #
  # So this file is patched, not copied: exactly one method is added.
  local target="$1"
  "$PY" - "$target" <<'PYEOF'
import pathlib, sys
target = pathlib.Path(sys.argv[1])
text = target.read_text(encoding="utf-8")

if "openai_oauth" in text:
    raise SystemExit(
        "refusing: the 1.8.7 base already mentions openai_oauth; this revision "
        "must not ship a partial implementation of that auth mode"
    )
if "def list_generative_aliases" in text:
    raise SystemExit("refusing: list_generative_aliases already present in the base")

anchor = (
    '    def list_aliases(self) -> list[str]:\n'
    '        """Return all registered model aliases."""\n'
    '        return list(self._models.keys())\n'
)
addition = (
    '\n'
    '    def list_generative_aliases(self) -> list[str]:\n'
    '        """Return the aliases that can actually generate assistant text.\n'
    '\n'
    '        A row declaring ``capabilities["supports_typed_decision"]`` is a\n'
    '        decision backend: it answers with a typed decision object and\n'
    '        generates no assistant text, so offering it as a session or\n'
    '        task-agent model choice is a guaranteed failure. Those surfaces use\n'
    '        this accessor.\n'
    '\n'
    '        The row stays in the registry: ``list_aliases`` still enumerates it\n'
    '        (the Models tab and the reload endpoint manage it) and the Output\n'
    '        Guard still resolves and executes it through the typed-decision seam.\n'
    '        """\n'
    '        return [\n'
    '            alias\n'
    '            for alias, cfg in self._models.items()\n'
    '            if not (cfg.capabilities or {}).get("supports_typed_decision")\n'
    '        ]\n'
)
if anchor not in text:
    raise SystemExit("model_registry.py source drift: list_aliases anchor not found")
target.write_text(text.replace(anchor, anchor + addition, 1), encoding="utf-8")
print("model_registry.py patched: list_generative_aliases added (no auth-mode change)")
PYEOF
}

patch_session() {
  local target="$1"
  "$PY" - "$target" "$REPO/turnstone/core/session.py" <<'PYEOF'
import pathlib, sys
target = pathlib.Path(sys.argv[1])
repo_registry = pathlib.Path(sys.argv[2]).parent / "model_registry.py"
text = target.read_text(encoding="utf-8")

# The 1.8.7 source spells the same enumeration three ways; patch each with its
# own asserted count so a source drift cannot silently leave a site unfixed.
PATCHES = [
    (
        'sorted(a for a in self._registry.list_aliases() if a != "default")',
        'sorted(a for a in self._registry.list_generative_aliases() if a != "default")',
        2,
    ),
    (
        "', '.join(self._registry.list_aliases())",
        "', '.join(self._registry.list_generative_aliases())",
        2,
    ),
    (
        '", ".join(self._registry.list_aliases())',
        '", ".join(self._registry.list_generative_aliases())',
        1,
    ),
]
total = 0
for old, new, expected in PATCHES:
    count = text.count(old)
    if count != expected:
        raise SystemExit(
            f"session.py source drift: expected {expected} of {old!r}, found {count}"
        )
    text = text.replace(old, new)
    total += count
if total != 5:
    raise SystemExit(f"expected 5 patched call sites, patched {total}")
if "self._registry.list_aliases()" in text:
    raise SystemExit("a session.py alias enumeration still uses the unfiltered accessor")
if "def list_generative_aliases" not in repo_registry.read_text(encoding="utf-8"):
    raise SystemExit("repository model_registry.py does not define list_generative_aliases")
target.write_text(text, encoding="utf-8")
print(f"session.py patched: {total} call sites (2+2+1)")
PYEOF
}

patch_openai_responses() {
  local target="$1"
  "$PY" - "$target" <<'PYEOF'
import pathlib, sys
target = pathlib.Path(sys.argv[1])
text = target.read_text(encoding="utf-8")
old = '        if any(t.get("defer_loading") for t in converted):\n            converted.append({"type": "tool_search"})'
new = (
    '        # Defence in depth: ``apply_tool_search`` normally strips defer_loading\n'
    '        # on a row without the capability, but a tool dict may arrive here\n'
    '        # already carrying the flag.  A direct-OAuth lane accepts no hosted\n'
    '        # tool_search request, so the capability -- not the flag alone -- decides.\n'
    '        if caps.supports_tool_search and any(t.get("defer_loading") for t in converted):\n'
    '            converted.append({"type": "tool_search"})'
)
count = text.count(old)
if count != 1:
    raise SystemExit(f"_openai_responses.py source drift: expected 1 anchor, found {count}")
target.write_text(text.replace(old, new), encoding="utf-8")
print("_openai_responses.py patched: tool_search capability-gated")
PYEOF
}

cmd_stage() {
  [[ ! -e "$NEW" ]] || die "slot already exists: $NEW (immutable; choose a new revision)"
  check_full_file_bases
  install -d -o "$STAGE_OWNER" -g "$STAGE_GROUP" -m 0755 "$NEW"
  cp -a -- "$SITE" "$NEW/turnstone"

  local f
  for f in "${FULL_FILES[@]}"; do
    install -o "$STAGE_OWNER" -g "$STAGE_GROUP" -m 0644 "$REPO/turnstone/$f" "$NEW/turnstone/$f"
    cmp -s "$REPO/turnstone/$f" "$NEW/turnstone/$f" || die "copy mismatch: $f"
  done
  patch_model_registry "$NEW/turnstone/core/model_registry.py"
  patch_console_picker "$NEW/turnstone/console/server.py"
  patch_session "$NEW/turnstone/core/session.py"
  patch_openai_responses "$NEW/turnstone/core/providers/_openai_responses.py"

  # The rest of the typed-decision lane is carried over from the ACTIVE
  # overlay's own sources, which are the reviewed 1.8.7 implementations.
  for f in core/typed_decision.py core/tool_advisory.py; do
    install -o "$STAGE_OWNER" -g "$STAGE_GROUP" -m 0644 "$REPO/turnstone/$f" "$NEW/turnstone/$f"
    cmp -s "$REPO/turnstone/$f" "$NEW/turnstone/$f" || die "copy mismatch: $f"
  done

  find "$NEW/turnstone" -name '__pycache__' -type d -prune -exec rm -r {} + 2>/dev/null || true
  chmod -R a+rX "$NEW/turnstone"

  say "STAGED: $NEW"
  "$PY" -m compileall -q "$NEW/turnstone" >/dev/null || die "staged tree does not compile"
  say "compileall: OK"
}

cmd_verify() {
  [[ -d "$NEW/turnstone" ]] || die "no staged candidate at $NEW"
  local f
  for f in "${FULL_FILES[@]}"; do
    cmp -s "$REPO/turnstone/$f" "$NEW/turnstone/$f" || die "staged $f differs from repository"
  done
  grep -q 'list_generative_aliases' "$NEW/turnstone/core/session.py" ||
    die "staged session.py is not patched"
  if grep -q 'self._registry.list_aliases()' "$NEW/turnstone/core/session.py"; then
    die "staged session.py still contains an unfiltered enumeration"
  fi
  grep -q 'caps.supports_tool_search and any' "$NEW/turnstone/core/providers/_openai_responses.py" \
    || die "staged _openai_responses.py is missing the tool_search guard"
  grep -q 'def list_generative_aliases' "$NEW/turnstone/core/model_registry.py" \
    || die "staged model_registry.py is missing list_generative_aliases"
  grep -q 'is_typed_decision = bool' "$NEW/turnstone/console/server.py" \
    || die "staged console/server.py is missing the typed-decision picker guard"
  # R2 must not carry a half-installed openai_oauth auth mode. That feature is
  # staged separately, with its own store, dispatch branch and settings entry.
  if grep -rq 'openai_oauth' "$NEW/turnstone/core" "$NEW/turnstone/console" "$NEW/turnstone/cli.py"; then
    die "staged tree references openai_oauth without the rest of that feature; refusing a partially deployed auth mode"
  fi
  say "file integrity: OK"

  # Prove the code the tests exercise is the STAGED BYTES, not the repo tree.
  # Run as the UNPRIVILEGED service identity, never as root: the staged package
  # is exercised with the same permissions the running service would have, and a
  # test that writes can only touch the disposable dirs prepared below. The
  # overlay itself stays root-owned and read-only to that account.
  local runner=()
  if [[ "$SANDBOX" != "1" ]]; then
    id -u "$SERVICE_USER" >/dev/null 2>&1 || die "service user $SERVICE_USER does not exist"
    runner=(runuser -u "$SERVICE_USER" --)
  fi

  # Writable per-run artifacts, outside the protected overlay, owned by the
  # identity that must write them. /var/tmp is 1777, so this directory needs no
  # traversal through the root-owned workdir.
  RUNDIR=$(mktemp -d /var/tmp/turnstone-r2run.XXXXXXXX) || die "cannot create the run directory"
  chmod 0700 "$RUNDIR"
  local TMPRUN="$RUNDIR/tmp"
  install -d -m 0700 "$TMPRUN"
  if [[ "$SANDBOX" != "1" ]]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$RUNDIR"
    chown "$SERVICE_USER:$SERVICE_USER" "$TMPRUN"
  fi

  # ``-P`` keeps CWD off sys.path while still honouring PYTHONPATH (``-I``
  # would ignore PYTHONPATH entirely, and plain ``-c`` would put this repository
  # first and silently test the wrong tree).
  ( cd "$RUNDIR" && "${runner[@]}" env       PYTHONPATH="$NEW" TMPDIR="$TMPRUN" HOME="$RUNDIR"       "$PY" -P - "$NEW" <<'PYEOF'
import pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve() / "turnstone"
import turnstone, turnstone.core.model_registry as mr
import turnstone.core.session as se, turnstone.core.output_guard_judge as og
import turnstone.cli as cli, turnstone.console.server as cs
mods = [mr, se, og, cli, cs]
for m in mods:
    f = pathlib.Path(m.__file__).resolve()
    assert f.is_relative_to(root), f"{m.__name__} loaded from {f}, not the staged tree"
assert hasattr(mr.ModelRegistry, "list_generative_aliases")
# The staged session.py must be the patched 1.8.7 file, not the 1.8.4 repo copy.
src = pathlib.Path(se.__file__).read_text(encoding="utf-8")
assert "list_generative_aliases" in src
assert "self._registry.list_aliases()" not in src
print("import provenance: all modules from", root)
PYEOF
  ) || exit 3

  say "--- acceptance suites against staged bytes (as ${SERVICE_USER})"
  # The suites insert ``parents[1]`` at sys.path[0]. That is the REPOSITORY when
  # they run in place, which would shadow the candidate and test the wrong code.
  # A copy under the disposable RUNDIR makes parents[1] the staged tree while
  # leaving the overlay itself untouched and unwritable.
  install -d -m 0700 "$RUNDIR/tests"
  local t rc=0
  # Scope note: the OAuth token store is LC-RD-007, not part of this revision,
  # so its suite is deliberately absent here.
  for t in test_generative_alias_exclusion test_output_guard_typed_decision \
           test_direct_oauth_request_profile; do
    install -m 0600 "$REPO/tests/$t.py" "$RUNDIR/tests/$t.py"
    if [[ "$SANDBOX" != "1" ]]; then
      chown "$SERVICE_USER:$SERVICE_USER" "$RUNDIR/tests/$t.py"
    fi
    ( cd "$RUNDIR" && "${runner[@]}" env \
        PYTHONPATH="$NEW" TMPDIR="$TMPRUN" HOME="$RUNDIR" \
        "$PY" "$RUNDIR/tests/$t.py" ) > "$WORKDIR/$t.log" 2>&1 || rc=1
    say "  $(tail -1 "$WORKDIR/$t.log")  [$t]"
    # The suite must have exercised the staged modules, not the installed ones.
    grep -q "turnstone" "$WORKDIR/$t.log" || true
  done
  [[ $rc -eq 0 ]] || die "an acceptance suite failed against the staged bytes"
  say "VERIFY OK"
}

cmd_status() {
  [[ -e "$NEW" ]] && { say "Slot:      $NEW"; stat -c '  %A %U:%G %n' "$NEW"; } || say "Slot:      absent"
  say "Active:"
  for u in turnstone-server turnstone-node@2 turnstone-console; do
    pid=$(systemctl show -p MainPID --value "$u.service" 2>/dev/null || true)
    if [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/environ" ]]; then
      say "  $u: $(tr '\0' '\n' < "/proc/$pid/environ" | grep '^PYTHONPATH=' || echo 'PYTHONPATH=(none)')"
    else
      say "  $u: (not running)"
    fi
  done
  for d in "$SERVER_DROPIN" "$NODE_DROPIN" "$CONSOLE_DROPIN"; do
    [[ -e "$d" ]] && say "  override: $d" || say "  override absent: $d"
  done
}

cmd_activate() {
  cat >&2 <<'MSG'
REFUSED: activation is an operator-gated production boundary.

This script stages and verifies only. Activation restarts turnstone-server,
turnstone-node@2..5 and turnstone-console, which interrupts live workstreams.
The exact command set is documented in TYPED-DECISION-V187-R2-CORRECTIONS.md
and must be approved before it is run.
MSG
  exit 3
}

case "${1:-}" in
  stage)    [[ $# -eq 1 ]] || die "usage: $0 stage";    cmd_stage ;;
  verify)   [[ $# -eq 1 ]] || die "usage: $0 verify";   cmd_verify ;;
  status)   [[ $# -eq 1 ]] || die "usage: $0 status";   cmd_status ;;
  activate) cmd_activate ;;
  *) die "usage: $0 {stage|verify|status|activate}" ;;
esac
