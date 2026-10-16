#!/usr/bin/env bash
# Pin remote SSH server ED25519 identities independently recorded by operator host audit.
# No remote login, sudo, authorized_keys changes, or restart.
set -Eeuo pipefail
set +x
umask 077
ROOT=/srv/turnstone/credentials
KH="$ROOT/ssh/known_hosts"
fail(){ printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" == 0 && $# == 3 ]] || fail 'Usage: sudo bash script.sh preflight|pin|status agent-vm|switchyard-vm <tailscale-host-or-ip>'
ACTION="$1"; DEST="$2"; HOST="$3"
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || fail 'Invalid host syntax'
[[ "$HOST" == *.tailc8ef7b.ts.net || "$HOST" =~ ^100\.([0-9]{1,3}\.){2}[0-9]{1,3}$ ]] ||
  fail 'Use a verified Tailscale MagicDNS name or a 100.x Tailscale IPv4 address'
case "$DEST" in
  agent-vm) EXPECTED='SHA256:MIPln7fkKZaaiCfPqOEX4+H0JBrzqlrNy+DszIKJ+Ak' ;;
  switchyard-vm) EXPECTED='SHA256:DSLHWq01pNsbSfoKOs9LEV4/una186mLNEmCrtYHslQ' ;;
  *) fail 'Unknown destination' ;;
esac
for p in /srv/turnstone "$ROOT" "$ROOT/ssh"; do
  [[ -d "$p" && ! -L "$p" && "$(stat -c '%U:%G %a' "$p")" == 'root:turnstone 750' ]] ||
    fail "Unsafe directory: $p"
done
[[ ! -L "$KH" ]] || fail 'Known-hosts symlink refused'
if [[ -e "$KH" ]]; then
  [[ -f "$KH" && "$(stat -c '%U:%G %a' "$KH")" == 'root:turnstone 640' ]] ||
    fail 'Unexpected known_hosts owner/mode'
fi
check_existing(){
  [[ -f "$KH" ]] || return 1
  ssh-keygen -F "$HOST" -f "$KH" >/dev/null 2>&1
}
case "$ACTION" in
  preflight|pin)
    if [[ "$ACTION" == pin ]] && check_existing; then
      fail 'Host already pinned; refusing to change it'
    fi
    tmp="$(mktemp)"
    trap 'rm -f -- "$tmp"' EXIT
    ssh-keyscan -T 7 -t ed25519 "$HOST" 2>/dev/null |
      awk 'NF>=3 && $2=="ssh-ed25519" {print $1,$2,$3}' > "$tmp"
    [[ "$(wc -l < "$tmp")" -eq 1 ]] || fail 'Expected exactly one ED25519 host key'
    FOUND="$(ssh-keygen -lf "$tmp" -E sha256 | awk 'NR==1 {print $2}')"
    [[ "$FOUND" == "$EXPECTED" ]] ||
      fail "Host identity mismatch for $DEST: expected $EXPECTED got $FOUND"
    echo "VERIFIED $DEST via $HOST: $FOUND"
    if [[ "$ACTION" == pin ]]; then
      if [[ ! -e "$KH" ]]; then
        install -o root -g turnstone -m 0640 /dev/null "$KH"
      fi
      if check_existing; then fail 'Host already pinned'; fi
      cat "$tmp" >> "$KH"
      ssh-keygen -F "$HOST" -f "$KH" >/dev/null || fail 'Pinned-record verification failed'
      echo 'PINNED: service-readable known_hosts; no remote authorization performed'
    else
      echo 'PREFLIGHT PASS: no trust file change'
    fi
    ;;
  status)
    if check_existing; then echo 'PINNED'; else echo 'NOT PINNED'; fi
    ;;
  *) fail 'Expected preflight|pin|status' ;;
esac
