"""Gate-level tests for the ``manual`` tool-policy action.

The ``manual`` action routes a winning invocation directly to a fresh
human ApprovalCycle and makes the automatic approval pipeline (skill
allow, ``auto_approve_tools`` / stale "Approve + Always", blanket
``auto_approve``, ``skip_permissions``, Smart Approvals) structurally
unreachable for it.

These tests exercise the shared :meth:`SessionUIBase.approve_tools` body
through :class:`ConsoleCoordinatorUI`, patching policy evaluation and
storage exactly like the existing ``test_coord_ui_approve_tools`` suite.

Audit rows are emitted by ``resolve_approval`` (the resolver thread).
The storage patch is therefore held by the *main* thread around both the
gate execution and the resolution so the audit write observes the mock
deterministically, regardless of gate-thread teardown timing.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.session_ui_base import manual_arg_digest

if TYPE_CHECKING:
    from collections.abc import Callable

MANUAL_TOOL = "mcp__protected__openclaw_agent_delete"
READ_TOOL = "mcp__readonly__openclaw_agents_list"
WRITE_TOOL = "mcp__other__openclaw_agent_run"


def _make_item(
    call_id: str,
    func: str,
    *,
    needs_approval: bool = True,
    mcp_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "call_id": call_id,
        "header": f"Tool: {func}",
        "preview": "preview text",
        "func_name": func,
        "approval_label": func,
        "needs_approval": needs_approval,
    }
    if mcp_args is not None:
        item["mcp_args"] = mcp_args
    return item


def _make_manual_item(call_id: str = "c1", func: str = MANUAL_TOOL) -> dict[str, Any]:
    return _make_item(call_id, func, mcp_args={"agent_id": "main"})


def _patch_storage(storage: Any):
    return patch("turnstone.core.storage._registry.get_storage", return_value=storage)


def _patch_policies(verdicts: dict[str, str]):
    return patch(
        "turnstone.core.policy.evaluate_tool_policies_batch",
        return_value=verdicts,
    )


def _new_ui() -> ConsoleCoordinatorUI:
    return ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")


def _manual_audit_rows(storage: Any) -> list[dict[str, Any]]:
    """Parse the ``tool.manual_resolved`` audit rows emitted to storage."""
    return [
        json.loads(c.kwargs["detail"])
        for c in storage.record_audit_event.call_args_list
        if c.kwargs.get("action") == "tool.manual_resolved"
    ]


def _wait_until_pending(ui: ConsoleCoordinatorUI, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if getattr(ui, "_pending_approval", None) is not None:
            return
        time.sleep(0.001)
    raise AssertionError("approval cycle was never registered")


def _run_gate_with_resolution(
    ui: ConsoleCoordinatorUI,
    storage: Any,
    items: list[dict[str, Any]],
    *,
    verdicts: dict[str, str],
    approved: bool = True,
    always: bool = False,
    timeout: bool = False,
    resolving_user_id: str | None = None,
    before_resolve: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run ``approve_tools`` in a background thread and resolve the parked
    ApprovalCycle from the main thread while the storage patch is held."""
    result: dict[str, Any] = {}

    def gate() -> None:
        try:
            with _patch_policies(verdicts):
                result["approved"], result["err"] = ui.approve_tools(items)
        except BaseException as exc:  # pragma: no cover - failure reporter
            result["exc"] = repr(exc)

    with _patch_storage(storage):
        t = threading.Thread(target=gate, daemon=True)
        t.start()
        try:
            _wait_until_pending(ui)
            if before_resolve is not None:
                before_resolve()
            ui.resolve_approval(
                approved,
                always=always,
                timeout=timeout,
                resolving_user_id=resolving_user_id,
            )
        finally:
            t.join(timeout=5.0)
    result["gate_alive"] = t.is_alive()
    return result


# ---------------------------------------------------------------------------
# Manual reaches a fresh human ApprovalCycle
# ---------------------------------------------------------------------------


