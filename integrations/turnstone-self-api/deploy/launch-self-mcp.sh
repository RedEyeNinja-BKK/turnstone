#!/bin/sh
# Privileged stdio launcher for the Turnstone self-management MCP bridge.
#
# WHAT THIS IS: a convenience wrapper so the trusted administrative principal
# can start the bridge as root with the exact environment it needs. It is NOT a
# security barrier -- the Turnstone service UID is not an isolation boundary,
# and MCP approvals are dispatch-level controls, not enforced authorization.
# See LC-RD-003F/LC-RD-003G for the accepted operating model.
#
# WHY IT EXISTS: the MCP row's `env` reaches the spawned process, but sudo's
# default `env_reset` drops environment variables before exec'ing the target, so
# SELF_MCP_SERVER_ID would silently be lost and the self-unload guard would
# silently go inactive. Reading a root-owned file here is deterministic and
# survives env_reset.
#
# INSTALL (root-owned, not writable by the service user):
#   install -d -o root -g root -m 0755 /opt/turnstone-integrations/self-admin
#   install -o root -g root -m 0755 .../launch-self-mcp.sh \
#       /opt/turnstone-integrations/self-admin/launch-self-mcp.sh
#   install -o root -g root -m 0644 .../server.py \
#       /opt/turnstone-integrations/self-admin/server.py
#
# SUDOERS (exact argv only; no generic root shell, no arguments accepted):
#   turnstone ALL=(root) NOPASSWD: /opt/turnstone-integrations/self-admin/launch-self-mcp.sh ""
#
# The trailing "" is REQUIRED. A sudoers command spec with no argument list
# matches ANY arguments, so omitting it would also permit
# `launch-self-mcp.sh <anything>`. See deploy/sudoers-turnstone-self-mcp.
#
# After registering the MCP row, write its server id so the self-disable guard
# is active (root-owned, 0644, no trailing newline):
#   printf '%s' '<server_id>' > /opt/turnstone-integrations/self-admin/self_server_id
#   chown root:root /opt/turnstone-integrations/self-admin/self_server_id
#   chmod 0644 /opt/turnstone-integrations/self-admin/self_server_id

set -eu

ADMIN_DIR=${SELF_MCP_DIR:-/opt/turnstone-integrations/self-admin}
PYTHON=${SELF_MCP_PYTHON:-/opt/turnstone-venv/bin/python}
ID_FILE="$ADMIN_DIR/self_server_id"

# Re-export the server id so the bridge's env source works too; the bridge also
# reads the file directly, so either path is sufficient.
if [ -r "$ID_FILE" ]; then
  SELF_MCP_SERVER_ID="$(cat "$ID_FILE")"
  export SELF_MCP_SERVER_ID
fi

exec "$PYTHON" "$ADMIN_DIR/server.py"
