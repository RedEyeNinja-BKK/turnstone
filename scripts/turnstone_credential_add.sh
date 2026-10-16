#!/usr/bin/env bash
# Turnstone C3: store credential values from a hidden terminal prompt.
# No model DB writes, runtime binding, remote auth, or service restart.
set -Eeuo pipefail
set +x
umask 077
ROOT=/srv/turnstone/credentials
fail(){ printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" -eq 0 ]] || fail 'Run with sudo'
[[ $# -ge 1 && $# -le 2 ]] || fail 'Usage: preflight|status|put <grafana-api|turnstone-api>'
for p in /srv/turnstone "$ROOT"; do
  [[ -d "$p" && ! -L "$p" ]] || fail "Missing/unsafe directory: $p"
  [[ "$(stat -c '%U:%G %a' "$p")" == 'root:turnstone 750' ]] || fail "Unexpected owner/mode: $p"
done
credential_path(){
  case "$1" in
    grafana-api) printf '%s' "$ROOT/grafana/api_token" ;;
    turnstone-api) printf '%s' "$ROOT/api/turnstone_admin_token" ;;
    *) fail 'Unknown category: allowed grafana-api, turnstone-api' ;;
  esac
}
check_subdir(){
  local dir="$1"
  [[ -d "$dir" && ! -L "$dir" ]] || fail "Missing/unsafe category directory: $dir"
  [[ "$(stat -c '%U:%G %a' "$dir")" == 'root:root 700' ]] || fail "Unexpected category owner/mode: $dir"
}
case "$1" in
  preflight)
    [[ $# == 1 ]] || fail 'preflight takes no second argument'
    check_subdir "$ROOT/grafana"
    check_subdir "$ROOT/api"
    command -v mktemp >/dev/null || fail 'mktemp unavailable'
    [[ -r /dev/tty ]] || fail 'Interactive terminal required'
    echo 'PREFLIGHT PASS: secure root-only staging available. No secrets read.'
    ;;
  status)
    [[ $# == 1 ]] || fail 'status takes no second argument'
    for n in grafana-api turnstone-api; do
      p="$(credential_path "$n")"
      if [[ -f "$p" && ! -L "$p" ]]; then
        printf '%s PRESENT %s\n' "$n" "$(stat -c '%U:%G %a' "$p")"
      elif [[ -e "$p" || -L "$p" ]]; then
        printf '%s UNSAFE\n' "$n"
      else
        printf '%s NOT_STAGED\n' "$n"
      fi
    done
    echo 'Staged files are not yet connected to the runtime.'
    ;;
  put)
    [[ $# == 2 ]] || fail 'put requires one credential category'
    p="$(credential_path "$2")"
    dir="$(dirname "$p")"
    check_subdir "$dir"
    [[ ! -e "$p" && ! -L "$p" ]] || fail 'Destination already exists; never overwrite'
    [[ -r /dev/tty && -w /dev/tty ]] || fail 'Interactive terminal required'
    printf 'Paste %s token (hidden), then Enter: ' "$2" >/dev/tty
    IFS= read -rs token </dev/tty || fail 'Unable to read token'
    printf '\nRe-enter token to confirm (hidden): ' >/dev/tty
    IFS= read -rs confirm </dev/tty || fail 'Unable to read confirmation'
    printf '\n' >/dev/tty
    [[ "$token" == "$confirm" ]] || fail 'Values differ; nothing stored'
    unset confirm
    [[ "$token" != '' ]] || fail 'Empty token; nothing stored'
    [[ "$token" =~ ^[[:graph:]]+$ ]] || fail 'Token contains whitespace/control chars; nothing stored'
    tmp="$(mktemp "$dir/.staging.XXXXXXXX")"
    trap '[[ -z "$tmp" ]] || rm -f -- "$tmp"' EXIT
    printf '%s' "$token" > "$tmp"
    unset token
    chown root:root "$tmp"
    chmod 0600 "$tmp"
    ln -- "$tmp" "$p" || fail 'Destination race/existing file; not overwritten'
    rm -f -- "$tmp"; tmp=''
    printf 'STAGED: %s (root-only, 0600); not activated in Turnstone.\n' "$2"
    ;;
  *) fail 'Expected preflight|status|put' ;;
esac
