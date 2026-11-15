"""Behaviour guards for shared_static/avatar.js (the person avatar and its dialog).

Runs the module under node with the shared fake DOM; ``demodulize`` drops its
imports, so ``authFetch``, ``openDialog``, ``setBusy`` and ``showToast`` are
stubbed here.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from tests._js_harness_helpers import FAKE_DOM, demodulize, node_skip, run_node_source

pytestmark = node_skip

_MODULE = Path(__file__).resolve().parent.parent / "turnstone/shared_static/avatar.js"

_PRELUDE = r"""
Object.defineProperty(FakeElement.prototype, "style", {
  get() {
    if (!this._style) {
      this._style = { props: {}, setProperty(k, v) { this.props[k] = v; } };
    }
    return this._style;
  },
});
document.body = new FakeElement("body");
document.body._setConnected(true);
const session = new Map();
globalThis.sessionStorage = {
  getItem: (k) => (session.has(k) ? session.get(k) : null),
  setItem: (k, v) => session.set(k, String(v)),
  removeItem: (k) => session.delete(k),
};
const calls = [];
let reply = null;
globalThis.authFetch = async (url, init) => {
  calls.push({ url, init });
  return reply(url, init);
};
const toasts = [];
globalThis.showToast = (message, type) => toasts.push({ message, type });
let opened = 0;
globalThis.openDialog = (dlg) => {
  opened += 1;
  dlg.open = true;
  return { close() { dlg.open = false; } };
};
globalThis.setBusy = (dlg, on) => {
  if (on) dlg.setAttribute("data-busy", "");
  else dlg.removeAttribute("data-busy");
};
const assert = (cond, message) => { if (!cond) throw new Error(message); };
"""


def _run(body: str) -> None:
    source = FAKE_DOM + _PRELUDE + demodulize(_MODULE) + "\n" + body
    proc = run_node_source(source)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_builds_with_dom_calls_only() -> None:
    body = _MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in body and "insertAdjacentHTML" not in body
    for name in ("paintAvatar", "makeAvatar", "readStoredAvatar", "openAvatarDialog"):
        assert f"export function {name}" in body


def test_letter_on_its_colour() -> None:
    _run(
        r"""
const a = makeAvatar({ color: 3, initial: "J", picture: null }, "u1", 28);
assert(a.className === "avatar", "class");
assert(a.dataset.color === "3", "colour index");
assert(a.textContent === "J", "letter");
assert(a.children.length === 0, "no picture element");
assert(a.getAttribute("aria-hidden") === "true", "decorative: the name carries the meaning");
assert(a.style.props["--avatar-size"] === "28px", "size");
"""
    )


@pytest.mark.parametrize("bad", ["7", "-1", '"2"', "2.5", "null", "true"])
def test_out_of_range_colour_is_neutral(bad: str) -> None:
    _run(
        f"""
const a = makeAvatar({{ color: {bad}, initial: "A" }}, "u1");
assert(!("color" in a.dataset), "a bad index must not pick a colour");
assert(a.textContent === "A", "the letter still shows");
"""
    )


def test_picture_then_letter_when_it_fails_to_load() -> None:
    _run(
        r"""
const a = makeAvatar({ color: 1, initial: "M", picture: "abc123" }, "id/with space");
const img = a.children[0];
assert(img && img.tagName === "IMG", "picture element");
assert(img.src === "/v1/api/users/id%2Fwith%20space/avatar?v=abc123", "versioned url: " + img.src);
assert(img.alt === "", "decorative image");
img.dispatch("error");
assert(a.children.length === 0 && a.textContent === "M", "falls back to the letter");
const noId = makeAvatar({ color: 1, initial: "M", picture: "abc123" }, "");
assert(noId.children.length === 0 && noId.textContent === "M", "no id, no picture url");
"""
    )


def test_stored_descriptor() -> None:
    _run(
        r"""
assert(readStoredAvatar() === null, "nothing stored");
session.set("ts.avatar", '{"color":2,"initial":"A","picture":null}');
assert(readStoredAvatar().color === 2, "parsed");
session.set("ts.avatar", "{not json");
assert(readStoredAvatar() === null, "malformed is ignored");
"""
    )


def test_dialog_saves_a_colour_and_reports_it() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":0,"initial":"J","picture":null}');
const changes = [];
reply = async () => ({ ok: true, json: async () => ({ avatar: { color: 4, initial: "J", picture: null } }) });
openAvatarDialog({ userId: "u1", onChange: (a) => changes.push(a) });
assert(opened === 1, "dialog opened");
const ui = _dialog;
assert(ui.dlg.getAttribute("data-kind") === "edit", "an editing dialog, not a read-only one");
assert(ui.inputs[0].checked && !ui.inputs[4].checked, "saved colour is selected");
assert(ui.samples[2].textContent === "J" && ui.samples[2].dataset.color === "2", "swatches show the letter");
assert(ui.remove.hidden === true && ui.colourNote.hidden === true, "no picture controls");
assert(ui.upload.textContent === "Upload picture", "upload label");
await ui.inputs[4].onchange();
assert(calls.length === 1 && calls[0].url === "/v1/api/users/u1/avatar", "PUT url");
assert(calls[0].init.method === "PUT" && JSON.parse(calls[0].init.body).color === 4, "colour body");
assert(JSON.parse(session.get("ts.avatar")).color === 4, "stored for the chip");
assert(changes.length === 1 && changes[0].color === 4, "caller told");
assert(ui.preview.dataset.color === "4" && ui.inputs[4].checked, "preview repainted");
assert(!ui.dlg.hasAttribute("data-busy"), "colour picks never lock the dialog");
"""
    )


