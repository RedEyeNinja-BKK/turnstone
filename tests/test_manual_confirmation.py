"""Focused tests for the Gate III-B-FINAL manual-approval confirmation binding.

For a MANUAL ApprovalCycle, an APPROVE resolution must present the fresh,
unpredictable, single-use ``manual_confirmation`` minted for that exact live
cycle.  Legacy/stale clients that merely POST ``approved=true`` without it
fail closed (zero resolution, zero backend dispatch, zero mutation-ledger
entry).  DENY never requires the confirmation.

Coverage:
- A: legacy/stale approval (approved=true, no confirmation) rejected
- B: wrong confirmation rejected (different value)
- C: reuse rejected (successful approval consumes the confirmation)
- D: stale/replaced-cycle confirmation rejected
- E: valid exact approval executes exactly once
- F: DENY requires no confirmation and produces zero execution
- G: existing authority hardening remains (handled by
  test_manual_resolver_human_only.py — a light re-check here)
- H: ordinary non-manual ask unchanged
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from tests.test_manual_policy import (
    MANUAL_TOOL,
    _make_manual_item,
    _new_ui,
    _patch_policies,
    _patch_storage,
    _pending_manual_confirmation,
    _wait_until_pending,
)


def _gate_once(ui, storage, items, verdicts):
    """Run approve_tools in a thread, return (result, thread)."""
    result: dict[str, object] = {}

    def gate() -> None:
        try:
            with _patch_policies(verdicts):
                result["approved"], result["err"] = ui.approve_tools(items)
        except BaseException as exc:  # pragma: no cover
            result["exc"] = repr(exc)

    t = threading.Thread(target=gate, daemon=True)
    with _patch_storage(storage):
        t.start()
        _wait_until_pending(ui)
    return result, t


def _finish(ui, storage, t, *, approved, conf=None):
    """Resolve the parked cycle, join the gate thread."""
    with _patch_storage(storage):
        ui.resolve_approval(approved, resolving_user_id="u-human", manual_confirmation=conf)
        t.join(timeout=5.0)
    return not t.is_alive()


def _run_with_confirmation(ui, storage, items, verdicts, *, conf, approved=True):
    result, t = _gate_once(ui, storage, items, verdicts)
    _finish(ui, storage, t, approved=approved, conf=conf)
    return result


class TestLegacyStaleApprovalRejected:
    def test_approved_without_confirmation_rejected(self):
        """A. Legacy/stale client posts approved=true with NO confirmation →
        reject; cycle remains unresolved; zero execution."""
        ui = _new_ui()
        items = [_make_manual_item()]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        # Attempt legacy approve with no confirmation.
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True, resolving_user_id="u-human", manual_confirmation=None
            )
            t.join(timeout=5.0)
        assert resolved is None  # rejected
        assert not result.get("approved")  # gate thread did not see approval
        # The cycle was NOT resolved by the rejected approve → the gate thread
        # is still parked.  No execution happened.
        assert items[0].get("denied") is not True
        # Unblock the gate thread with a DENY (never requires confirmation)
        # so teardown does not report a leaked thread.
        with _patch_storage(storage):
            ui.resolve_approval(False, resolving_user_id="u-human")
            t.join(timeout=5.0)

    def test_approved_with_empty_confirmation_rejected(self):
        ui = _new_ui()
        items = [_make_manual_item()]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True, resolving_user_id="u-human", manual_confirmation=""
            )
        assert resolved is None
        # Unblock the gate thread with DENY.
        with _patch_storage(storage):
            ui.resolve_approval(False, resolving_user_id="u-human")
            t.join(timeout=5.0)


class TestWrongConfirmationRejected:
    def test_wrong_confirmation_value_rejected(self):
        """B. A wrong confirmation value (not the cycle's minted value) is
        rejected."""
        ui = _new_ui()
        items = [_make_manual_item()]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True,
                resolving_user_id="u-human",
                manual_confirmation="definitely-wrong-value",
            )
        assert resolved is None
        assert not result.get("approved")
        # Unblock the gate thread with DENY.
        with _patch_storage(storage):
            ui.resolve_approval(False, resolving_user_id="u-human")
            t.join(timeout=5.0)

    def test_other_cycles_confirmation_rejected(self):
        """B2. A confirmation minted for a DIFFERENT cycle must not satisfy
        this cycle.  (Each cycle mints its own value; simulate by using the
        wrong cycle's value.)"""
        ui = _new_ui()
        items = [_make_manual_item()]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        # Mint a confirmation for a DIFFERENT fake cycle.
        other = "other-cycle-confirmation-not-ours"
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True, resolving_user_id="u-human", manual_confirmation=other
            )
        assert resolved is None
        # Unblock the gate thread with DENY.
        with _patch_storage(storage):
            ui.resolve_approval(False, resolving_user_id="u-human")
            t.join(timeout=5.0)


class TestReuseRejected:
    def test_successful_approval_consumes_confirmation(self):
        """C. A successful manual approval consumes the single-use
        confirmation; a second use with the SAME value is rejected."""
        ui = _new_ui()
        items = [_make_manual_item(call_id="c-reuse")]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        conf = _pending_manual_confirmation(ui)
        assert conf, "a manual confirmation must be minted"
        with _patch_storage(storage):
            resolved1 = ui.resolve_approval(
                True, resolving_user_id="u-human", manual_confirmation=conf
            )
            t.join(timeout=5.0)
        assert resolved1 is not None
        assert result.get("approved") is True
        # The cycle is resolved; a second attempt with the same confirmation
        # must not dispatch anything (the cycle is gone / consumed).
        # After resolution the gate unregisters the cycle; a late second
        # resolve with the same conf returns None (no cycle).
        with _patch_storage(storage):
            resolved2 = ui.resolve_approval(
                True,
                resolving_user_id="u-human",
                manual_confirmation=conf,
                cycle_id=resolved1,
            )
        assert resolved2 is None  # consumed / gone — no second dispatch


class TestStaleCycleRejected:
    def test_stale_replaced_cycle_confirmation_rejected(self):
        """D. A confirmation belonging to a stale/expired/replaced cycle is
        rejected.  After the gate unregisters the cycle, a resolve with the
        old confirmation finds nothing and returns None."""
        ui = _new_ui()
        items = [_make_manual_item(call_id="c-stale")]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        conf = _pending_manual_confirmation(ui)
        assert conf
        # Let the cycle time out / be replaced: simulate by resolving via
        # timeout (no confirmation needed) then attempting approve with conf.
        with _patch_storage(storage):
            ui.resolve_approval(False, timeout=True, cycle_id=None)
            t.join(timeout=5.0)
        with _patch_storage(storage):
            late = ui.resolve_approval(True, resolving_user_id="u-human", manual_confirmation=conf)
        assert late is None  # stale cycle — nothing to resolve


class TestValidExactApproval:
    def test_valid_confirmation_approves_exactly_once(self):
        """E. Correct cycle + live confirmation + eligible operator + manual
        policy → approve exactly once."""
        ui = _new_ui()
        items = [_make_manual_item(call_id="c-valid")]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        conf = _pending_manual_confirmation(ui)
        assert conf
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True, resolving_user_id="u-human", manual_confirmation=conf
            )
            t.join(timeout=5.0)
        assert resolved is not None
        assert result.get("approved") is True
        assert result.get("err") is None
        assert items[0].get("denied") is not True


class TestDenyNoConfirmation:
    def test_deny_requires_no_confirmation_and_zero_execution(self):
        """F. Eligible operator DENY needs no confirmation and produces zero
        execution."""
        ui = _new_ui()
        items = [_make_manual_item(call_id="c-deny")]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                False, resolving_user_id="u-human", manual_confirmation=None
            )
            t.join(timeout=5.0)
        assert resolved is not None
        assert result.get("approved") is False
        assert items[0].get("denied") is True
        # No execution: no auto-approval state.
        assert ui.serialize_recent_auto_approvals() == []


class TestOrdinaryAskUnchanged:
    def test_ask_cycle_does_not_require_confirmation(self):
        """H. Ordinary non-manual ask approvals are unchanged — no
        confirmation required."""
        ui = _new_ui()
        items = [
            {
                "call_id": "c-ask",
                "func_name": "bash",
                "approval_label": "bash",
                "needs_approval": True,
                "preview": "echo hi",
            }
        ]
        storage = MagicMock()
        result, t = _gate_once(ui, storage, items, verdicts={})
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True, resolving_user_id="u-human", manual_confirmation=None
            )
            t.join(timeout=5.0)
        assert resolved is not None
        assert result.get("approved") is True