def test_manual_single_reaches_approval_cycle_and_approves() -> None:
    """A single winning ``manual`` invocation parks on a fresh human
    cycle; the human approval executes it and emits ``tool.manual_resolved``."""
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result = _run_gate_with_resolution(
        ui,
        storage,
        items,
        verdicts={MANUAL_TOOL: "manual"},
        approved=True,
        resolving_user_id="u-human",
    )

    assert result["approved"] is True
    assert result["err"] is None
    assert not result["gate_alive"]
    assert items[0].get("denied") is not True
    # No automatic approval state was created for the manual call.
    assert ui.serialize_recent_auto_approvals() == []
    # Durable audit: tool.manual_resolved with the human actor and the full
    # canonical field set (parsed, not substring-matched).
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    row = rows[0]
    assert row["approval_mode"] == "manual"
    assert row["decision"] == "approved"
    assert row["source"] == "human"  # direct human approve actor
    assert row["tool_name"] == MANUAL_TOOL  # canonical namespaced identity
    assert row["call_id"] == "c1"
    assert row["cycle_id"]
    assert row["user_id"] == "u-human"
    assert row["always_suppressed"] is False
    assert row["arg_digest"] == manual_arg_digest(items[0])  # exact SHA-256
    assert row["timestamp"]
    # user_decision stays an outcome, never the mode.
    for c in storage.update_intent_verdict.call_args_list:
        assert c.kwargs.get("user_decision") != "manual"


def test_manual_deny_executes_nothing() -> None:
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result = _run_gate_with_resolution(
        ui,
        storage,
        items,
        verdicts={MANUAL_TOOL: "manual"},
        approved=False,
        resolving_user_id="u-human",
    )

    assert result["approved"] is False
    assert items[0].get("denied") is True
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    assert rows[0]["decision"] == "denied"
    assert rows[0]["user_id"] == "u-human"  # direct human deny actor
    assert rows[0]["source"] == "human"
    assert rows[0]["tool_name"] == MANUAL_TOOL
    assert rows[0]["call_id"] == "c1"
    assert rows[0]["cycle_id"]


def test_manual_timeout_denies_without_human_actor() -> None:
    """A manual cycle that times out fails closed with no human actor."""
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result = _run_gate_with_resolution(
        ui,
        storage,
        items,
        verdicts={MANUAL_TOOL: "manual"},
        approved=False,
        timeout=True,
    )

    assert result["approved"] is False
    assert items[0].get("denied") is True
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    assert rows[0]["decision"] == "timeout"
    assert rows[0]["user_id"] == ""  # no human actor
    assert rows[0]["source"] == "system"  # automatic timeout, no invented actor


# ---------------------------------------------------------------------------
# Automatic-gate exclusion — the auto pipeline must not even be entered
# ---------------------------------------------------------------------------


def test_manual_beats_smart_approval() -> None:
    """Smart Approvals must not be invoked for a manual batch."""
    ui = _new_ui()
    ui.smart_approvals_enabled = True
    ui.smart_approval_threshold = 0.0
    items = [_make_manual_item()]
    storage = MagicMock()
    with patch.object(ui, "_apply_smart_approvals", wraps=ui._apply_smart_approvals) as spy:
        result = _run_gate_with_resolution(
            ui,
            storage,
            items,
            verdicts={MANUAL_TOOL: "manual"},
            approved=True,
        )

    assert result["approved"] is True
    spy.assert_not_called()


def test_manual_beats_blanket_auto_approve() -> None:
    """Blanket ``auto_approve`` (and ``skip_permissions`` which sets the
    same flag) must not drain a manual invocation."""
    ui = _new_ui()
    ui.auto_approve = True  # blanket_active equivalent
    items = [_make_manual_item()]
    storage = MagicMock()
    with patch.object(ui, "_tag_auto_approved") as tag_spy:
        result = _run_gate_with_resolution(
            ui,
            storage,
            items,
            verdicts={MANUAL_TOOL: "manual"},
            approved=True,
        )

    assert result["approved"] is True
    tag_spy.assert_not_called()
    assert ui.serialize_recent_auto_approvals() == []


