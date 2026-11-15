/* Person avatars: one initial on the person's colour, or their uploaded picture.

   The server sends a small descriptor wherever it sends a person's name:
   {color, initial, picture}.  turnstone/core/avatars.py owns the colour index
   and the letter (so every viewer sees the same letter); base.css owns the six
   colours (--avatar-<n>-color and --avatar-<n>-tint, per theme); shell.css
   owns the circle (.avatar).  This module only draws, plus the "Your avatar"
   dialog behind the rail user menu, where each change is saved at once. */

import { authFetch } from "./auth.js";
import { openDialog, setBusy } from "./hatch.js";
import { showToast } from "./toast.js";

export const PALETTE_SIZE = 6;
const COLOUR_NAMES = ["Orange", "Green", "Teal", "Blue", "Violet", "Pink"];
const STORAGE_KEY = "ts.avatar";

function colourOf(avatar) {
  const c = avatar ? avatar.color : null;
  return Number.isInteger(c) && c >= 0 && c < PALETTE_SIZE ? c : null;
}

function initialOf(avatar) {
  return (avatar && typeof avatar.initial === "string" && avatar.initial) || "";
}

/** URL of a user's uploaded picture; the version lets the browser cache it. */
export function avatarPictureUrl(userId, version) {
  return (
    "/v1/api/users/" +
    encodeURIComponent(userId) +
    "/avatar?v=" +
    encodeURIComponent(version)
  );
}

/** Draw `avatar` ({color, initial, picture}, or null) into the .avatar `el`. */
export function paintAvatar(el, avatar, userId) {
  const colour = colourOf(avatar);
  if (colour === null) delete el.dataset.color;
  else el.dataset.color = String(colour);
  const initial = initialOf(avatar);
  el.replaceChildren();
  if (avatar && avatar.picture && userId) {
    const img = document.createElement("img");
    img.alt = "";
    img.decoding = "async";
    // Before src, or the fetch has already started: a long list only fetches
    // the pictures near the screen.
    img.loading = "lazy";
    // A picture that fails to load (removed meanwhile, offline) shows the letter.
    img.addEventListener(
      "error",
      () => {
        el.replaceChildren();
        el.textContent = initial;
      },
      { once: true },
    );
    img.src = avatarPictureUrl(userId, avatar.picture);
    el.append(img);
  } else {
    el.textContent = initial;
  }
}

/** A new .avatar element.  `size` in px; the CSS default is 24. */
export function makeAvatar(avatar, userId, size) {
  const el = document.createElement("span");
  el.className = "avatar";
  el.setAttribute("aria-hidden", "true");
  if (size) el.style.setProperty("--avatar-size", size + "px");
  paintAvatar(el, avatar, userId);
  return el;
}

/** The signed-in user's descriptor, as auth.js stored it (null before whoami). */
export function readStoredAvatar() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

function storeAvatar(avatar) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(avatar));
  } catch (e) {
    /* storage refused: the rail chip re-reads storage, so it keeps the old
       avatar until the next whoami */
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The server's byte cap for a picture (MAX_PICTURE_BYTES in core/avatars.py).
// Only a file the browser couldn't shrink can come near it.
const MAX_PICTURE_BYTES = 4 * 1024 * 1024;

// Phone photos run to 24 megapixels and many megabytes, past the server's caps
// (4 MB; 4096 by 4096 pixels for JPEG, 2048 by 2048 for PNG and WebP).  So the
// browser crops the centre square and scales it to this side before
// uploading; the server crops and scales again.
const PREPARED_SIDE = 512;

// The picture as a small square PNG, or the file as picked when the browser
// can't decode or draw it (the server then says what it accepts).  Images draw
// upright: browsers apply the EXIF orientation to decoded images.
async function preparePicture(file) {
  let url = "";
  try {
    url = URL.createObjectURL(file);
    const img = new Image();
    img.src = url;
    await img.decode();
    const width = img.naturalWidth;
    const height = img.naturalHeight;
    const side = Math.min(width, height);
    if (!side) return file;
    const out = Math.min(side, PREPARED_SIDE);
    const canvas = document.createElement("canvas");
    canvas.width = out;
    canvas.height = out;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(
      img,
      (width - side) / 2,
      (height - side) / 2,
      side,
      side,
      0,
      0,
      out,
      out,
    );
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/png"),
    );
    return blob || file;
  } catch (e) {
    return file;
  } finally {
    if (url) URL.revokeObjectURL(url);
  }
}

// The dialog's controls, built once.  Each open rebinds their handlers.
let _dialog = null;