def test_arrowing_through_colours_sends_only_the_newest() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":2,"initial":"J","picture":null}');
let release;
const gate = new Promise((resolve) => { release = resolve; });
reply = async (url, init) => {
  const color = JSON.parse(init.body).color;
  if (calls.length === 1) await gate; // hold the first request in flight
  return { ok: true, json: async () => ({ avatar: { color, initial: "J", picture: null } }) };
};
const changes = [];
openAvatarDialog({ userId: "u1", onChange: (a) => changes.push(a.color) });
const ui = _dialog;
const first = ui.inputs[3].onchange();
ui.inputs[4].onchange();
ui.inputs[5].onchange();
assert(ui.preview.dataset.color === "5", "the preview follows the newest pick at once");
release();
await first;
const sent = calls.map((c) => JSON.parse(c.init.body).color).join();
assert(sent === "3,5", "only the newest pick follows the one in flight: " + sent);
assert(changes.join() === "3,5", "caller told about each saved colour");
assert(ui.inputs[5].checked && JSON.parse(session.get("ts.avatar")).color === 5, "ends on the newest");
"""
    )


def test_a_reopen_joins_the_colour_save_in_flight() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":2,"initial":"J","picture":null}');
let release;
const gate = new Promise((resolve) => { release = resolve; });
reply = async (url, init) => {
  const color = JSON.parse(init.body).color;
  if (calls.length === 1) await gate; // hold the first save in flight
  return { ok: true, json: async () => ({ avatar: { color, initial: "J", picture: null } }) };
};
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
const inFlight = ui.inputs[3].onchange();
ui.dlg.open = false; // closed with the save in flight, then opened again
const changes = [];
openAvatarDialog({ userId: "u1", onChange: (a) => changes.push(a.color) });
assert(ui.inputs[3].checked && ui.preview.dataset.color === "3", "the reopened dialog shows the pick");
assert(ui.inputs[5].onchange() === undefined, "no second sender starts");
release();
await inFlight;
const sent = calls.map((c) => JSON.parse(c.init.body).color).join();
assert(sent === "3,5", "one sender, newest pick last: " + sent);
assert(JSON.parse(session.get("ts.avatar")).color === 5, "ends on the newest pick");
assert(changes.join() === "3,5", "the newest open's caller is told");
"""
    )


def test_a_colour_failure_after_closing_shows_as_a_toast() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":0,"initial":"J","picture":null}');
let release;
const gate = new Promise((resolve) => { release = resolve; });
reply = async () => {
  await gate;
  return { ok: false, json: async () => ({ error: "Nope." }) };
};
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
const inFlight = ui.inputs[3].onchange();
ui.dlg.open = false;
release();
await inFlight;
assert(toasts.length === 1 && toasts[0].message === "Nope." && toasts[0].type === "error", "toast");
assert(!ui.alert.classList.contains("is-visible"), "nothing left in the closed dialog");
"""
    )


def test_byte_cap_matches_the_server() -> None:
    from turnstone.core.avatars import MAX_PICTURE_BYTES

    body = _MODULE.read_text(encoding="utf-8")
    match = re.search(r"const MAX_PICTURE_BYTES = ([0-9 *]+);", body)
    assert match is not None
    assert math.prod(int(part) for part in match.group(1).split("*")) == MAX_PICTURE_BYTES


def test_failed_colour_snaps_back_and_focus_follows() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":0,"initial":"J","picture":"v1"}');
reply = async () => ({ ok: false, json: async () => ({ error: "Nope." }) });
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
assert(ui.remove.hidden === false && ui.colourNote.hidden === false, "picture controls show");
assert(ui.upload.textContent === "Replace picture", "upload offers to replace");
ui.inputs[3].focus();
await ui.inputs[3].onchange();
assert(ui.alert.textContent === "Nope." && ui.alert.classList.contains("is-visible"), "error shown");
assert(ui.inputs[0].checked && !ui.inputs[3].checked, "selection snaps back");
assert(document.activeElement === ui.inputs[0], "focus stays with the selection");
assert(JSON.parse(session.get("ts.avatar")).color === 0, "nothing stored");
reply = async () => { throw new Error("offline"); };
await ui.remove.onclick();
assert(ui.alert.textContent.includes("connection"), "network failure explained");
"""
    )