def test_manual_beats_matching_auto_approve_tools() -> None:
    """A matching ``auto_approve_tools`` entry (skill allow / prior
    "Approve + Always") must not auto-approve a manual call."""
    ui = _new_ui()
    ui.auto_approve_tools = {MANUAL_TOOL}  # stale Always / skill allow
    items = [_make_manual_item()]
    storage = MagicMock()
    with patch.object(ui, "_tag_auto_approved") as tag_spy:
        result = _run_gate_with_resolution(
            ui,
            storage,
            items,
            verdicts={MANUAL_TOOL: "manual"},
            approved=True,
        )

    assert result["approved"] is True  # only after a human resolved it
    tag_spy.assert_not_called()


def test_manual_beats_skill_allowed_tools_populated_path() -> None:
    """A skill-derived ``allowed_tools`` entry (populated exactly as
    server.py wires skill session config: set + SKILL source map) must
    not auto-approve a manual call — the manual early branch returns
    before the auto_approve_tools subset check runs."""
    from turnstone.core.session_ui_base import AutoApproveReason

    ui = _new_ui()
    # Mirror server.py's skill wiring: allowed_tools -> set + source map.
    ui.auto_approve_tools = {MANUAL_TOOL}
    ui._auto_approve_tools_source = {MANUAL_TOOL: AutoApproveReason.SKILL}
    items = [_make_manual_item()]
    storage = MagicMock()
    with patch.object(ui, "_tag_auto_approved") as tag_spy:
        result = _run_gate_with_resolution(
            ui,
            storage,
            items,
            verdicts={MANUAL_TOOL: "manual"},
            approved=True,
        )

    assert result["approved"] is True  # only after a human resolved it
    tag_spy.assert_not_called()
    assert ui.serialize_recent_auto_approvals() == []


# ---------------------------------------------------------------------------
# Batch isolation — one manual GO authorizes exactly one protected call
# ---------------------------------------------------------------------------


def _assert_batch_rejected(ui, items, storage, verdicts) -> None:
    with _patch_storage(storage), _patch_policies(verdicts):
        approved, err = ui.approve_tools(items)

    assert approved is False
    assert err is not None and "exactly one executable call" in err
    # Every executable sibling is denied — zero execution.
    for it in items:
        if not it.get("error"):
            assert it.get("denied") is True
    # No durable auto-approval evidence may exist for the rejected batch.
    assert ui.serialize_recent_auto_approvals() == []
    storage.create_intent_verdicts_bulk.assert_not_called()


def _batch_verdicts(*manual_labels: str) -> dict[str, str]:
    return {label: "manual" for label in manual_labels}


def test_batch_manual_plus_manual_rejected() -> None:
    ui = _new_ui()
    items = [_make_manual_item("c1"), _make_manual_item("c2")]
    _assert_batch_rejected(ui, items, MagicMock(), _batch_verdicts(MANUAL_TOOL, MANUAL_TOOL))


def test_batch_manual_local_plus_remote_rejected() -> None:
    ui = _new_ui()
    local = "mcp__openclaw-gateway__openclaw_agent_create"
    remote = "mcp__openclaw-remote-gateway__openclaw_agent_create"
    items = [_make_manual_item("c1", local), _make_manual_item("c2", remote)]
    _assert_batch_rejected(ui, items, MagicMock(), _batch_verdicts(local, remote))


def test_batch_manual_plus_mcp_read_rejected() -> None:
    ui = _new_ui()
    items = [_make_manual_item("c1"), _make_item("c2", READ_TOOL)]
    _assert_batch_rejected(ui, items, MagicMock(), _batch_verdicts(MANUAL_TOOL))


def test_batch_manual_plus_mcp_write_rejected() -> None:
    ui = _new_ui()
    items = [_make_manual_item("c1"), _make_item("c2", WRITE_TOOL)]
    _assert_batch_rejected(ui, items, MagicMock(), _batch_verdicts(MANUAL_TOOL))


def test_batch_manual_plus_native_write_rejected() -> None:
    ui = _new_ui()
    items = [_make_manual_item("c1"), _make_item("c2", "bash")]
    _assert_batch_rejected(ui, items, MagicMock(), _batch_verdicts(MANUAL_TOOL))


def test_batch_manual_plus_native_read_rejected() -> None:
    ui = _new_ui()
    items = [
        _make_manual_item("c1"),
        _make_item("c2", "read_file", needs_approval=False),
    ]
    _assert_batch_rejected(ui, items, MagicMock(), _batch_verdicts(MANUAL_TOOL))


