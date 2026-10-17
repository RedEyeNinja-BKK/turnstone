#!/usr/bin/env bash
# Pin remote SSH server ED25519 identities independently recorded by operator host audit.
# No remote login, sudo, authorized_keys changes, or restart.
set -Eeuo pipefail
set +x
umask 077
ROOT=/srv/turnstone/credentials
KH="$ROOT/ssh/known_hosts"
fail(){ printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
[[ $# -eq 3 ]] || fail 'Usage: [sudo] bash script.sh preflight|pin|status agent-vm|switchyard-vm <tailscale-host-or-ip>'
ACTION="$1"; DEST="$2"; HOST="$3"
# preflight and status are READ-ONLY (they only consult the trust file and the
# peer), so they must not require root -- otherwise the failure classes this
# script now reports can only be discovered by someone who already has root.
# Only `pin` mutates the trust file.
case "$ACTION" in
  preflight|status) ;;
  pin) [[ "$EUID" == 0 ]] || fail 'pin requires root (it writes the trust file)' ;;
  *) fail 'Expected preflight|pin|status' ;;
esac
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
# ssh-keyscan prints NOTHING on an unroutable host: it just exits 1. So when the
# scan yields no key and no diagnostic, probe the port ourselves instead of
# guessing -- that silence is what made the original failure invisible.
probe_ssh_port(){
  local host="$1" out rc=0
  out="$(timeout 8 bash -c "exec 3<>/dev/tcp/$host/22" 2>&1)" || rc=$?
  # rc 124 is the timeout wrapper: an unroutable or filtered peer prints NOTHING,
  # so the exit status -- not the message -- is the only signal.
  if [[ $rc -eq 124 ]]; then echo PEER_UNREACHABLE; return; fi
  if [[ $rc -eq 0 ]]; then echo OPEN; return; fi
  case "$out" in
    *"refused"*) echo PORT_CLOSED ;;
    *"No route"*|*"unreachable"*) echo PEER_UNREACHABLE ;;
    *) echo PEER_UNREACHABLE ;;
  esac
}

check_existing(){
  [[ -f "$KH" ]] || return 1
  ssh-keygen -F "$HOST" -f "$KH" >/dev/null 2>&1
}
case "$ACTION" in
  preflight|pin)
    if [[ "$ACTION" == pin ]] && check_existing; then
      fail 'Host already pinned; refusing to change it'
    fi
    # A private work directory, not a predictable /tmp name: this runs as root.
    WORKDIR="$(mktemp -d /var/tmp/turnstone-sshpin.XXXXXXXX)"
    trap '[[ -n "${WORKDIR:-}" && -d "$WORKDIR" ]] && find "$WORKDIR" -mindepth 0 -delete 2>/dev/null' EXIT
    tmp="$WORKDIR/keyscan.out"
    scan_err="$WORKDIR/keyscan.err"

    # Resolve first, so a DNS fault reads as a DNS fault. getent is the same NSS
    # path an ssh client takes, so this is the honest precheck.
    if [[ "$HOST" == *.tailc8ef7b.ts.net ]]; then
      if ! getent ahostsv4 "$HOST" >/dev/null 2>&1; then
        # A /etc/hosts entry is not DNS; saying "resolves" here would hide the fault.
        if grep -qiE "(^|[[:space:]])${HOST//./\\.}([[:space:]]|$)" /etc/hosts 2>/dev/null; then
          fail "DNS_RESOLUTION_FAILED for $HOST: only a static /etc/hosts entry answers; MagicDNS is not integrated with this host's resolver"
        fi
        fail "DNS_RESOLUTION_FAILED for $HOST: name does not resolve via NSS (MagicDNS/accept-dns or resolver integration)"
      fi
    fi

    # Capture output AND status separately. Under `set -o pipefail`, a failing
    # ssh-keyscan in a pipeline aborted the script with no message at all --
    # which is exactly the silent exit this replaces.
    scan_rc=0
    ssh-keyscan -T 7 -t ed25519 "$HOST" > "$tmp" 2> "$scan_err" || scan_rc=$?
    awk 'NF>=3 && $2=="ssh-ed25519" {print $1,$2,$3}' "$tmp" > "$tmp.filtered"
    if [[ ! -s "$tmp.filtered" ]]; then
      reason="$(tr '[:upper:]' '[:lower:]' < "$scan_err" | head -3)"
      case "$reason" in
        *"name or service not known"*|*"could not resolve"*|*"temporary failure in name resolution"*)
          fail "DNS_RESOLUTION_FAILED for $HOST (ssh-keyscan rc=$scan_rc)" ;;
        *"connection refused"*)
          fail "PORT_CLOSED for $HOST: ssh-keyscan rc=$scan_rc (SSH not listening?)" ;;
        *"timed out"*|*"operation timed out"*|*"no route to host"*)
          fail "PEER_UNREACHABLE for $HOST: ssh-keyscan rc=$scan_rc (network/firewall/tailnet)" ;;
        *)
          case "$(probe_ssh_port "$HOST")" in
            PORT_CLOSED)
              fail "PORT_CLOSED for $HOST: TCP/22 refused (SSH not listening or filtered)" ;;
            PEER_UNREACHABLE)
              fail "PEER_UNREACHABLE for $HOST: no TCP/22 connection (offline, route or firewall)" ;;
            *)
              fail "KEYSCAN_FAILED for $HOST: ssh-keyscan rc=$scan_rc, no ED25519 key returned (auth/filtering?)" ;;
          esac ;;
      esac
    fi
    mv "$tmp.filtered" "$tmp"
    [[ "$(wc -l < "$tmp")" -eq 1 ]] || fail "Expected exactly one ED25519 host key from $HOST, got $(wc -l < "$tmp")"
    FOUND="$(ssh-keygen -lf "$tmp" -E sha256 | awk 'NR==1 {print $2}')"
    [[ -n "$FOUND" ]] || fail "FINGERPRINT_UNREADABLE for $HOST"
    [[ "$FOUND" == "$EXPECTED" ]] ||
      fail "HOSTKEY_MISMATCH for $DEST via $HOST: expected $EXPECTED got $FOUND"
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
