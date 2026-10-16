#!/usr/bin/env bash
# Fresh-install ONLY. Never rerun on an already staged credential store.
# Correct Turnstone-native authority: /srv/turnstone/credentials.
set -Eeuo pipefail
umask 077
PARENT=/srv/turnstone
ROOT=/srv/turnstone/credentials
KEYDIR="$ROOT/ssh/keys"
die() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" == 0 && $# == 1 ]] || die "Usage: sudo bash $0 preflight|stage|status"
getent passwd turnstone >/dev/null || die 'turnstone service account missing'
getent group turnstone >/dev/null || die 'turnstone group missing'
[[ "$(getent passwd turnstone | cut -d: -f6)" == /var/lib/turnstone ]] || die 'Unexpected service home'
check_parent() {
  if [[ -e "$PARENT" || -L "$PARENT" ]]; then
    [[ -d "$PARENT" && ! -L "$PARENT" ]] || die 'Unexpected parent type'
    [[ "$(stat -c '%U:%G %a' "$PARENT")" == 'root:turnstone 750' ]] ||
      die 'Existing parent ownership or permissions not expected'
  fi
}
fingerprints() {
  for name in turnstone-agent-vm-ed25519 turnstone-switchyard-vm-ed25519; do
    local key="$KEYDIR/$name"
    [[ -f "$key" && -f "$key.pub" ]] || die "Missing $name"
    [[ "$(stat -c '%U:%G %a' "$key")" == "turnstone:turnstone 600" ]] ||
      die "Key permissions changed: $name"
    runuser -u turnstone -- ssh-keygen -y -f "$key" >/dev/null ||
      die "Service-user key read failed: $name"
    ssh-keygen -lf "$key.pub" -E sha256 | awk -v n="$name" '{print n,$2,$4}'
  done
}
case "$1" in
  preflight)
    check_parent
    [[ ! -e "$ROOT" && ! -L "$ROOT" ]] || die 'Store already exists; do not overwrite'
    echo 'PREFLIGHT PASS: new Turnstone-native store path available'
    ;;
  stage)
    check_parent
    [[ ! -e "$ROOT" && ! -L "$ROOT" ]] || die 'Store already exists; do not overwrite'
    if [[ ! -e "$PARENT" ]]; then
      install -d -o root -g turnstone -m 0750 "$PARENT"
    fi
    install -d -o root -g turnstone -m 0750 "$ROOT" "$ROOT/ssh" "$KEYDIR"
    for dir in bootstrap providers oauth grafana api; do
      install -d -o root -g root -m 0700 "$ROOT/$dir"
    done
    cat >"$ROOT/manifest.yaml" <<'YAML'
# Turnstone-only credential metadata; never write secret values here.
schema_version: 1
host: turnstone-vm
status: identities_minted_remote_authorization_pending
principals:
  - name: turnstone-agent-vm-ed25519
    destination: agent-vm
    remote_user: turnstone-mgmt
    client_user: turnstone
    key_path: /srv/turnstone/credentials/ssh/keys/turnstone-agent-vm-ed25519
    scope_intended: full_remote_sudo
    authorized: false
    provenance: newly_minted_on_turnstone-vm
  - name: turnstone-switchyard-vm-ed25519
    destination: switchyard-vm
    remote_user: turnstone-mgmt
    client_user: turnstone
    key_path: /srv/turnstone/credentials/ssh/keys/turnstone-switchyard-vm-ed25519
    scope_intended: full_remote_sudo
    authorized: false
    provenance: newly_minted_on_turnstone-vm
YAML
    cat >"$ROOT/README.md" <<'README'
Canonical credential authority for clean turnstone-vm.
Fresh SSH identities are NOT authorized on remote hosts until the
operator separately approves and installs their public keys.
Other categories are empty; existing model API keys remain in
Turnstone PostgreSQL until compatible secret references are deployed.
Off-VM encrypted backup and host-key verification remain pending.
README
    chown root:turnstone "$ROOT/manifest.yaml" "$ROOT/README.md"
    chmod 0640 "$ROOT/manifest.yaml" "$ROOT/README.md"
    ssh-keygen -q -t ed25519 -N '' -C turnstone-vm-to-agent-vm -f "$KEYDIR/turnstone-agent-vm-ed25519"
    ssh-keygen -q -t ed25519 -N '' -C turnstone-vm-to-switchyard-vm -f "$KEYDIR/turnstone-switchyard-vm-ed25519"
    for name in turnstone-agent-vm-ed25519 turnstone-switchyard-vm-ed25519; do
      chown turnstone:turnstone "$KEYDIR/$name" "$KEYDIR/$name.pub"
      chmod 0600 "$KEYDIR/$name"
      chmod 0644 "$KEYDIR/$name.pub"
    done
    echo 'STAGED: new identities (fresh installations only)'
    fingerprints
    ;;
  status)
    [[ -d "$ROOT" ]] || die 'Turnstone-native store missing'
    stat -c '%n %U:%G %a' "$PARENT" "$ROOT" "$ROOT/ssh" "$KEYDIR"
    fingerprints
    ;;
  *) die 'Use preflight|stage|status' ;;
esac
