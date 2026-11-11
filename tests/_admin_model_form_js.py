"""Node harness for the console Edit Model form (``admin.js``).

The form's create/edit round-trip is JavaScript state, and the repo has no
jsdom/vitest runner — the established pattern elsewhere in this suite is to
evaluate the real source under ``node`` against a hand-built DOM shim and
assert on what the code actually emits.  This module owns that shim once so
the round-trip tests read as scenarios, not as DOM plumbing.

The harness loads ``turnstone/console/static/admin.js`` verbatim (classic
script semantics), stubs the network with a URL router, and exposes
``showEditModelModal`` / ``submitCreateModel`` for one scenario at a time.
Each scenario runs in a fresh VM context, so module-level form state cannot
leak between cases.

A scenario is a dict:

``row``
    Model-definition row returned by ``GET /model-definitions/{id}``.
    Omit for a create-form scenario.
``alias`` / ``model``
    Required for a create-form scenario (the form refuses a blank one).
``provider`` / ``api_surface`` / ``server_type``
    Select values applied AFTER the edit-load, mimicking an operator change.
``typed_capabilities`` / ``typed_extra_body``
    Raw textarea text applied after the load, i.e. what the operator typed.

The result carries the request bodies the form actually sent, the visible
form error text, and the textarea values at submit time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ADMIN_JS = Path(__file__).resolve().parent.parent / "turnstone/console/static/admin.js"

_JOB = r"""
const fs = require("fs");
const vm = require("vm");

const adminPath = process.argv[2];
const scenarios = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// The tile matrix renders one input[data-cap] per key (console/static/index.html).
const TILE_KEYS = [
  "supports_tools", "supports_vision", "supports_pdf", "supports_web_search",
  "supports_temperature", "supports_effort", "server_parses_reasoning",
  "supports_verbosity", "supports_pro_mode", "supports_transcription",
  "supports_speech_synthesis", "supports_audio_input", "supports_rerank",
];

function makeElement(id) {
  let value = "";
  const el = {
    id,
    get value() { return value; },
    // A real input coerces assignment to a string; the form code relies on
    // that (``.value.trim()`` on numbers the API returned).
    set value(v) { value = v === null || v === undefined ? "" : String(v); },
    checked: false,
    hidden: false,
    disabled: false,
    placeholder: "",
    textContent: "",
    title: "",
    open: true,
    style: {},
    dataset: {},
    options: [],
    children: [],
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {},
    removeEventListener() {},
    removeAttribute() {},
    setAttribute() {},
    getAttribute() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    click() {},
    closest() { return null; },
    insertAdjacentHTML() {},
    remove() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
  };
  return el;
}

function makeContext(row) {
  const registry = new Map();
  const tiles = new Map();
  const requests = [];

  for (const key of TILE_KEYS) {
    const el = makeElement("tile:" + key);
    el.getAttribute = (name) => (name === "data-cap" ? key : null);
    tiles.set(key, el);
  }
  const getById = (id) => {
    if (!registry.has(id)) registry.set(id, makeElement(id));
    return registry.get(id);
  };

  const documentStub = {
    getElementById: getById,
    createElement: (tag) => makeElement("created:" + tag),
    createTextNode: (text) => ({ textContent: text }),
    querySelector(selector) {
      const m = /^#model-capgrid input\[data-cap="([^"]+)"\]$/.exec(selector || "");
      return m ? tiles.get(m[1]) || null : null;
    },
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    body: makeElement("body"),
    documentElement: makeElement("html"),
    readyState: "complete",
  };

  // URL router: the model GET yields the row; everything else (auth
  // constraints, capability lookups, reloads) yields an empty object.
  const authFetch = (url, opts) => {
    if (opts && opts.body) requests.push({ url, body: opts.body });
    let payload = {};
    if (row && /\/model-definitions\/[^/]+$/.test(url || "")) payload = row;
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(""),
    });
  };

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Promise,
    JSON,
    Math,
    Date,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Error,
    RegExp,
    encodeURIComponent,
    decodeURIComponent,
    parseInt,
    parseFloat,
    isNaN,
    URL,
    URLSearchParams,
    document: documentStub,
    authFetch,
    fetch: authFetch,
    showToast: () => {},
    loadAdminModels: () => {},
    _flagModelSyncPending: () => {},
    escapeHtml: (s) => String(s),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { clipboard: { writeText: () => Promise.resolve() }, userAgent: "node" },
    addEventListener() {},
    removeEventListener() {},
    matchMedia: (q) => ({ matches: false, media: q, addEventListener() {} }),
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.TurnstoneHatch = {
    openShelf(el) { if (el) el.open = true; },
    closeShelf(el) { if (el) el.open = false; },
    setBusy() {},
  };

  const context = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(adminPath, "utf8"), context, { filename: adminPath });
  return { context, getById, tiles, requests };
}