def test_batch_manual_plus_policy_allow_sibling_rejected_no_evidence() -> None:
    """A policy-``allow`` sibling in a manual batch is denied with the
    batch and must not leave durable auto-approved evidence — and the
    serialized tool_info event must NOT carry auto_approved=true for the
    rejected sibling (staging fix)."""
    ui = _new_ui()
    items = [_make_manual_item("c1"), _make_item("c2", WRITE_TOOL)]
    storage = MagicMock()
    verdicts = {MANUAL_TOOL: "manual", WRITE_TOOL: "allow"}
    captured: list[dict[str, Any]] = []
    ui._enqueue = captured.append  # type: ignore[method-assign]
    with _patch_storage(storage), _patch_policies(verdicts):
        approved, err = ui.approve_tools(items)

    assert approved is False
    assert err is not None and "exactly one executable call" in err
    assert items[0].get("denied") is True
    assert items[1].get("denied") is True
    # The allow sibling was never committed as auto-approved.
    assert ui.serialize_recent_auto_approvals() == []
    storage.create_intent_verdicts_bulk.assert_not_called()
    # Serialized event assertion: the rejected allow sibling is denied and
    # carries NO auto_approved=true (a call that never executed must not be
    # serialized as approved).
    infos = [e for e in captured if e.get("type") == "tool_info"]
    assert infos, "expected a tool_info event for the rejected batch"
    serialized = infos[0]["items"]
    by_label = {it["approval_label"]: it for it in serialized}
    assert WRITE_TOOL in by_label
    allow_serialized = by_label[WRITE_TOOL]
    assert allow_serialized.get("denied") is True or allow_serialized.get("error")
    assert allow_serialized.get("auto_approved") is not True
    assert "auto_approve_reason" not in allow_serialized
    # The manual sibling is also denied with no auto-approved marker.
    manual_serialized = by_label[MANUAL_TOOL]
    assert manual_serialized.get("auto_approved") is not True


def test_batch_manual_with_policy_deny_sibling_is_valid() -> None:
    """A hard-denied sibling is not executable, so a manual batch with a
    deny sibling is valid: the manual call reaches the human gate and the
    denied sibling stays blocked."""
    ui = _new_ui()
    items = [_make_manual_item("c1"), _make_item("c2", "delete_workstream")]
    storage = MagicMock()
    verdicts = {MANUAL_TOOL: "manual", "delete_workstream": "deny"}
    result = _run_gate_with_resolution(
        ui,
        storage,
        items,
        verdicts=verdicts,
        approved=True,
    )

    assert result["approved"] is True
    assert items[0].get("denied") is not True
    assert items[1].get("denied") is True


# ---------------------------------------------------------------------------
# Approve + Always suppression
# ---------------------------------------------------------------------------


def test_manual_always_true_is_suppressed_and_not_persisted() -> None:
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    captured: list[dict[str, Any]] = []
    ui._enqueue = captured.append  # type: ignore[method-assign]
    result = _run_gate_with_resolution(
        ui,
        storage,
        items,
        verdicts={MANUAL_TOOL: "manual"},
        approved=True,
        always=True,
        resolving_user_id="u-human",
    )

    assert result["approved"] is True
    # The tool name was never added to the Always set.
    assert MANUAL_TOOL not in ui.auto_approve_tools
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    assert rows[0]["always_suppressed"] is True
    # The published approval_resolved SSE/broadcast reports effective
    # always=False even though the stale client requested always=True.
    resolved = [e for e in captured if e.get("type") == "approval_resolved"]
    assert resolved, "expected an approval_resolved event"
    assert resolved[0].get("always") is False


def test_manual_always_false_records_false() -> None:
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result = _run_gate_with_resolution(
        ui,
        storage,
        items,
        verdicts={MANUAL_TOOL: "manual"},
        approved=True,
        always=False,
    )

    assert result["approved"] is True
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    assert rows[0]["always_suppressed"] is False


# ---------------------------------------------------------------------------
# Subsequent invocation requires a fresh manual card
# ---------------------------------------------------------------------------


