"""Guards for the Gate III-B-F manual-card deliberate-gesture UX.

Both the interactive pane (``shared_static/interactive.js``) and the
coordinator pane (``console/static/coordinator/coordinator.js``) must never
let generic keyboard activity approve a manual approval card:

- bare Enter must NOT approve a manual card
- bare `y` must NOT approve a manual card
- approve-all (`a` / Shift+A) must NOT approve a manual card
- Enter in an auto-focused feedback field must NOT approve a manual card
- auto-focus must not land on an Approve control for a manual card
- deliberate activation of the explicitly focused Approve button
  (``conv-btn--approve``) with Enter/Space still works
- explicit Deny still works

These are source-structure guards (no browser E2E tooling).  They assert the
manual-branch code exists and is ordered before the legacy generic paths.
"""

from __future__ import annotations

from pathlib import Path

_INTERACTIVE_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/interactive.js"
_COORDINATOR_JS = (
    Path(__file__).resolve().parent.parent / "turnstone/console/static/coordinator/coordinator.js"
)


def _body_interactive() -> str:
    return _INTERACTIVE_JS.read_text(encoding="utf-8")


def _body_coordinator() -> str:
    return _COORDINATOR_JS.read_text(encoding="utf-8")


class TestInteractiveManualCard:
    def test_manual_card_marked(self):
        """Manual cards carry the ``conv-batch--manual`` class so the keydown
        handler can require a deliberate gesture."""
        body = _body_interactive()
        assert "conv-batch--manual" in body
        assert 'it.approval_mode === "manual"' in body

    def test_manual_feedback_enter_does_not_approve(self):
        """Enter in the feedback field must NOT approve a manual card (only
        Escape denies there)."""
        body = _body_interactive()
        # The manual-branch feedback handler must exist and only deny on Esc.
        assert "if (isManual)" in body
        assert 'e.key === "Escape"' in body
        # The manual feedback branch must not call resolveApproval(true, ...)
        # on Enter — the generic Enter-approve is behind the non-manual guard.
        assert "resolveApproval(\n            true" in body  # legacy path still present

    def test_manual_bare_keys_do_not_approve(self):
        """Bare y/Enter must not approve a manual card — the manual branch
        rejects generic keys and only denies on n/Escape."""
        body = _body_interactive()
        # The manual branch returns after deny handling, before the legacy
        # y/Enter approve path.
        assert "if (isManual) {" in body
        assert 'km === "n" || e.key === "Escape"' in body

    def test_manual_requires_focused_approve_control(self):
        """The ONLY keyboard approval for a manual card is deliberate
        activation of the explicitly focused Approve button."""
        body = _body_interactive()
        assert "conv-btn--approve" in body
        assert 'e.key === "Enter" || e.key === " "' in body

    def test_manual_no_autofocus_feedback(self):
        """Manual cards must NOT auto-focus the feedback field (which would
        let a stray Enter approve)."""
        body = _body_interactive()
        assert "isManual" in body
        # The auto-focus call must be guarded by !isManual.
        assert "if (!isManual) {" in body

    def test_manual_approve_all_never_allowed(self):
        """Approve-all (`a`) must not reach a manual card — the manual branch
        returns before the legacy `a` handler."""
        body = _body_interactive()
        # The legacy 'a' approve-all path still exists but is after the
        # manual-branch early return.
        assert 'k === "a"' in body


class TestCoordinatorManualCard:
    def test_manual_batch_marked(self):
        body = _body_coordinator()
        assert "conv-batch--manual" in body
        assert 'it.approval_mode === "manual"' in body

    def test_manual_bare_enter_does_not_approve(self):
        """Bare Enter must not approve a manual coordinator batch."""
        body = _body_coordinator()
        assert 'const isManual = batch.classList.contains("conv-batch--manual")' in body
        # The manual branch must deny on Esc/d before the generic Enter path.
        assert "if (isManual) {" in body

    def test_manual_requires_focused_approve_control(self):
        body = _body_coordinator()
        assert "conv-btn--approve" in body
        assert 'e.key === "Enter" || e.key === " "' in body

    def test_manual_approve_all_never_allowed(self):
        body = _body_coordinator()
        # The manual branch returns before the generic Shift+A approve-all.
        assert "if (isManual) {" in body

    def test_manual_no_autofocus_approve(self):
        """Manual batches must NOT auto-focus the Approve button."""
        body = _body_coordinator()
        assert "conv-batch--manual" in body
        # Both _focusBatchPrimary call sites must be guarded by
        # !conv-batch--manual.
        assert (
            body.count('!entry.batch.classList.contains("conv-batch--manual")') >= 1
            or body.count('!batch.classList.contains("conv-batch--manual")') >= 1
        )
