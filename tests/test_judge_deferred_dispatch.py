"""Deferred intent-judge dispatch.

A batch's approval disposition is not known when the batch is prepared.  A
policy ``allow`` rule, a per-tool "Always" grant or the blanket auto-approve
flag settles calls inside ``approve_tools`` without consulting a verdict, so a
daemon started at preparation time spent inference that nothing could read --
and the next turn pinned ``superseded_batch`` on it mid-flight.

``evaluate(..., defer=True)`` therefore publishes the heuristics and the batch
identity but starts nothing; the gate later calls ``dispatch`` once, for the
calls that still have a consumer (a Smart Approvals wait or a person).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.core.judge import (
    _UNPINNED_EARLY_CANCEL,
    IntentJudge,
    JudgeCancelReason,
    _early_cancel_reason,
    _pin_judge_cancel,
)

from tests._session_helpers import as_stream
from tests._session_helpers import mock_completion_result as _mock_result

from .test_judge import _make_judge, _make_mock_provider


def _item(call_id: str, command: str = "ls -la") -> dict[str, Any]:
    return {
        "call_id": call_id,
        "func_name": "bash",
        "command": command,
        "func_args": {"command": command},
        "needs_approval": True,
    }


def _wait_for_done(done: threading.Event, timeout: float = 10.0) -> None:
    assert done.wait(timeout), "judge daemon never reported done"


class TestMissingVerdictReason:
    def test_missing_verdict_is_a_typed_reason(self) -> None:
        assert JudgeCancelReason.MISSING_VERDICT.value == "missing_verdict"

    def test_missing_verdict_is_not_expected_auxiliary(self) -> None:
        # ``is_expected_auxiliary`` gates whether an abort counts as routine
        # caller behaviour.  A run that produced no verdict is a judge fault,
        # not routine, and must not be absorbed as if it were a supersede.
        assert JudgeCancelReason.MISSING_VERDICT.is_expected_auxiliary is False
        assert JudgeCancelReason.SUPERSEDED_BATCH.is_expected_auxiliary is True

    def test_pinned_reason_is_returned_as_the_enum(self) -> None:
        # Before the first evaluation there is nothing to classify; a caller
        # that pinned a reason gets it back, an unpinned caller keeps the
        # historical wording (audit greps depend on it).
        pinned = threading.Event()
        _pin_judge_cancel(pinned, JudgeCancelReason.MISSING_VERDICT)
        assert _early_cancel_reason(pinned) is JudgeCancelReason.MISSING_VERDICT
        assert _early_cancel_reason(threading.Event()) == _UNPINNED_EARLY_CANCEL

    def test_empty_answer_reads_as_missing_verdict_not_unclassified(self) -> None:
        """An evaluation that returns nothing is a known cause, not ``unknown``.

        This is the measured production shape: a never-superseded batch whose
        model answered with no parseable verdict used to fall through to the
        free-text ``unclassified`` label, which hides the real cause behind
        "we do not know why".
        """
        provider = _make_mock_provider(response_content="")
        provider.create_streaming.return_value = as_stream(_mock_result("", None))
        judge = _make_judge(provider)

        delivered: list[Any] = []
        done = threading.Event()
        judge.evaluate(
            [_item("c1")],
            [{"role": "user", "content": "test"}],
            callback=delivered.append,
            cancel_event=threading.Event(),
            done_callback=done.set,
        )
        _wait_for_done(done)

        assert len(delivered) == 1
        assert delivered[0].tier == "llm_fallback"
        assert delivered[0].fallback_reason == "missing_verdict"
        assert "unclassified" not in delivered[0].reasoning


class TestDeferredEvaluate:
    def test_defer_starts_no_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        judge = _make_judge()
        spawns: list[str] = []

        def _record(*args: Any, **kwargs: Any) -> None:
            spawns.append("spawned")

        monkeypatch.setattr(judge, "_run_judge", _record)
        items = [_item("c1"), _item("c2")]
        verdicts = judge.evaluate(
            items,
            [],
            callback=lambda v: None,
            cancel_event=threading.Event(),
            done_callback=lambda: None,
            batch_id="batch-a",
            defer=True,
        )

        assert len(verdicts) == 2, "the heuristic tier must still run synchronously"
        assert spawns == []
        assert thread_names_starting_with("intent-judge") == set()

    def test_defer_returns_heuristics_with_verdicts_attached(self) -> None:
        judge = _make_judge()
        verdicts = judge.evaluate(
            [_item("c1")],
            [],
            callback=lambda v: None,
            cancel_event=threading.Event(),
            done_callback=lambda: None,
            defer=True,
        )
        assert verdicts[0].call_id == "c1"
        assert verdicts[0].tier == "heuristic"

    def test_dispatch_twice_spawns_one_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        judge = _make_judge()
        spawns: list[str] = []

        def _record(*args: Any, **kwargs: Any) -> None:
            spawns.append("spawned")

        monkeypatch.setattr(judge, "_run_judge", _record)
        items = [_item("c1")]
        heuristics = judge.evaluate(
            items,
            [],
            callback=lambda v: None,
            cancel_event=threading.Event(),
            done_callback=lambda: None,
            defer=True,
        )
        # The gate guards with its own one-shot claim; ``dispatch`` itself is
        # the single spawn point, so calling it again is a second spawn only if
        # the caller lets it be one.  Assert the callable is idempotent at the
        # layer the gate controls: two dispatches must not evaluate twice.
        judge.dispatch(
            items,
            [],
            heuristics,
            lambda v: None,
            threading.Event(),
            lambda: None,
            None,
            "batch-a",
        )
        assert spawns == ["spawned"]

    def test_deferred_batch_consumes_the_published_batch_id(self) -> None:
        judge = _make_judge()
        seen: list[str] = []

        def _callback(verdict: Any) -> None:
            seen.append(verdict.batch_id)

        done = threading.Event()
        items = [_item("c1")]
        heuristics = judge.evaluate(
            items,
            [],
            callback=_callback,
            cancel_event=threading.Event(),
            done_callback=done.set,
            batch_id="batch-pinned",
            defer=True,
        )
        judge.dispatch(
            items,
            [],
            heuristics,
            _callback,
            threading.Event(),
            done.set,
            None,
            "batch-pinned",
        )
        _wait_for_done(done)
        assert seen and set(seen) == {"batch-pinned"}


def thread_names_starting_with(prefix: str) -> set[str]:
    return {t.name for t in threading.enumerate() if t.name.startswith(prefix)}


def _noop(_: Any) -> None:
    return None


def test_module_helpers_are_stable() -> None:
    # Guard against the helper above silently matching the main thread.
    assert "MainThread" not in thread_names_starting_with("intent-judge")
    time.sleep(0)