// The colour-save state lives beside the dialog rather than in one open's
// closure: a save can outlast the open that started it, and a reopen must
// join that save, not start a second sender that could land an older pick
// after a newer one.
let _current = null; // the avatar as last saved
let _wanted = null; // the newest colour picked and not saved yet
let _sending = false; // a colour save is in flight
let _onChange = null; // the newest open's repaint callback

function buildDialog() {
  const dlg = el("dialog", "hatch hatch--dialog avatar-dialog");
  dlg.id = "avatar-dialog";
  dlg.setAttribute("data-kind", "edit");
  dlg.setAttribute("aria-labelledby", "avatar-dialog-title");

  const head = el("header", "sh-head");
  const led = el("span", "sh-led");
  led.setAttribute("aria-hidden", "true");
  const title = el("h2", "sh-title", "Your avatar");
  title.id = "avatar-dialog-title";
  const x = el("button", "sh-x", "\u2715");
  x.type = "button";
  x.setAttribute("data-close", "");
  x.setAttribute("aria-label", "Close");
  head.append(led, title, x);

  const body = el("div", "sh-body");
  const alert = el("div", "sh-alert");
  alert.setAttribute("role", "alert");

  const previewRow = el("div", "avatar-dialog-preview");
  const preview = makeAvatar(null, "", 56);
  previewRow.append(
    preview,
    el(
      "p",
      "sh-prose",
      "Others see this next to your name, and on its own where your name doesn't fit.",
    ),
  );

  const colourHead = el("div", "sh-section", "Colour");
  colourHead.id = "avatar-colour-label";
  const swatches = el("div", "avatar-swatches");
  swatches.setAttribute("role", "radiogroup");
  swatches.setAttribute("aria-labelledby", "avatar-colour-label");
  const inputs = [];
  const samples = [];
  for (let n = 0; n < PALETTE_SIZE; n++) {
    // The radio covers its swatch, so a click anywhere on the colour picks
    // it; no <label>, which the dialog styles as a form caption.
    const swatch = el("span", "avatar-swatch");
    const input = el("input");
    input.type = "radio";
    input.name = "avatar-colour";
    input.value = String(n);
    input.id = "avatar-colour-" + n;
    input.setAttribute("aria-label", COLOUR_NAMES[n]);
    const sample = makeAvatar({ color: n, initial: "" }, "", 32);
    swatch.append(input, sample);
    swatches.append(swatch);
    inputs.push(input);
    samples.push(sample);
  }
  const colourNote = el(
    "p",
    "label-hint hint-block avatar-dialog-hint",
    "With a picture, the colour shows as the ring around it.",
  );

  const pictureHead = el("div", "sh-section", "Picture");
  const pictureRow = el("div", "avatar-dialog-picture");
  const upload = el("button", "sh-btn", "Upload picture");
  upload.type = "button";
  upload.setAttribute("aria-describedby", "avatar-picture-hint");
  const remove = el("button", "sh-btn sh-btn--quiet", "Remove picture");
  remove.type = "button";
  const file = el("input");
  file.type = "file";
  file.accept = "image/png,image/jpeg,image/webp";
  file.hidden = true;
  pictureRow.append(upload, remove, file);
  const hint = el(
    "p",
    "label-hint hint-block avatar-dialog-hint",
    "PNG, JPEG or WebP. It is cropped to a square.",
  );
  hint.id = "avatar-picture-hint";

  body.append(
    alert,
    previewRow,
    colourHead,
    swatches,
    colourNote,
    pictureHead,
    pictureRow,
    hint,
  );

  const foot = el("footer", "sh-foot");
  const done = el("button", "sh-btn sh-btn--primary", "Done");
  done.type = "button";
  done.setAttribute("data-close", "");
  done.setAttribute("autofocus", "");
  foot.append(el("div", "sh-foot-meta"), done);

  dlg.append(head, body, foot);
  document.body.append(dlg);
  return {
    dlg,
    alert,
    preview,
    inputs,
    samples,
    colourNote,
    upload,
    remove,
    file,
  };
}

/**
 * Open the "Your avatar" dialog for the signed-in user.  `onChange(avatar)`
 * runs after every saved change so the caller can repaint (the rail chip).
 */
