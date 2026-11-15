"""admin.js: the Users table's avatars and its "remove picture" action."""

from __future__ import annotations

from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"


def _run(signature: str, body: str) -> None:
    sliced = extract_braced(_ADMIN.read_text(), signature)
    result = run_node_source("import assert from 'node:assert/strict';\n" + sliced + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_remove_picture_is_offered_only_for_a_user_with_one() -> None:
    _run(
        "function _userMenuItems(u) {",
        """
// _kebabMenu drops the falsy entry.
const labels = (u) => _userMenuItems(u).filter(Boolean).map((item) => item.label);
const jin = {user_id: "u1", username: "jin"};
assert.deepEqual(labels(jin), ["roles", "delete"]);
assert.deepEqual(labels({...jin, avatar: {color: 1, initial: "J", picture: null}}), [
  "roles",
  "delete",
]);
const items = _userMenuItems({
  ...jin,
  avatar: {color: 1, initial: "J", picture: "0123456789abcdef"},
});
assert.deepEqual(items.map((item) => item.label), ["roles", "remove picture", "delete"]);
assert.deepEqual(items[1].attrs, {"data-remove-picture": "u1", "data-username": "jin"});
""",
    )


def test_removing_a_picture_asks_first_then_deletes_it() -> None:
    _run(
        "function confirmRemoveUserPicture(userId, username) {",
        """
let confirmed = null;
const showConfirmModal = (title, message, action, onConfirm) => {
  confirmed = {title, message, onConfirm};
};
const calls = [];
let ok = true;
const authFetch = (url, init) => {
  calls.push([url, init]);
  return Promise.resolve({ok});
};
const toasts = [];
const showToast = (text) => toasts.push(text);
let reloads = 0;
const loadAdminUsers = () => { reloads++; };
const drain = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };

confirmRemoveUserPicture("a/b c", "jin");
assert.equal(calls.length, 0, "nothing is removed before the confirm");
assert.equal(confirmed.title, "Remove picture");
assert.match(confirmed.message, /jin's avatar picture/);
confirmed.onConfirm();
await drain();
assert.deepEqual(calls, [["/v1/api/admin/users/a%2Fb%20c/avatar/picture", {method: "DELETE"}]]);
assert.deepEqual(toasts, ["Removed jin's picture"]);
assert.equal(reloads, 1);

ok = false;
confirmRemoveUserPicture("u2", "ana");
confirmed.onConfirm();
await drain();
assert.equal(toasts.at(-1), "Failed to remove the picture");
assert.equal(reloads, 1, "a refused removal leaves the table as it is");
""",
    )


def test_each_user_row_gets_its_avatar_after_the_expand_indicator() -> None:
    _run(
        "function _paintUserAvatars(container, users) {",
        """
const made = [];
globalThis.window = {
  TurnstoneAvatar: {
    makeAvatar: (avatar, userId) => {
      const el = {avatar, userId};
      made.push(el);
      return el;
    },
  },
};
function row(userId) {
  const indicator = {nextSibling: "username text"};
  const col = {
    firstChild: indicator,
    inserted: [],
    querySelector: (sel) => (sel === ".admin-expand-indicator" ? indicator : null),
    insertBefore(el, before) { this.inserted.push([el, before]); },
  };
  return {
    col,
    getAttribute: (name) => (name === "data-user-id" ? userId : null),
    querySelector: (sel) => (sel === ".admin-col-username" ? col : null),
  };
}
const rows = [row("u1"), row("u2"), row("not-listed")];
const container = {
  querySelectorAll: (sel) => (sel === ".admin-row[data-user-id]" ? rows : []),
};
const avatar = {color: 2, initial: "J", picture: null};
_paintUserAvatars(container, [{user_id: "u1", avatar}, {user_id: "u2"}]);
assert.deepEqual(made, [{avatar, userId: "u1"}, {avatar: null, userId: "u2"}]);
assert.deepEqual(rows[0].col.inserted, [[made[0], "username text"]]);
assert.deepEqual(rows[1].col.inserted, [[made[1], "username text"]]);
assert.deepEqual(rows[2].col.inserted, [], "a row whose user isn't listed is left alone");

// Without the avatar module the table still renders, with no avatars.
globalThis.window = {};
_paintUserAvatars(container, [{user_id: "u1", avatar}]);
assert.equal(made.length, 2);
""",
    )
