#!/usr/bin/env bash
# ONE-TIME Turnstone credential-store rename; legacy path is an input only.
set -Eeuo pipefail
umask 077
OLD=/srv/localclaw/credentials
NEW=/srv/turnstone/credentials
OLD_PARENT=/srv/localclaw
NEW_PARENT=/srv/turnstone
fail() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" == 0 && "$#" == 1 ]] || fail "Usage: sudo bash $0 preflight|migrate|status"
check_keys() {
  local root="$1" name expected actual
  for name in turnstone-agent-vm-ed25519 turnstone-switchyard-vm-ed25519; do
    [[ -f "$root/ssh/keys/$name" && -f "$root/ssh/keys/$name.pub" ]] || fail "Key files missing: $name"
    [[ ! -L "$root/ssh/keys/$name" && ! -L "$root/ssh/keys/$name.pub" ]] || fail "Key symlink: $name"
    [[ "$(stat -c '%U:%G %a' "$root/ssh/keys/$name")" == "turnstone:turnstone 600" ]] ||
      fail "Private key owner/mode changed: $name"
    runuser -u turnstone -- ssh-keygen -y -f "$root/ssh/keys/$name" >/dev/null ||
      fail "Service user cannot read private key: $name"
    case "$name" in
      turnstone-agent-vm-ed25519) expected='SHA256:tprbJhQJiYZDEfBfu9OPdaJTEZbXnmfcXK+bYTa499w';;
      *) expected='SHA256:xzfw8ginB2oAh+f8wSEgeJi5Ei0ZR+yGMpg7U0Pyj4c';;
    esac
    actual="$(ssh-keygen -lf "$root/ssh/keys/$name.pub" -E sha256 | awk 'NR==1{print $2}')"
    [[ "$actual" == "$expected" ]] || fail "Fingerprint mismatch: $name"
  done
}
manifest_check() {
  python3 - "$1/manifest.yaml" "$1" <<'PY'
import sys
from pathlib import Path
s=Path(sys.argv[1]).read_text();root=sys.argv[2]
for name in ('turnstone-agent-vm-ed25519','turnstone-switchyard-vm-ed25519'):
    assert s.count(f'key_path: {root}/ssh/keys/{name}')==1, f'manifest path mismatch: {name}'
assert s.count('key_path:')==2, 'Unexpected extra key records'
print('Manifest paths: PASS')
PY
}
preflight() {
  for p in "$OLD_PARENT" "$OLD" "$OLD/ssh" "$OLD/ssh/keys"; do
    [[ -d "$p" && ! -L "$p" ]] || fail "Unexpected legacy path: $p"
  done
  [[ -f "$OLD/manifest.yaml" && ! -L "$OLD/manifest.yaml" ]] || fail 'Manifest missing or symlink'
  [[ ! -e "$NEW" && ! -L "$NEW" ]] || fail 'New store exists: refusing overwrite'
  [[ "$(stat -c '%U:%G %a' "$OLD_PARENT")" == 'root:turnstone 750' ]] || fail 'Legacy parent mode/owner changed'
  [[ "$(stat -c '%U:%G %a' "$OLD")" == 'root:turnstone 750' ]] || fail 'Legacy store mode/owner changed'
  [[ "$(stat -c '%U:%G %a' "$OLD/manifest.yaml")" == 'root:turnstone 640' ]] || fail 'Manifest mode/owner changed'
  if [[ -e "$NEW_PARENT" || -L "$NEW_PARENT" ]]; then
    [[ -d "$NEW_PARENT" && ! -L "$NEW_PARENT" ]] || fail 'New parent is not a real directory'
    [[ "$(stat -c '%U:%G %a' "$NEW_PARENT")" == 'root:turnstone 750' ]] ||
      fail 'Existing new parent owner/mode unexpected'
  fi
  local entry
  while IFS= read -r -d '' entry; do
    [[ "$entry" == "$OLD" ]] || fail "Unexpected item under legacy parent: $entry"
  done < <(find "$OLD_PARENT" -mindepth 1 -maxdepth 1 -print0)
  [[ "$(stat -c %d "$OLD_PARENT")" == "$(stat -c %d /srv)" ]] ||
    fail 'Mount boundary differs; manual review required'
  check_keys "$OLD"
  manifest_check "$OLD"
  echo 'PREFLIGHT PASS: fingerprints preserved; rename safe'
}
status() {
  for p in "$NEW_PARENT" "$NEW" "$NEW/ssh" "$NEW/ssh/keys"; do
    [[ -d "$p" && ! -L "$p" ]] || fail "New path missing/symlink: $p"
    stat -c '%n %U:%G %a' "$p"
  done
  [[ ! -e "$OLD" && ! -L "$OLD" ]] || fail 'Old store still exists'
  [[ "$(stat -c '%U:%G %a' "$NEW_PARENT")" == 'root:turnstone 750' ]] || fail 'New parent mode/owner changed'
  check_keys "$NEW"
  manifest_check "$NEW"
  for name in turnstone-agent-vm-ed25519 turnstone-switchyard-vm-ed25519; do
    ssh-keygen -lf "$NEW/ssh/keys/$name.pub" -E sha256 |
      awk -v n="$name" '{print n,$2,$4}'
  done
  echo 'PASS: /srv/turnstone/credentials; no new credentials minted'
}
case "$1" in
  preflight) preflight ;;
  migrate)
    preflight
    TMP="$OLD/.manifest-rename-$$"
    [[ ! -e "$TMP" ]] || fail 'Temporary manifest already exists'
    trap 'if [[ -f "$TMP" ]]; then rm -f -- "$TMP"; fi' EXIT
    python3 - "$OLD/manifest.yaml" "$TMP" "$OLD" "$NEW" <<'PY'
from pathlib import Path
import os,sys,grp
src,dst,old,new=sys.argv[1:]
s=Path(src).read_text()
for name in ('turnstone-agent-vm-ed25519','turnstone-switchyard-vm-ed25519'):
    a=f'key_path: {old}/ssh/keys/{name}'
    assert s.count(a)==1
    s=s.replace(a,f'key_path: {new}/ssh/keys/{name}')
fd=os.open(dst,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o640)
try:
    os.fchown(fd,0,grp.getgrnam('turnstone').gr_gid)
    os.write(fd,s.encode())
    os.fsync(fd)
finally:
    os.close(fd)
PY
    if [[ ! -e "$NEW_PARENT" ]]; then
      install -d -o root -g turnstone -m 0750 "$NEW_PARENT"
    fi
    mv -T -- "$OLD" "$NEW"
    TMP="$NEW/$(basename "$TMP")"
    mv -T -- "$TMP" "$NEW/manifest.yaml"
    TMP=''
    rmdir -- "$OLD_PARENT" ||
      echo 'NOTICE: old parent not empty; no recursive deletion done' >&2
    status
    ;;
  status) status ;;
  *) fail 'Expected preflight|migrate|status' ;;
esac
