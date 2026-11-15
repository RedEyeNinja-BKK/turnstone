"""admin.js: unlinking an identity or deleting a user names where cached tokens may remain."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"


def _run(signatures: list[str], body: str) -> None:
    source = _ADMIN.read_text()
    sliced = "\n".join(extract_braced(source, signature) for signature in signatures)
    result = run_node_source("import assert from 'node:assert/strict';\n" + sliced + "\n" + body)
    assert result.returncode == 0, result.stderr


_HARNESS = """
let confirmed = null;
const showConfirmModal = (title, message, action, onConfirm) => {
  confirmed = {title, message, onConfirm};
};
let reply = {ok: true, body: {status: "ok", model_memo_unconfirmed: []}};
const authFetch = () =>
  Promise.resolve({ok: reply.ok, json: () => Promise.resolve(reply.body)});
const toasts = [];
const showToast = (text, type) => toasts.push([text, type]);
const loadAdminUsers = () => {};
const drain = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };
"""


@pytest.mark.parametrize(
    ("status", "unconfirmed", "expected"),
    [
        ("ok", [], ["User 'jin' deleted", None]),
        (
            "incomplete",
            ["console", "node-2"],
            [
                "User 'jin' deleted, but cached tokens may stay on console, node-2 until they expire",
                "warn",
            ],
        ),
    ],
    ids=["complete", "incomplete"],
)
def test_deleting_a_user_warns_when_cached_tokens_may_remain(
    status: str, unconfirmed: list[str], expected: list[object]
) -> None:
    _run(
        [
            "function _toastUnconfirmedEviction(done, data) {",
            "function confirmDeleteUser(userId, username) {",
        ],
        _HARNESS
        + f"""
reply = {{ok: true, body: {{status: "{status}", model_memo_unconfirmed: {unconfirmed!r}}}}};
confirmDeleteUser("u1", "jin");
confirmed.onConfirm();
await drain();
assert.deepEqual(toasts, [[{expected[0]!r}, {"undefined" if expected[1] is None else repr(expected[1])}]]);
""",
    )


def test_a_failed_delete_still_says_so() -> None:
    _run(
        [
            "function _toastUnconfirmedEviction(done, data) {",
            "function confirmDeleteUser(userId, username) {",
        ],
        _HARNESS
        + """
reply = {ok: false, body: {error: "nope"}};
confirmDeleteUser("u1", "jin");
confirmed.onConfirm();
await drain();
assert.deepEqual(toasts, [["Failed to delete user", undefined]]);
""",
    )


def test_the_unlink_confirmation_says_what_the_next_sign_in_does() -> None:
    source = _ADMIN.read_text()
    unlink = extract_braced(
        source, "function _confirmUnlinkOidc(issuer, subject, username, userId) {"
    )
    assert "will create a new user" in unlink
    assert "re-link" not in unlink


def test_an_unreadable_reply_warns_instead_of_reporting_success() -> None:
    """A 200 whose body cannot be read says nothing about the eviction, so the toast fails
    closed."""
    _run(
        [
            "function _toastUnconfirmedEviction(done, data) {",
            "function confirmDeleteUser(userId, username) {",
        ],
        _HARNESS.replace("const authFetch", "let authFetch")
        + """
reply = {ok: true, body: null};
authFetch = () => Promise.resolve({ok: true, json: () => Promise.reject(new Error("bad"))});
confirmDeleteUser("u1", "jin");
confirmed.onConfirm();
await drain();
assert.deepEqual(toasts, [[
  "User 'jin' deleted, but part of the result could not be confirmed; check the audit log",
  "warn",
]]);
""",
    )


@pytest.mark.parametrize(
    ("status", "unconfirmed", "expected"),
    [
        ("ok", [], ["OIDC identity unlinked", None]),
        (
            "incomplete",
            ["node-2"],
            [
                "OIDC identity unlinked, but cached tokens may stay on node-2 until they expire",
                "warn",
            ],
        ),
    ],
    ids=["complete", "incomplete"],
)
def test_unlinking_an_identity_warns_when_cached_tokens_may_remain(
    status: str, unconfirmed: list[str], expected: list[object]
) -> None:
    _run(
        [
            "function _issuerShortName(issuer) {",
            "function _toastUnconfirmedEviction(done, data) {",
            "function _confirmUnlinkOidc(issuer, subject, username, userId) {",
        ],
        _HARNESS
        + f"""
globalThis.document = {{querySelectorAll: () => []}};
const setSafeHtml = () => {{}};
reply = {{ok: true, body: {{status: "{status}", model_memo_unconfirmed: {unconfirmed!r}}}}};
_confirmUnlinkOidc("https://idp.example.com", "sub-1", "jin", "u1");
confirmed.onConfirm();
await drain();
assert.deepEqual(toasts, [[{expected[0]!r}, {"undefined" if expected[1] is None else repr(expected[1])}]]);
""",
    )
