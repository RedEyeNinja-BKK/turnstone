#!/usr/bin/env bash
# Five-server baseline: existing head on :8080; additional node-2..5 :8082..85.
# Root-only tool. No edits to existing services, model routes or permissions.
set -Eeuo pipefail
umask 077
PY=/opt/turnstone-venv/bin/python
CLI=/opt/turnstone-venv/bin/turnstone-server
OVERLAY=/opt/turnstone-overlays/typed-decision-v187-finish
UNIT=/etc/systemd/system/turnstone-node@.service
ENVFILE=/etc/turnstone/turnstone-five-nodes.env
BACKUP=/var/backups/turnstone/five-node-v187-20261016
die() { echo "REFUSED: $*" >&2; exit 1; }
[[ "$EUID" == 0 && $# -ge 1 ]] || die "Usage: sudo bash $0 preflight|stage|start N|status|stop N"
[[ -x "$PY" && -x "$CLI" ]] || die "Installed Turnstone entry points missing"
[[ "$("$PY" -c 'import importlib.metadata as m;print(m.version("turnstone"))')" == 1.8.7 ]] ||
  die "Installed Turnstone must be 1.8.7"
pid="$(systemctl show -p MainPID --value turnstone-server.service)"
[[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/environ" ]] ||
  die "Head server is not running"
[[ "$(systemctl is-active turnstone-console.service)" == active ]] ||
  die "Console not active"
tr '\0' '\n' <"/proc/$pid/environ" |
  grep -Fxq "PYTHONPATH=$OVERLAY" || die "Head is not on the validated finishing overlay"
[[ -f "$OVERLAY/turnstone/core/typed_decision.py" ]] ||
  die "Typed-decision overlay missing"

# Verify current head bootstrap variable names without printing their values.
head_env_preflight() {
  "$PY" - "$pid" <<'PY'
import pathlib,sys
p=pathlib.Path(f"/proc/{sys.argv[1]}/environ")
env={}
for item in p.read_bytes().split(b"\0"):
    if b"=" in item:
        k,v=item.split(b"=",1); env[k.decode()]=v.decode()
expected={"TURNSTONE_ADVERTISE_URL","TURNSTONE_CONFIG","TURNSTONE_CONSOLE_URL","TURNSTONE_NODE_ID"}
found={k for k in env if k.startswith("TURNSTONE_")}
assert found == expected, (sorted(found),sorted(expected))
assert env["TURNSTONE_NODE_ID"]=="turnstone-head-1"
assert env["TURNSTONE_CONFIG"] and env["TURNSTONE_CONSOLE_URL"]
print("Head bootstrap keys and stable node identity: OK (values hidden)")
PY
}
health() {
  "$PY" - "$1" "$2" <<'PY'
import json,sys,urllib.request
port=int(sys.argv[1]); node=sys.argv[2]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",timeout=4) as res:
    d=json.load(res)
assert d.get("status")=="ok" and d.get("version")=="1.8.7"
assert d.get("node_id")==node, d.get("node_id")
assert d.get("backend",{}).get("status")=="up", d.get("backend")
PY
}
registry() {
  runuser -u postgres -- psql -d turnstone -X -A -t -v ON_ERROR_STOP=1 -c \
   "SELECT count(*) FROM services WHERE service_type='server' AND service_id='turnstone-node-$1' AND url='http://127.0.0.1:808$1';" |
    grep -Fxq 1
}
port_free() {
  "$PY" - "$1" <<'PY'
import socket,sys
p=int(sys.argv[1])
with socket.socket() as s:
    s.settimeout(0.5)
    if s.connect_ex(("127.0.0.1",p))==0:
        raise SystemExit(f"Port {p} occupied")
PY
}
mem_available() {
  awk '$1=="MemAvailable:"{print int($2/1024)}' /proc/meminfo
}
status() {
  echo "=== Services ==="
  systemctl --no-pager --plain is-active turnstone-server.service turnstone-console.service || true
  for n in 2 3 4 5; do
    printf "node-%s: " "$n"
    systemctl --no-pager --plain is-active "turnstone-node@$n.service" || true
  done
  echo "=== Shared PostgreSQL service registration ==="
  runuser -u postgres -- psql -d turnstone -X -P pager=off \
    -c "SELECT service_id,url,last_heartbeat FROM services WHERE service_type='server' ORDER BY service_id;"
  free -h
}
case "$1" in
 preflight)
   [[ $# == 1 ]] || die "preflight takes no extra args"
   head_env_preflight
   health 8080 turnstone-head-1
   [[ ! -e "$UNIT" && ! -e "$ENVFILE" ]] ||
     die "Cluster unit or env already exists; do not overwrite"
   for n in 2 3 4 5; do port_free "808$n"; done
   echo "Ports :8082..8085 free; head healthy; memory available $(mem_available) MiB"
   status
   echo "FIVE-NODE PREFLIGHT: PASS; no mutation"
   ;;
 stage)
   [[ $# == 1 ]] || die "stage takes no extra args"
   head_env_preflight
   health 8080 turnstone-head-1
   [[ ! -e "$UNIT" && ! -e "$ENVFILE" && ! -e "$BACKUP" ]] ||
     die "One staging artifact already exists; refusing overwrite"
   for n in 2 3 4 5; do port_free "808$n"; done
   install -d -o root -g root -m 0700 "$BACKUP"
   systemctl cat turnstone-server.service >"$BACKUP/head-unit.before"
   systemctl cat turnstone-console.service >"$BACKUP/console-unit.before"
   runuser -u postgres -- pg_dump -Fc -d turnstone >"$BACKUP/turnstone.before.pgcustom" ||
     die "PostgreSQL backup failed; no worker will start"
   chmod 0600 "$BACKUP/"*
   "$PY" - "$pid" "$ENVFILE" <<'PY'
import pathlib,sys
p=pathlib.Path(f"/proc/{sys.argv[1]}/environ")
env={}
for item in p.read_bytes().split(b"\0"):
    if b"=" in item:
        k,v=item.split(b"=",1);env[k.decode()]=v.decode()
keys=("TURNSTONE_CONFIG","TURNSTONE_CONSOLE_URL")
for k in keys:
    if not env.get(k) or any(c in env[k] for c in ('\n','\r','"','\\','%')):
        raise SystemExit(f"Unexpected {k} format")
path=pathlib.Path(sys.argv[2])
path.write_text("".join(k+'="'+env[k]+'"\n' for k in keys))
path.chmod(0o600)
PY
   cat >"$UNIT" <<'EOF'
[Unit]
Description=Turnstone v1.8.7 execution node %i (five-server baseline)
After=network-online.target turnstone-server.service
Wants=network-online.target

[Service]
Type=simple
User=turnstone
Group=turnstone
WorkingDirectory=/var/lib/turnstone
EnvironmentFile=/etc/turnstone/turnstone-five-nodes.env
Environment=PYTHONPATH=/opt/turnstone-overlays/typed-decision-v187-finish
Environment=TURNSTONE_NODE_ID=turnstone-node-%i
Environment=TURNSTONE_ADVERTISE_URL=http://127.0.0.1:808%i
ExecStart=/opt/turnstone-venv/bin/turnstone-server --host 127.0.0.1 --port 808%i
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
   chown root:root "$UNIT" "$ENVFILE"
   chmod 0644 "$UNIT"
   chmod 0600 "$ENVFILE"
   systemctl daemon-reload
   echo "STAGED: four-worker systemd template and root-only shared bootstrap env"
   echo "BACKUP: $BACKUP (root-only)"
   echo "No new nodes started, head/console unchanged"
   ;;
 start)
   [[ $# == 2 ]] || die "start requires node index 2..5"
   n="$2"
   case "$n" in 2|3|4|5) ;; *) die "Worker index must be 2..5";; esac
   [[ -f "$UNIT" && -f "$ENVFILE" ]] || die "stage first"
   [[ "$(stat -c %a "$ENVFILE")" == 600 ]] ||
     die "Shared bootstrap env is not root-private"
   health 8080 turnstone-head-1
   if [[ "$n" -gt 2 ]]; then
     prev=$((n-1))
     health "808$prev" "turnstone-node-$prev" ||
       die "Earlier node-$prev not healthy"
     registry "$prev" || die "Earlier node-$prev not registered"
   fi
   [[ "$(systemctl is-active "turnstone-node@$n.service" 2>/dev/null || true)" != active ]] ||
     die "Node-$n already active"
   port_free "808$n"
   mib="$(mem_available)"
   [[ "$mib" -ge 750 ]] || die "Available RAM only $mib MiB: stop rollout"
   systemctl enable --now "turnstone-node@$n.service"
   passed=0
   for _ in $(seq 1 60); do
     if health "808$n" "turnstone-node-$n" 2>/dev/null && registry "$n"; then
       passed=1; break
     fi
     sleep 2
   done
   if [[ "$passed" != 1 ]]; then
     journalctl -u "turnstone-node@$n.service" --no-pager -n 50 || true
     die "Node-$n not both healthy and registered; do not start next node"
   fi
   echo "NODE-$n: HEALTHY AND REGISTERED, active code=$OVERLAY"
   status
   ;;
 status)
   [[ $# == 1 ]] || die "status takes no extra args"
   status
   ;;
 stop)
   [[ $# == 2 ]] || die "stop requires node index 2..5"
   n="$2"
   case "$n" in 2|3|4|5) ;; *) die "Worker index must be 2..5";; esac
   echo "Only new worker node-$n will be stopped+disabled; head and console untouched."
   read -r -p "Type STOP-$n to confirm: " ans
   [[ "$ans" == "STOP-$n" ]] || die "Not approved"
   systemctl disable --now "turnstone-node@$n.service"
   status
   ;;
 *) die "Usage: sudo bash $0 preflight|stage|start N|status|stop N" ;;
esac
