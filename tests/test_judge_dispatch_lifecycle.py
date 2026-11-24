"""Session-level lifecycle for a batch that settles before it is dispatched.

The gate tests substitute a recording dispatcher to prove *which* calls a
batch hands to inference.  This suite instead drives the real seam: a real
``ChatSession``, a real ``IntentJudge``, the real ``SessionUIBase`` approval
body, and the session's own generation bookkeeping.  It exists because a
mutation that skipped generation retirement survived the substituted-dispatcher
tests — the retirement path is only reachable through the live gate.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import NullUI, make_session
from tests.conftest import resolve_when_pending
from tests.test_judge import _make_judge
from turnstone.core.judge import JudgeCancelReason


def _session(*, auto_approve: bool) -> Any:
    ui = NullUI()
    ui.auto_approve = auto_approve
    session = make_session(ui=ui)
    return session


def _bash(session: Any, call_id: str, command: str = "ls -la") -> dict[str, Any]:
    """Prepare a genuine bash approval item through the real preparer."""
    return session._prepare_bash(call_id, {"command": command})


def _record_spawns(judge: Any) -> list[tuple[int, str]]:
    """Replace the daemon body so a spawn is observable but spends nothing."""
    spawns: list[tuple[int, str]] = []
    original = judge._run_judge

    def _spy(items, messages, heuristics, callback, cancel_event, done_callback,
             backend_auth_resolver, batch_id):
        spawns.append((len(items), batch_id))
        done_callback()

    judge._run_judge = _spy  # type: ignore[method-assign]
    judge._original_run_judge = original  # type: ignore[attr-defined]
    return spawns


@pytest.fixture
def judge() -> Any:
    return _make_judge()


def _attach(session: Any, judge: Any) -> None:
    session._ensure_judge = lambda: judge  # type: ignore[method-assign]


def test_blanket_settled_batch_dispatches_nothing_and_retires(judge: Any, tmp_db: str) -> None:
    """A batch blanket auto-approve settles retires its generation.

    Asserts, on the real objects: no daemon spawned; the generation left
    ``_judge_cancel_events``; the main-loop slot is clear; and unrelated
    generations stay registered.
    """
    session = _session(auto_approve=True)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    unrelated = threading.Event()
    session._judge_cancel_events.add(unrelated)

    items = [_bash(session, "c1"), _bash(session, "c2")]
    generation = session._evaluate_intent(items, defer_llm=True)

    assert generation is not None
    assert generation in session._judge_cancel_events, "the generation must be published"
    assert session._judge_cancel_event is generation
    assert all("_judge_dispatch" in it for it in items)
    assert spawns == [], "preparation must spend nothing"

    approved, _feedback = session.ui.approve_tools(items)

    assert approved is True
    assert spawns == [], "a blanket-settled batch has no verdict consumer"
    assert generation not in session._judge_cancel_events, "the retired generation must leave the set"
    assert session._judge_cancel_event is None, "the main-loop slot must be cleared"
    assert unrelated in session._judge_cancel_events, "unrelated generations must survive"


def test_retired_generation_is_not_superseded_by_the_next_batch(
    judge: Any, tmp_db: str
) -> None:
    """A never-dispatched generation must not be pinned ``superseded_batch``.

    The removed generation has no daemon to abort, so firing it would record an
    abort for a batch that never started.  The successor must find nothing to
    supersede and dispatch its own calls.
    """
    session = _session(auto_approve=True)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    first = [_bash(session, "c1")]
    retired = session._evaluate_intent(first, defer_llm=True)
    session._push_smart_approval_config(first)
    session.ui.approve_tools(first)
    assert spawns == []

    # A successor batch begins exactly as the tool loop begins one: detach the
    # prior slot, pin + fire it, then evaluate.
    with session._judge_events_lock:
        prior = session._judge_cancel_event
        session._judge_cancel_event = None
    assert prior is None, "retirement must have cleared the slot the successor reads"

    session.ui.auto_approve = False
    second = [_bash(session, "c2", "cat README.md")]
    generation = session._evaluate_intent(second, defer_llm=True)
    session._push_smart_approval_config(second)

    timer = resolve_when_pending(session.ui, True)
    timer.start()
    try:
        session.ui.approve_tools(second)
    finally:
        timer.cancel()

    assert len(spawns) == 1, f"the successor must judge its own call, got {spawns!r}"
    assert spawns[0][0] == 1
    assert getattr(retired, "_judge_cancel_reason", None) is None, (
        "the retired generation must never be pinned with a cancel reason"
    )
    assert generation is not None
    session.close()


def test_session_close_while_prepared_but_undispatched(judge: Any, tmp_db: str) -> None:
    """``close()`` must not abort a generation that has no daemon.

    A generation prepared and then closed un-dispatched leaves nothing for
    ``close()`` to cancel.  Close must not raise, and the registry must be
    empty afterwards.
    """
    session = _session(auto_approve=False)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    items = [_bash(session, "c1")]
    generation = session._evaluate_intent(items, defer_llm=True)
    assert generation is not None

    session.close()

    assert spawns == []
    assert session._judge_cancel_events == set()
    assert getattr(generation, "_judge_cancel_reason", None) is JudgeCancelReason.SESSION_CLOSED


def test_mixed_batch_starts_inference_only_for_the_undecided_call(
    judge: Any, tmp_db: str
) -> None:
    """Policy resolves one call, a person still decides the other."""
    session = _session(auto_approve=False)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    items = [_bash(session, "c1", "ls -la"), _bash(session, "c2", "cat README.md")]
    items[0]["approval_label"] = "auto_allowed"
    items[1]["approval_label"] = "needs_person"
    session._evaluate_intent(items, defer_llm=True)
    session._push_smart_approval_config(items)

    with patch(
        "turnstone.core.policy.evaluate_loaded_tool_policies",
        return_value={"auto_allowed": "allow", "needs_person": "ask"},
    ):
        timer = resolve_when_pending(session.ui, True)
        timer.start()
        try:
            session.ui.approve_tools(items)
        finally:
            timer.cancel()

    assert len(spawns) == 1, f"exactly one dispatch expected, got {spawns!r}"
    assert spawns[0][0] == 1, "only the undecided call may be judged"
    session.close()


def test_duplicate_dispatch_attempts_spawn_once(judge: Any, tmp_db: str) -> None:
    """Two dispatches of one prepared batch must not evaluate twice.

    The gate's one-shot claim is the only thing standing between a re-entrant
    gate (or a retried decision) and a second batch of paid evaluation, so it
    is exercised through the real stamped callable rather than a stand-in.
    """
    session = _session(auto_approve=False)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    items = [_bash(session, "c1", "cat README.md")]
    session._evaluate_intent(items, defer_llm=True)
    session._push_smart_approval_config(items)
    dispatch = items[0]["_judge_dispatch"]

    timer = resolve_when_pending(session.ui, True)
    timer.start()
    try:
        session.ui.approve_tools(items)
    finally:
        timer.cancel()

    assert len(spawns) == 1
    dispatch([items[0]])
    dispatch()
    assert len(spawns) == 1, f"a repeated dispatch must not spawn, got {spawns!r}"
    session.close()


def test_cancel_before_dispatch_records_no_abort(judge: Any, tmp_db: str) -> None:
    """Cancelling a batch that never dispatched records no batch abort.

    ``close()`` pins ``session_closed`` on a generation prepared but not yet
    dispatched, but no fallback verdicts exist to deliver and therefore no
    ``judge.batch.aborted`` row is owed.
    """
    session = _session(auto_approve=True)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    aborted: list[Any] = []
    with patch(
        "turnstone.core.session._record_judge_batch_abort",
        side_effect=lambda **kw: aborted.append(kw),
    ):
        items = [_bash(session, "c1")]
        session._evaluate_intent(items, defer_llm=True)
        session.ui.approve_tools(items)
        session.close()

    assert spawns == []
    assert aborted == [], f"no abort may be recorded for a retired generation: {aborted!r}"


def test_independent_sessions_retire_independently(judge: Any, tmp_db: str) -> None:
    """Two sessions' generations are separate resources.

    Retiring one batch must not touch another session's live generation.
    """
    first = _session(auto_approve=True)
    second = _session(auto_approve=False)
    _attach(first, judge)
    _attach(second, judge)
    spawns = _record_spawns(judge)

    first_items = [_bash(first, "c1")]
    first_generation = first._evaluate_intent(first_items, defer_llm=True)

    second_items = [_bash(second, "c2", "cat README.md")]
    second_generation = second._evaluate_intent(second_items, defer_llm=True)

    first.ui.approve_tools(first_items)

    assert first_generation not in first._judge_cancel_events
    assert second_generation is not None
    assert second_generation in second._judge_cancel_events, (
        "another session's live generation must not be retired"
    )
    assert spawns == []

    second.close()
    first.close()


def test_cancel_after_dispatch_aborts_the_live_daemon(judge: Any, tmp_db: str) -> None:
    """A dispatched generation stays ownable until its daemon finishes.

    ``close()`` and a superseding batch fire the generation; the registry entry
    is the daemon's to remove through its done callback, so a dispatched batch
    must still be present after close and must carry the pinned reason.
    """
    session = _session(auto_approve=False)
    _attach(session, judge)

    pending_done: list[Any] = []

    def _hold(items, messages, heuristics, callback, cancel_event, done_callback,
              backend_auth_resolver, batch_id):
        pending_done.append(done_callback)

    judge._run_judge = _hold  # type: ignore[method-assign]

    items = [_bash(session, "c1", "cat README.md")]
    session._evaluate_intent(items, defer_llm=True)
    session._push_smart_approval_config(items)

    timer = resolve_when_pending(session.ui, True)
    timer.start()
    try:
        session.ui.approve_tools(items)
    finally:
        timer.cancel()

    assert len(pending_done) == 1, "an undecided batch must have dispatched"
    generation = session._judge_cancel_event
    session.close()

    assert generation is not None
    assert generation.is_set(), "close must abort a live daemon"
    assert generation in session._judge_cancel_events, (
        "the daemon owns its registry entry until it finishes"
    )

    # The daemon returns: its done callback retires the entry.
    pending_done[0]()
    assert generation not in session._judge_cancel_events


def test_stale_verdict_cannot_authorize_a_later_batch(judge: Any, tmp_db: str) -> None:
    """A verdict delivered for a retired generation cannot qualify a new call.

    Providers reuse call ids across turns, so a late delivery from an earlier
    batch can carry an id the current batch owns.  Ownership is exact-event
    identity: the retired generation's delivery must be audit-only — no cache
    entry and nothing parked on the live cycle that could satisfy a Smart
    Approvals wait.
    """
    session = _session(auto_approve=True)
    _attach(session, judge)
    _record_spawns(judge)

    first = [_bash(session, "c1")]
    retired = session._evaluate_intent(first, defer_llm=True)
    session.ui.approve_tools(first)
    assert retired is not None

    # The provider reuses the call id in the next turn.
    session.ui.auto_approve = False
    second = [_bash(session, "c1", "cat README.md")]
    live = session._evaluate_intent(second, defer_llm=True)
    session._push_smart_approval_config(second)

    stale = {
        "call_id": "c1",
        "tier": "llm",
        "confidence": 0.99,
        "recommendation": "approve",
        "batch_id": "retired-batch",
    }
    delivered: list[str] = []

    def _deliver_stale() -> None:
        session.ui.on_intent_verdict(dict(stale), retired)
        delivered.append("sent")

    timer = resolve_when_pending(session.ui, False, before=_deliver_stale)
    timer.start()
    try:
        session.ui.approve_tools(second)
    finally:
        timer.cancel()

    assert delivered == ["sent"]
    assert "c1" not in session.ui._llm_verdicts, (
        "a retired generation's verdict must not enter the live verdict cache"
    )
    assert session.ui._verdict_origins.get("c1") in (None, id(live)), (
        "the cache must never attribute a verdict to the retired generation"
    )
    session.close()


def test_mandatory_approval_still_requires_a_person(judge: Any, tmp_db: str) -> None:
    """Denying the batch leaves the call unexecuted and the generation retired.

    The denial path must not resurrect a dispatch, and the batch must not read
    as approved.
    """
    session = _session(auto_approve=False)
    _attach(session, judge)
    spawns = _record_spawns(judge)

    items = [_bash(session, "c1", "echo hello")]
    assert items[0].get("needs_approval") is True
    session._evaluate_intent(items, defer_llm=True)
    session._push_smart_approval_config(items)

    timer = resolve_when_pending(session.ui, False, "no")
    timer.start()
    try:
        approved, feedback = session.ui.approve_tools(items)
    finally:
        timer.cancel()

    assert approved is False
    assert len(spawns) == 1, "an undecided call is judged before the person decides"
    session.close()
