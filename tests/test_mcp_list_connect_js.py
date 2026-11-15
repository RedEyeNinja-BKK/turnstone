"""admin.js: the MCP Servers list offers "connect" only for an enabled per-user OAuth server.

/start answers a disabled server as unknown, so the action would open a 404.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"

_HARNESS = """
const els = {};
function stub(id) {
  return {
    id,
    value: "",
    textContent: "",
    html: "",
    classList: { add() {}, remove() {} },
    querySelectorAll: () => [],
    focus() {},
  };
}
globalThis.document = {
  getElementById: (id) => els[id] || (els[id] = stub(id)),
};
function setSafeHtml(el, html) {
  el.html = html;
}
function escapeHtml(s) {
  return String(s);
}
const menus = [];
function _kebabMenu(items) {
  menus.push(items.filter(Boolean).map((item) => item.label));
  return "";
}
function _wireMcpTokenDropButtons() {}
__FUNCTIONS__
const server = (name, extra) => ({
  server_id: "id-" + name,
  name,
  transport: "streamable-http",
  url: "https://" + name + ".example.com/mcp",
  auth_type: "oauth_user",
  enabled: true,
  status: {},
  ...extra,
});
_renderMcpServers([
  server("on"),
  server("off", { enabled: false }),
  server("plain", { auth_type: "none" }),
]);
console.log(JSON.stringify(menus));
"""


def test_connect_is_offered_only_for_an_enabled_oauth_user_server() -> None:
    source = _ADMIN.read_text()
    functions = extract_braced(source, "function _renderMcpServers(items) {")
    result = run_node_source(_HARNESS.replace("__FUNCTIONS__", functions))

    assert result.returncode == 0, result.stderr
    on, off, plain = json.loads(result.stdout)
    assert "connect" in on
    assert "connect" not in off
    assert "connect" not in plain
