"""Production-path regression for Gate III-B-FINAL-R1.

The REAL publication-order defect: ``_run_human_approval_cycle`` serialized
the ``approve_request`` card BEFORE ``ApprovalCycle.__init__`` minted the
fresh single-use ``manual_confirmation``.  The mint injects
``_manual_confirmation`` into the manual item only AFTER the card items were
serialized, so the emitted/replayed ``cycle.card`` permanently carried
``manual_confirmation=""`` — a current browser could never perform a valid
MANUAL APPROVE (the server silently 409s the missing confirmation, the card
never advances, and the cycle times out into an auto-deny).

The earlier focused tests read the confirmation from the CYCLE
(``cycle.manual_confirmation``) rather than from the WIRE CARD, so they passed
even though the delivered card was broken.  This regression drives the REAL
approval-cycle publication path (``approve_tools`` -> manual early branch ->
``_run_human_approval_cycle``) and asserts that the card actually
delivered/stored/replayed for the cycle carries the SAME non-empty
confirmation that the valid APPROVE path accepts.

It fails on the buggy ordering (canonical 196c4188) and passes once the card
is re-serialized after the mint.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

from tests.test_manual_policy import (
    MANUAL_TOOL,
    _make_manual_item,
    _new_ui,
    _patch_policies,
    _patch_storage,
    _wait_until_pending,
)


def _gate_once(ui, storage, items, verdicts):
    """Run approve_tools in a thread, return (result, thread).

    Mirrors tests/test_manual_confirmation.py so the regression drives the
    identical REAL construction/publication path.
    """
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


class TestManualConfirmationDeliveryOrder:
    def test_published_card_carries_the_accepted_confirmation(self) -> None:
        """The REAL publication path: the card the browser receives (and the
        reconnect replay re-sends) must contain the SAME non-empty
        confirmation the valid APPROVE path accepts."""
        ui = _new_ui()
        items = [_make_manual_item(call_id="c-delivery-order")]
        storage = MagicMock()

        # Drive the actual production construction/publication path.
        result, t = _gate_once(ui, storage, items, verdicts={MANUAL_TOOL: "manual"})

        # 1. A MANUAL cycle was constructed and registered.
        cycles = getattr(ui, "_approval_cycles", None)
        assert cycles, "an approval cycle must be registered"
        unresolved = [c for c in cycles.values() if not getattr(c, "resolved", False)]
        assert len(unresolved) == 1, "exactly one live cycle expected"
        cycle = unresolved[0]
        assert getattr(cycle, "has_manual", False) is True

        # 2. The server-minted confirmation is non-empty.
        minted = getattr(cycle, "manual_confirmation", "") or ""
        assert minted, "the cycle must mint a non-empty manual_confirmation"
        assert len(minted) >= 32, "confirmation must be unpredictably long"

        # 3. The ACTUAL serialized card delivered/stored for this cycle carries
        #    the SAME non-empty manual_confirmation.  This is the wire value a
        #    current browser reads and echoes back on a deliberate Approve.
        card = cycle.card or {}
        assert card.get("type") == "approve_request"
        card_items = card.get("items") or []
        assert card_items, "the card must carry serialized items"
        manual_item = next(
            (it for it in card_items if it.get("approval_mode") == "manual"),
            None,
        )
        assert manual_item is not None, "the card must expose the manual item"
        delivered = manual_item.get("manual_confirmation", "") or ""
        assert delivered, (
            "card item manual_confirmation must be non-empty on the wire "
            "(bug: serialized before mint leaves '')"
        )
        assert delivered == minted, "the card must carry the SAME confirmation the cycle minted"

        # 4. Replay/pending serialization of that exact cycle retains the value.
        replays = ui.pending_approval_cards()
        replay_manual = [
            it
            for card_ in replays
            if (card_.get("cycle_id") or "") == cycle.cycle_id
            for it in (card_.get("items") or [])
            if it.get("approval_mode") == "manual"
        ]
        assert replay_manual, "the reconnect replay must include the manual card"
        assert (replay_manual[0].get("manual_confirmation", "") or "") == minted, (
            "the replay must retain the same confirmation value"
        )

        # 5. The card-delivered value is the one the current valid APPROVE
        #    path accepts -> exactly one approval, one execution, human audit.
        with _patch_storage(storage):
            resolved = ui.resolve_approval(
                True,
                resolving_user_id="u-human",
                manual_confirmation=delivered,
            )
            t.join(timeout=5.0)
        assert resolved is not None, "valid confirmation must resolve the cycle"
        assert result.get("approved") is True
        assert result.get("err") is None
        assert items[0].get("denied") is not True
        manual_rows = [
            json.loads(c.kwargs["detail"])
            for c in storage.record_audit_event.call_args_list
            if c.kwargs.get("action") == "tool.manual_resolved"
        ]
        assert any(
            row.get("approval_mode") == "manual" and row.get("decision") == "approved"
            for row in manual_rows
        ), "the valid approval must be recorded as an approved manual resolution"
