"""The approval gate starts intent inference only for calls that still need it.

``_evaluate_intent(defer_llm=True)`` stamps ``_judge_dispatch`` on the batch and
spends nothing.  ``approve_tools`` resolves the batch against the operator's own
rules first (policy, "Always" grants, blanket auto-approve); only then does it
know which calls can still consume a verdict.  That decision is what these tests
pin: settled batches must never start a daemon, and partially settled batches
must judge exactly the remainder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from tests.conftest import resolve_when_pending
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI


def _make_items(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "call_id": call_id,
            "header": f"Tool: {func}",
            "preview": "preview text",
            "func_name": func,
            "approval_label": func,
            "needs_approval": True,
        }
        for call_id, func in pairs
    ]


def _stamp_dispatcher(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Stamp a recording dispatcher the way ``_evaluate_intent`` does."""
    calls: list[list[dict[str, Any]]] = []

    def _dispatch(remaining: list[dict[str, Any]] | None = None) -> None:
        calls.append([] if remaining is None else list(remaining))

    for item in items:
        item["_judge_dispatch"] = _dispatch
    return calls


def _patch_storage(storage: Any):
    return patch("turnstone.core.storage._registry.get_storage", return_value=storage)


def _patch_policies(verdicts: dict[str, str]):
    return patch(
        "turnstone.core.policy.evaluate_loaded_tool_policies",
        return_value=verdicts,
    )


def _run(ui: ConsoleCoordinatorUI, items: list[dict[str, Any]], **policies: str) -> None:
    with _patch_storage(MagicMock()):
        with patch(
            "turnstone.core.policy.evaluate_loaded_tool_policies",
            return_value=dict(policies),
        ):
            ui.approve_tools(items)


def test_blanket_auto_approve_never_starts_the_judge() -> None:
    """The whole batch settles on the operator's blanket flag; no verdict can
    be read, so starting the daemon would spend inference and discard it."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve = True
    items = _make_items(("c1", "spawn_workstream"), ("c2", "delete_workstream"))
    dispatched = _stamp_dispatcher(items)

    _run(ui, items)

    assert dispatched == [[]], f"expected one retire call, got {dispatched!r}"


def test_always_grant_covering_the_batch_never_starts_the_judge() -> None:
    """A per-tool "Always" grant settles every call in the batch locally."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve_tools = {"spawn_workstream"}
    ui._auto_approve_tools_source = {"spawn_workstream": "always"}
    items = _make_items(("c1", "spawn_workstream"))
    dispatched = _stamp_dispatcher(items)

    _run(ui, items)

    assert dispatched == [[]], f"expected one retire call, got {dispatched!r}"


def test_policy_allow_never_starts_the_judge() -> None:
    """An admin ``allow`` rule approves the call with no verdict consulted."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "spawn_workstream"))
    dispatched = _stamp_dispatcher(items)

    _run(ui, items, spawn_workstream="allow")

    assert dispatched == [[]], f"expected one retire call, got {dispatched!r}"


def test_partial_settlement_judges_only_the_remainder() -> None:
    """Policy settles one call; the person still sees the other's verdict."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "spawn_workstream"), ("c2", "delete_workstream"))
    dispatched = _stamp_dispatcher(items)

    timer = resolve_when_pending(ui, False)
    timer.start()
    try:
        _run(ui, items, spawn_workstream="allow")
    finally:
        timer.cancel()

    assert len(dispatched) == 1, f"expected a single dispatch, got {dispatched!r}"
    judged = dispatched[0]
    assert [it["call_id"] for it in judged] == ["c2"], (
        "only the undecided call may start inference; a settled one has no consumer"
    )


def test_human_gated_batch_judges_the_pending_items() -> None:
    """No local rule settles the batch, so the verdict backs the person's card."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    items = _make_items(("c1", "delete_workstream"))
    dispatched = _stamp_dispatcher(items)

    timer = resolve_when_pending(ui, False)
    timer.start()
    try:
        _run(ui, items)
    finally:
        timer.cancel()

    assert len(dispatched) == 1
    assert [it["call_id"] for it in dispatched[0]] == ["c1"]


def test_unstamped_batch_takes_the_historical_path() -> None:
    """Callers that do not defer pass no dispatcher and are unaffected."""
    ui = ConsoleCoordinatorUI(ws_id="coord-1", user_id="u1")
    ui.auto_approve = True
    items = _make_items(("c1", "spawn_workstream"))

    # No ``_judge_dispatch`` stamp: the gate must not invent one.
    _run(ui, items)

    assert "_judge_dispatch" not in items[0]