def test_manual_subsequent_invocation_requires_new_cycle() -> None:
    """One approval clears exactly one invocation; the next invocation
    must reach a fresh ApprovalCycle even after a prior approval."""
    ui = _new_ui()
    storage = MagicMock()

    for round_idx in range(2):
        items = [_make_manual_item(call_id=f"c-{round_idx}")]
        result = _run_gate_with_resolution(
            ui,
            storage,
            items,
            verdicts={MANUAL_TOOL: "manual"},
            approved=True,
        )
        assert result["approved"] is True
        assert items[0].get("denied") is not True


# ---------------------------------------------------------------------------
# Workstream-wide sweeps (Stop / cancel / session close) emit manual audit
# ---------------------------------------------------------------------------


def test_manual_sweep_resolution_emits_manual_audit() -> None:
    """A workstream-wide sweep (e.g. Stop / cancel) that denies a manual
    cycle must emit ``tool.manual_resolved`` with the sweep decision."""
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result: dict[str, Any] = {}

    def gate() -> None:
        try:
            with _patch_policies({MANUAL_TOOL: "manual"}):
                result["approved"], result["err"] = ui.approve_tools(items)
        except BaseException as exc:  # pragma: no cover - failure reporter
            result["exc"] = repr(exc)

    with _patch_storage(storage):
        t = threading.Thread(target=gate, daemon=True)
        t.start()
        try:
            _wait_until_pending(ui)
            swept = ui.resolve_all_approvals(False, "Cancelled by user")
        finally:
            t.join(timeout=5.0)

    assert swept == 1
    assert result["approved"] is False
    assert items[0].get("denied") is True
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1, "expected a tool.manual_resolved audit row from the sweep"
    assert rows[0]["approval_mode"] == "manual"
    assert rows[0]["decision"] == "denied"
    assert rows[0]["tool_name"] == MANUAL_TOOL
    assert rows[0]["always_suppressed"] is False
    # Automatic sweep (workstream close / recovery / system cancel): NO
    # human actor — never attributed to the workstream owner.
    assert rows[0]["user_id"] == ""
    assert rows[0]["source"] == "system"


def test_manual_human_initiated_sweep_propagates_actor() -> None:
    """A user-initiated Stop/cancel sweep that can prove the authenticated
    initiator propagates that actor with source='human'."""
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result: dict[str, Any] = {}

    def gate() -> None:
        try:
            with _patch_policies({MANUAL_TOOL: "manual"}):
                result["approved"], result["err"] = ui.approve_tools(items)
        except BaseException as exc:  # pragma: no cover - failure reporter
            result["exc"] = repr(exc)

    with _patch_storage(storage):
        t = threading.Thread(target=gate, daemon=True)
        t.start()
        try:
            _wait_until_pending(ui)
            ui.resolve_all_approvals(
                False,
                "Cancelled by user",
                resolving_user_id="u-human",
                source="human",
            )
        finally:
            t.join(timeout=5.0)

    assert result["approved"] is False
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    assert rows[0]["decision"] == "denied"
    assert rows[0]["user_id"] == "u-human"  # proven human initiator
    assert rows[0]["source"] == "human"


def test_manual_sweep_timeout_audit_has_no_human_actor() -> None:
    """A timeout sweep of a manual cycle carries no human actor."""
    ui = _new_ui()
    items = [_make_manual_item()]
    storage = MagicMock()
    result: dict[str, Any] = {}

    def gate() -> None:
        try:
            with _patch_policies({MANUAL_TOOL: "manual"}):
                result["approved"], result["err"] = ui.approve_tools(items)
        except BaseException as exc:  # pragma: no cover - failure reporter
            result["exc"] = repr(exc)

    with _patch_storage(storage):
        t = threading.Thread(target=gate, daemon=True)
        t.start()
        try:
            _wait_until_pending(ui)
            ui.resolve_all_approvals(False, "timeout", timeout=True)
        finally:
            t.join(timeout=5.0)

    assert result["approved"] is False
    rows = _manual_audit_rows(storage)
    assert len(rows) == 1
    assert rows[0]["decision"] == "timeout"
    assert rows[0]["user_id"] == ""  # no human actor on a timeout sweep
    assert rows[0]["source"] == "system"
    assert rows[0]["always_suppressed"] is False


