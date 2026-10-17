#!/bin/sh
# Privileged runtime launcher for the bounded read-only Grafana integration.
#
# WHAT THIS IS
#   The single command the Turnstone service identity is authorized to run as
#   root for Grafana. The sudoers drop-in names each permitted verb literally
#   (".../launch-grafana-readonly.sh identity"), so there is no wildcard and no
#   free-form argument. Variable input -- the bounded query request -- arrives
#   on stdin and is validated again inside grafana_runtime.py.
#
# WHAT THIS IS NOT
#   Not a shell, not an HTTP proxy, and not a general-purpose sudo path. It
#   accepts exactly one verb from a closed set, never evaluates its arguments,
#   and execs a fixed interpreter with a fixed module. There is no daemon and
#   no socket.
#
# INSTALL
#   scripts/install_grafana_runtime.sh install   (as root)
#
# The interpreter and directory overrides exist for the installer's own
# verification step. sudo runs with env_reset, so a caller cannot set them
# through sudo; and the module is root-owned, so a caller cannot replace it.

set -eu
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

DIR=${GRAFANA_RUNTIME_DIR:-/opt/turnstone-integrations/grafana-readonly}
PYTHON=${GRAFANA_RUNTIME_PYTHON:-/opt/turnstone-venv/bin/python}

if [ "$#" -ne 1 ]; then
  echo "REFUSED: exactly one verb argument is required" >&2
  exit 2
fi

case "$1" in
  identity|permissions|health|dashboards|folders|datasources|alerts|all|query) ;;
  *)
    echo "REFUSED: unknown verb" >&2
    exit 2
    ;;
esac

if [ ! -x "$PYTHON" ]; then
  echo "REFUSED: interpreter unavailable" >&2
  exit 2
fi

if [ ! -f "$DIR/grafana_runtime.py" ]; then
  echo "REFUSED: runtime module missing" >&2
  exit 2
fi

# -I: isolated mode (ignore PYTHONPATH and user site, which are caller-derived)
# -B: never write bytecode into a root-owned directory
exec "$PYTHON" -I -B "$DIR/grafana_runtime.py" "$1"