def test_colour_picks_wait_out_a_picture_change() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":0,"initial":"J","picture":null}');
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
let clicked = 0;
ui.file.click = () => { clicked += 1; };
ui.dlg.setAttribute("data-busy", "");
await ui.inputs[2].onchange();
await ui.remove.onclick();
ui.upload.onclick();
assert(calls.length === 0 && clicked === 0, "nothing starts while a picture change is in flight");
assert(ui.inputs[0].checked, "the selection stays on the saved colour");
"""
    )


def test_removing_the_picture_keeps_keyboard_focus() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":1,"initial":"M","picture":"v1"}');
reply = async () => ({ ok: true, json: async () => ({ avatar: { color: 1, initial: "M", picture: null } }) });
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
ui.remove.focus();
await ui.remove.onclick();
assert(calls[0].init.method === "DELETE" && calls[0].url === "/v1/api/users/u1/avatar/picture", "DELETE");
assert(ui.remove.hidden && document.activeElement === ui.upload, "focus moves to Upload");
assert(ui.upload.textContent === "Upload picture" && ui.colourNote.hidden, "back to the letter");
"""
    )


def test_upload_checks_size_and_shows_progress() -> None:
    _run(
        r"""
session.set("ts.avatar", '{"color":1,"initial":"M","picture":null}');
let label = null;
reply = async () => {
  label = _dialog.upload.textContent;
  return { ok: true, json: async () => ({ avatar: { color: 1, initial: "M", picture: "abc" } }) };
};
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
// No canvas in this harness, so the file goes as picked and its size counts.
ui.file.files = [new Blob([new Uint8Array(4 * 1024 * 1024 + 1)], { type: "image/png" })];
await ui.file.onchange();
assert(calls.length === 0, "an oversized file is not sent");
assert(ui.alert.textContent === "That picture is over 4 MB. Choose a smaller one.", "plain size message");
ui.file.files = [new Blob(["png"], { type: "image/png" })];
await ui.file.onchange();
assert(label === "Uploading" + String.fromCodePoint(0x2026), "progress label: " + label);
assert(calls[0].init.method === "PUT" && calls[0].init.body instanceof FormData, "multipart PUT");
assert(ui.upload.textContent === "Replace picture" && !ui.remove.hidden, "picture set");
assert(ui.preview.children[0] && ui.preview.children[0].tagName === "IMG", "preview shows it");
"""
    )


def test_upload_sends_a_small_square_drawn_in_the_browser() -> None:
    """A 24-megapixel phone photo is cropped and scaled before it leaves."""
    _run(
        r"""
const drawn = [];
globalThis.Image = class {
  constructor() { this.naturalWidth = 6000; this.naturalHeight = 4000; }
  decode() { return Promise.resolve(); }
};
const fakeCreate = document.createElement;
document.createElement = (tag) => {
  if (tag !== "canvas") return fakeCreate(tag);
  return {
    width: 0,
    height: 0,
    getContext() { return { drawImage: (...args) => drawn.push(args.slice(1)) }; },
    toBlob(done, type) { done(new Blob(["small-png"], { type })); },
  };
};
session.set("ts.avatar", '{"color":1,"initial":"M","picture":null}');
reply = async () => ({ ok: true, json: async () => ({ avatar: { color: 1, initial: "M", picture: "abc" } }) });
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
ui.file.files = [new File([new Uint8Array(9 * 1024 * 1024)], "IMG_0001.JPG", { type: "image/jpeg" })];
await ui.file.onchange();
assert(drawn.length === 1, "drawn once");
assert(drawn[0].join() === "1000,0,4000,4000,0,0,512,512", "centre square scaled to 512: " + drawn[0].join());
const sent = calls[0].init.body.get("file");
assert(sent.size === 9 && sent.type === "image/png" && sent.name === "avatar.png", "the small PNG goes, not the 9 MB photo");
assert(ui.alert.textContent === "", "no size error");
"""
    )


def test_a_file_the_browser_cannot_draw_goes_as_picked() -> None:
    _run(
        r"""
globalThis.Image = class {
  decode() { return Promise.reject(new Error("cannot decode")); }
};
session.set("ts.avatar", '{"color":1,"initial":"M","picture":null}');
reply = async () => ({ ok: false, json: async () => ({ error: "Use a PNG, JPEG or WebP image." }) });
openAvatarDialog({ userId: "u1" });
const ui = _dialog;
ui.file.files = [new File(["heic-bytes"], "IMG_0002.HEIC", { type: "image/heic" })];
await ui.file.onchange();
const sent = calls[0].init.body.get("file");
assert(sent.name === "IMG_0002.HEIC" && sent.size === 10, "the original goes up");
assert(ui.alert.textContent === "Use a PNG, JPEG or WebP image.", "the server's reason shows");
"""
    )