# ---------------------------------------------------------------------------
# Non-manual regression
# ---------------------------------------------------------------------------


def test_non_manual_allow_still_auto_approves() -> None:
    """Ordinary ``allow`` policy behavior is unchanged (no human gate)."""
    ui = _new_ui()
    items = [_make_item("c1", READ_TOOL)]
    storage = MagicMock()
    with _patch_storage(storage), _patch_policies({READ_TOOL: "allow"}):
        approved, _err = ui.approve_tools(items)
    assert approved is True
    snapshot = ui.serialize_recent_auto_approvals()
    assert len(snapshot) == 1 and snapshot[0]["auto_approve_reason"] == "policy"


def test_non_manual_deny_still_blocks() -> None:
    ui = _new_ui()
    items = [_make_item("c1", "delete_workstream")]
    storage = MagicMock()
    with _patch_storage(storage), _patch_policies({"delete_workstream": "deny"}):
        approved, err = ui.approve_tools(items)
    assert approved is False
    assert err == "Blocked by tool policy"
    assert items[0].get("denied") is True


def test_non_manual_ask_still_reaches_human() -> None:
    """Ordinary ``ask`` (incl. Smart Approvals when enabled) unchanged."""
    ui = _new_ui()
    ui.smart_approvals_enabled = True
    items = [_make_item("c1", READ_TOOL)]
    storage = MagicMock()
    with (
        _patch_storage(storage),
        _patch_policies({READ_TOOL: "ask"}),
        patch.object(ui, "_apply_smart_approvals", wraps=ui._apply_smart_approvals) as spy,
    ):
        result = _run_gate_with_resolution(
            ui,
            storage,
            items,
            verdicts={READ_TOOL: "ask"},
            approved=True,
        )
    assert result["approved"] is True
    spy.assert_called_once()


# ---------------------------------------------------------------------------
# Card fidelity + argument digest
# ---------------------------------------------------------------------------


def test_manual_card_carries_mode_and_digest_not_full_args() -> None:
    """The approval card exposes the manual mode and the arg digest, but
    NOT the raw full prepared arguments (which may carry secrets); the
    digest still binds to the exact prepared execution args."""
    from turnstone.core.session_ui_base import manual_arg_digest, manual_execution_args

    ui = _new_ui()
    item = _make_item("c1", MANUAL_TOOL, mcp_args={"agent_id": "main", "model": "gpt-x"})
    item["_manual"] = True
    item["_manual_arg_digest"] = manual_arg_digest(item)

    serialized = ui._serialize_approval_items([item])[0]
    assert serialized["approval_mode"] == "manual"
    assert serialized["arg_digest"] == manual_arg_digest(item)
    # Raw full arguments are not broadcast on the wire card.
    assert "full_args" not in serialized
    assert "gpt-x" not in serialized["preview"]
    # Digest binds to the exact prepared execution args (MCP args), not
    # the 200-char preview or reconstructed text.
    assert manual_execution_args(item) == {"agent_id": "main", "model": "gpt-x"}


def test_arg_digest_canonical_and_byte_exact() -> None:
    """SHA-256 over canonical JSON: key order independent, UTF-8, compact,
    Unicode preserved, lists ordered, scalars standard."""
    from turnstone.core.session_ui_base import canonical_args_json, manual_arg_digest

    a = {"z": 1, "a": {"y": 2, "x": [1, 2, 3]}, "u": "ไทย"}
    b = {"u": "ไทย", "a": {"x": [1, 2, 3], "y": 2}, "z": 1}
    assert canonical_args_json(a) == canonical_args_json(b)
    assert manual_arg_digest({"mcp_args": a}) == manual_arg_digest({"mcp_args": b})

    # Whitespace/newlines must not change the digest (compact separators).
    assert canonical_args_json({"k": [1, 2]}) == '{"k":[1,2]}'
    # Lists preserve order.
    assert canonical_args_json({"k": [1, 2]}) != canonical_args_json({"k": [2, 1]})