const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function runScenario(scenario) {
  const { context, getById, tiles, requests } = makeContext(scenario.row || null);

  if (scenario.row) {
    context.showEditModelModal(scenario.row.definition_id);
    await tick(50);
    // ``then_create`` models the operator closing an edit and opening the
    // create shelf in the same page session — the state that could leak.
    if (scenario.then_create) {
      context.showCreateModelModal();
      await tick(50);
    }
  } else {
    context.showCreateModelModal();
    await tick(50);
  }

  const setSelect = (id, value) => {
    if (value === undefined) return;
    getById(id).value = value;
    context._applyProviderDefaults();
  };
  if (scenario.alias !== undefined) getById("model-alias").value = scenario.alias;
  if (scenario.model !== undefined) getById("model-name").value = scenario.model;
  setSelect("model-provider", scenario.provider);
  setSelect("model-api-surface", scenario.api_surface);
  setSelect("model-server-type", scenario.server_type);

  if (scenario.typed_capabilities !== undefined) {
    getById("model-capabilities").value = scenario.typed_capabilities;
  }
  if (scenario.typed_extra_body !== undefined) {
    getById("model-extra-body").value = scenario.typed_extra_body;
  }

  requests.length = 0;
  context.submitCreateModel();
  await tick(30);

  return {
    requests: requests.map((r) => ({ url: r.url, body: JSON.parse(r.body) })),
    error: getById("model-create-error").textContent || "",
    form: {
      provider: getById("model-provider").value,
      api_surface: getById("model-api-surface").value,
      extra_body: getById("model-extra-body").value,
      capabilities: getById("model-capabilities").value,
      edit_id: getById("model-edit-id").value,
    },
    tiles: Object.fromEntries(TILE_KEYS.map((k) => [k, !!tiles.get(k).checked])),
  };
}

(async () => {
  const out = [];
  for (const scenario of scenarios) out.push(await runScenario(scenario));
  process.stdout.write("__RESULT__" + JSON.stringify(out) + "\n");
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + "\n");
  process.exit(1);
});
"""


def node_available() -> bool:
    return shutil.which("node") is not None


def run_scenarios(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run every scenario through the real ``admin.js`` and return results."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - guarded by the suite's skip mark
        raise RuntimeError("node is not available")
    with tempfile.TemporaryDirectory(prefix="admin-form-js-") as tmp:
        job = Path(tmp) / "job.js"
        payload = Path(tmp) / "scenarios.json"
        job.write_text(_JOB, encoding="utf-8")
        payload.write_text(json.dumps(scenarios), encoding="utf-8")
        proc = subprocess.run(
            [node, str(job), str(ADMIN_JS), str(payload)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    if proc.returncode != 0:
        raise AssertionError(f"admin.js form harness failed:\n{proc.stderr}\n{proc.stdout}")
    marker = "__RESULT__"
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(marker)),
        None,
    )
    if line is None:  # pragma: no cover - harness contract
        raise AssertionError(f"no result emitted:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(line[len(marker) :])