export function openAvatarDialog({ userId, onChange, opener }) {
  if (!userId) return;
  const ui = _dialog || (_dialog = buildDialog());
  const {
    dlg,
    alert,
    preview,
    inputs,
    samples,
    colourNote,
    upload,
    remove,
    file,
  } = ui;
  // A save still in flight already holds the newest avatar.
  if (!_sending) _current = readStoredAvatar();
  _onChange = onChange;

  // A failure that lands after the dialog closed goes to a toast instead.
  const showError = (message) => {
    if (message && !dlg.open) {
      showToast(message, "error");
      return;
    }
    alert.textContent = message;
    alert.classList.toggle("is-visible", Boolean(message));
  };
  const render = () => {
    // A pick still waiting to be saved shows as picked.
    const colour = _wanted !== null ? _wanted : colourOf(_current);
    paintAvatar(preview, { ..._current, color: colour }, userId);
    inputs.forEach((input, n) => {
      input.checked = n === colour;
      // Each swatch shows the person's own letter in that colour.
      paintAvatar(samples[n], { color: n, initial: initialOf(_current) }, "");
    });
    const hasPicture = Boolean(_current && _current.picture);
    // Hiding the focused Remove button would drop focus onto the page.
    if (!hasPicture && document.activeElement === remove) upload.focus();
    remove.hidden = !hasPicture;
    colourNote.hidden = !hasPicture;
    upload.textContent = hasPicture ? "Replace picture" : "Upload picture";
  };
  const busy = () => dlg.hasAttribute("data-busy");

  // Saves one change and reports whether it worked.
  const save = async (url, init) => {
    showError("");
    try {
      const r = await authFetch(url, init);
      let data = null;
      try {
        data = await r.json();
      } catch (e) {
        /* fall through to the generic message */
      }
      if (!r.ok || !data || !data.avatar) {
        showError(
          (data && data.error) || "That change wasn't saved. Try again.",
        );
        return false;
      }
      _current = data.avatar;
      storeAvatar(_current);
      if (_onChange) _onChange(_current);
      return true;
    } catch (e) {
      showError(
        "That change wasn't saved. Check your connection and try again.",
      );
      return false;
    }
  };
  const base = "/v1/api/users/" + encodeURIComponent(userId) + "/avatar";

  // Colour picks don't take the busy lock: arrowing through the swatches
  // clicks each one, and hatch cancels clicks while busy, which would leave
  // focus on one swatch and the selection on another.  Only the newest pick
  // is sent, one request at a time.
  const sendColour = async () => {
    _sending = true;
    try {
      while (_wanted !== null && _wanted !== colourOf(_current)) {
        const ok = await save(base, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ color: _wanted }),
        });
        if (!ok) _wanted = null;
      }
    } finally {
      _sending = false;
    }
    _wanted = null;
    const onSwatch = inputs.includes(document.activeElement);
    render();
    // After a failure the selection snaps back; keep focus with it.
    const checked = inputs[colourOf(_current)];
    if (onSwatch && checked) checked.focus();
  };
  inputs.forEach((input, n) => {
    input.onchange = () => {
      if (busy()) {
        render(); // a picture change is in flight
        return undefined;
      }
      _wanted = n;
      paintAvatar(preview, { ..._current, color: n }, userId);
      return _sending ? undefined : sendColour();
    };
  });

  // A picture change holds hatch's busy lock from its first step, preparing
  // the file included.  `makeInit` returns the request, or null to stop.
  const savePicture = async (makeInit, label) => {
    setBusy(dlg, true);
    if (label) upload.textContent = label;
    try {
      const init = await makeInit();
      if (init) await save(base + "/picture", init);
    } finally {
      setBusy(dlg, false);
      render();
    }
  };
  upload.onclick = () => {
    if (!busy() && !_sending) file.click();
  };
  remove.onclick = () =>
    busy() || _sending
      ? undefined
      : savePicture(async () => ({ method: "DELETE" }));
  file.onchange = () => {
    const picked = file.files && file.files[0];
    file.value = "";
    if (!picked || busy() || _sending) return undefined;
    return savePicture(async () => {
      const prepared = await preparePicture(picked);
      if (prepared.size > MAX_PICTURE_BYTES) {
        showError("That picture is over 4 MB. Choose a smaller one.");
        return null;
      }
      const form = new FormData();
      form.append(
        "file",
        prepared,
        prepared === picked ? picked.name : "avatar.png",
      );
      return { method: "PUT", body: form };
    }, "Uploading…");
  };

  showError("");
  render();
  openDialog(dlg, { opener });
}

/* Classic scripts (console admin.js) reach the painter through this bridge,
   handler-time only, like window.TurnstoneHatch. */
window.TurnstoneAvatar = { makeAvatar, paintAvatar };
