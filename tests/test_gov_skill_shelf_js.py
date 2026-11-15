"""governance.js: the skill editor's lock affordance for a skill mirrored from an MCP server."""

from __future__ import annotations

import json
from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_GOV = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "governance.js"

_HARNESS = """
const els = {};
let focused = null;
function el(id) {
  if (!els[id]) {
    els[id] = {
      id,
      value: "",
      checked: false,
      hidden: false,
      disabled: false,
      textContent: "",
      title: "",
      open: false,
      dataset: {},
      attrs: {},
      classList: { add() {}, remove() {} },
      setAttribute(k, v) { this.attrs[k] = v; },
      removeAttribute(k) { delete this.attrs[k]; },
      appendChild() {},
      focus() { focused = id; },
    };
  }
  return els[id];
}
globalThis.document = {
  getElementById: el,
  querySelectorAll: () => [],
  createElement: () => ({ className: "", textContent: "" }),
  createTextNode: (text) => ({ text }),
};
globalThis.window = { TurnstoneHatch: { openShelf() {} } };
const _SKILL_SPEC_FIELD_IDS = ["skl-name", "skl-content"];
let _skillShelfProvenance = null;
let _govSkills = [];
function _skillShelfWire() {}
function _updateVarsDisplay() {}
function _updateSkillShelfMeta() {}
function _loadSkillResources() {}
function loadGovSkills() { return Promise.resolve(); }
function showToast() {}
function authFetch() { return Promise.resolve({ ok: true, json: () => ({}) }); }
function escapeHtml(s) { return String(s); }
function setSafeHtml() {}
function _skillShelfClosed() {}
__FUNCTIONS__
const results = {};
for (const [label, tmpl] of Object.entries(__ROWS__)) {
  for (const key of Object.keys(els)) delete els[key];
  focused = null;
  _skillShelfProvenance = null;
  _govSkills = [tmpl];
  showEditTemplateModal(tmpl.template_id);
  results[label] = {
    lockHidden: el("skl-lock").hidden,
    focused,
    provenance: _skillShelfProvenance,
  };
}
console.log(JSON.stringify(results));
"""


def _row(template_id: str, **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "template_id": template_id,
        "name": template_id,
        "category": "general",
        "description": "",
        "content": "",
        "tags": "[]",
        "variables": "[]",
        "paths": "[]",
        "arguments": "[]",
        "allowed_tools": "[]",
        "auto_approve": False,
        "enabled": True,
        "readonly": True,
        "scan_report": "",
        "risk_level": "",
    }
    row.update(fields)
    return row


def test_a_mirrored_prompt_offers_no_unlock_and_focuses_its_enabled_switch() -> None:
    source = _GOV.read_text()
    functions = "\n".join(
        extract_braced(source, signature)
        for signature in (
            "function _renderSkillShelfProvenance(tmpl, isReadonly, isUnlockedInstall) {",
            "function showEditTemplateModal(tmplId) {",
        )
    )
    rows = {
        "mirrored": _row("mcp__srv__p", origin="mcp", mcp_server="srv"),
        "installed": _row(
            "installed-skill", origin="source", source_url="https://skills.example.com/x"
        ),
    }
    proc = run_node_source(
        _HARNESS.replace("__FUNCTIONS__", functions).replace("__ROWS__", json.dumps(rows))
    )

    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert results["mirrored"]["lockHidden"] is True
    assert results["mirrored"]["focused"] == "sklc-enabled"
    assert "from MCP server srv" in results["mirrored"]["provenance"]
    assert "unlock" not in results["mirrored"]["provenance"]
    assert results["installed"]["lockHidden"] is False
    assert results["installed"]["focused"] == "skl-lock"
    assert "unlock to edit" in results["installed"]["provenance"]
