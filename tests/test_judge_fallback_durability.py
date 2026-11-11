"""Durability of judge fallback accounting (#19).

The point of these tests is narrow and operational: after a judge batch is
abandoned, can an operator who has **only the database** (no systemd journal,
no Loki) answer:

* how many abandonments happened, and why (superseded vs deadline vs fault)?
* which specific verdicts belong to one abandoned batch?

The answer today is: the cause is durable inside ``intent_verdicts.reasoning``
(every fallback row is the heuristic verdict relabelled ``llm_fallback`` with
the cause in parentheses), and the batch is durable as one
``judge.batch.aborted`` audit row whose ``call_ids`` join back to those rows.
``batch_id`` also rides the live verdict dict and the structured log, but it is
NOT a stored column — these tests pin what is actually queryable.
"""

from __future__ import annotations

import json
import threading

import pytest

from tests.test_judge import _make_item, _make_judge, _make_mock_provider
from turnstone.core.judge import (
    UNCLASSIFIED_FALLBACK_REASON,
    IntentJudge,
    JudgeCancelReason,
    _pin_judge_cancel,
    fallback_metric_reason,
)
from turnstone.core.session import _JUDGE_AUDIT_MAX_CALL_IDS, _record_judge_batch_abort

WS_ID = "ws-durability"


def _heuristic_verdict(call_id: str):
    """Build a heuristic verdict through the production rule engine."""
    from turnstone.core.judge import evaluate_heuristic

    return evaluate_heuristic("bash", {"command": "echo hi"}, "bash", call_id)


def _persist_fallback(db, verdict, call_id: str) -> None:
    """Persist a fallback verdict exactly as the session UI base does."""
    db.upsert_intent_verdict(
        verdict_id=verdict.verdict_id,
        ws_id=WS_ID,
        call_id=call_id,
        func_name=verdict.func_name,
        func_args=verdict.func_args,
        intent_summary=verdict.intent_summary,
        risk_level=verdict.risk_level,
        confidence=verdict.confidence,
        recommendation=verdict.recommendation,
        reasoning=verdict.reasoning,
        evidence=json.dumps(verdict.evidence),
        tier=verdict.tier,
        judge_model=verdict.judge_model,
        latency_ms=verdict.latency_ms,
    )


def _fallback_from_judge(reason: JudgeCancelReason, call_id: str, batch_id: str):
    judge = IntentJudge.__new__(IntentJudge)
    judge._model = "test-model"  # only used as the verdict's judge_model label
    return judge._fallback_verdict(
        _heuristic_verdict(call_id),
        reason,
        batch_id=batch_id,
    )


class TestCauseIsDurablyQueryable:
    def test_reasoning_carries_the_bounded_cause(self, tmp_db):
        """The cause survives a real storage round-trip, not just the wire."""
        from turnstone.core.storage._registry import get_storage

        db = get_storage()
        verdict = _fallback_from_judge(JudgeCancelReason.SUPERSEDED_BATCH, "tc_1", "b1")
        _persist_fallback(db, verdict, "tc_1")

        row = db.get_intent_verdict(verdict.verdict_id)
        assert row is not None
        assert row["tier"] == "llm_fallback"
        assert "(superseded_batch)" in row["reasoning"]
        # The legacy wording is preserved verbatim for callers that never
        # pinned, so old dashboards/greps keep working.
        assert "superseded_batch" in fallback_metric_reason(JudgeCancelReason.SUPERSEDED_BATCH)

    def test_causes_are_distinguishable_in_the_same_table(self, tmp_db):
        """Supersede, deadline and fault must not collapse into one bucket."""
        from turnstone.core.storage._registry import get_storage

        db = get_storage()
        cases = {
            "tc_supersede": JudgeCancelReason.SUPERSEDED_BATCH,
            "tc_deadline": JudgeCancelReason.DEADLINE,
            "tc_fault": JudgeCancelReason.PROVIDER_ERROR,
        }
        for call_id, reason in cases.items():
            _persist_fallback(db, _fallback_from_judge(reason, call_id, "b1"), call_id)

        rows = db.list_intent_verdicts(ws_id=WS_ID)
        by_call = {row["call_id"]: row for row in rows}
        assert len(by_call) == 3
        for call_id, reason in cases.items():
            assert f"({reason.value})" in by_call[call_id]["reasoning"]

        # A receipt can be computed from the database alone.
        superseded = [r for r in rows if "(superseded_batch)" in r["reasoning"]]
        assert [r["call_id"] for r in superseded] == ["tc_supersede"]


class TestBatchCorrelation:
    def test_abort_writes_one_audit_row_joinable_to_verdicts(self, tmp_db):
        """The audit row's call_ids resolve to the abandoned verdict rows."""
        from turnstone.core.storage._registry import get_storage

        db = get_storage()
        call_ids = ["tc_a", "tc_b"]
        for call_id in call_ids:
            _persist_fallback(
                db,
                _fallback_from_judge(JudgeCancelReason.SUPERSEDED_BATCH, call_id, "b-42"),
                call_id,
            )

        _record_judge_batch_abort(
            ws_id=WS_ID,
            user_id="user-1",
            batch_id="b-42",
            reason=JudgeCancelReason.SUPERSEDED_BATCH,
            call_ids=call_ids,
        )

        events = db.list_audit_events(action="judge.batch.aborted", resource_id=WS_ID)
        assert len(events) == 1
        detail = json.loads(events[0]["detail"])
        assert detail["batch_id"] == "b-42"
        assert detail["reason"] == "superseded_batch"
        assert detail["abandoned_calls"] == 2
        assert detail["truncated"] is False

        # Real end-to-end join on durable ids only.
        rows = {row["call_id"]: row for row in db.list_intent_verdicts(ws_id=WS_ID)}
        for call_id in detail["call_ids"]:
            assert call_id in rows
            assert "(superseded_batch)" in rows[call_id]["reasoning"]

    def test_audit_detail_is_bounded_and_capped(self, tmp_db):
        from turnstone.core.storage._registry import get_storage

        db = get_storage()
        many = [f"tc_{i}" for i in range(_JUDGE_AUDIT_MAX_CALL_IDS + 5)]
        _record_judge_batch_abort(
            ws_id=WS_ID,
            user_id="",
            batch_id="b-big",
            reason=JudgeCancelReason.DEADLINE,
            call_ids=many,
        )

        detail = json.loads(
            db.list_audit_events(action="judge.batch.aborted", resource_id=WS_ID)[0]["detail"]
        )
        assert detail["abandoned_calls"] == len(many)
        assert len(detail["call_ids"]) == _JUDGE_AUDIT_MAX_CALL_IDS
        assert detail["truncated"] is True

    def test_no_row_without_abandonment(self, tmp_db):
        """A batch that fully evaluated must not create a false abort row."""
        from turnstone.core.storage._registry import get_storage

        db = get_storage()
        _record_judge_batch_abort(
            ws_id=WS_ID,
            user_id="",
            batch_id="b-none",
            reason=JudgeCancelReason.SUPERSEDED_BATCH,
            call_ids=[],
        )
        assert db.list_audit_events(action="judge.batch.aborted") == []

    def test_unpinned_cause_is_labelled_not_dropped(self, tmp_db):
        """An unclassified cause is still recorded, under a bounded label."""
        from turnstone.core.storage._registry import get_storage

        db = get_storage()
        _record_judge_batch_abort(
            ws_id=WS_ID,
            user_id="",
            batch_id="b-legacy",
            reason="judge cancelled before evaluating this call",
            call_ids=["tc_legacy"],
        )
        detail = json.loads(
            db.list_audit_events(action="judge.batch.aborted", resource_id=WS_ID)[0]["detail"]
        )
        assert detail["reason"] == UNCLASSIFIED_FALLBACK_REASON


class TestMetricAggregation:
    def test_fallback_counter_is_bounded_and_additive(self):
        """The new counter subdivides; the legacy (tier, risk) counter is intact."""
        from turnstone.core.metrics import MetricsCollector

        collector = MetricsCollector()
        # Same verdict, both counters: neither is a re-partition of the other.
        collector.record_judge_verdict("llm_fallback", "medium", 10)
        collector.record_judge_fallback("superseded_batch")
        collector.record_judge_fallback("superseded_batch")
        collector.record_judge_fallback("provider_error")

        text = collector.generate_text({}, 0)
        assert 'turnstone_judge_verdicts_total{tier="llm_fallback",risk_level="medium"} 1' in text
        assert 'turnstone_judge_fallbacks_total{reason="superseded_batch"} 2' in text
        assert 'turnstone_judge_fallbacks_total{reason="provider_error"} 1' in text

    def test_label_set_is_bounded(self):
        """Free text can never become a metric label."""
        assert fallback_metric_reason(JudgeCancelReason.DEADLINE) == "deadline"
        assert fallback_metric_reason("deadline") == "deadline"
        assert (
            fallback_metric_reason("judge cancelled before evaluating this call")
            == UNCLASSIFIED_FALLBACK_REASON
        )
        assert fallback_metric_reason(None) == UNCLASSIFIED_FALLBACK_REASON
        assert fallback_metric_reason(object()) == UNCLASSIFIED_FALLBACK_REASON

    def test_shared_metric_helper_routes_the_cause(self):
        from turnstone.core.session_ui_base import fire_judge_verdict_metric

        class _Recorder:
            def __init__(self) -> None:
                self.verdicts: list[tuple[str, str, int]] = []
                self.fallbacks: list[str] = []

            def record_judge_verdict(self, tier: str, risk: str, latency: int) -> None:
                self.verdicts.append((tier, risk, latency))

            def record_judge_fallback(self, reason: str) -> None:
                self.fallbacks.append(reason)

        recorder = _Recorder()
        fire_judge_verdict_metric(
            recorder,
            {
                "tier": "llm_fallback",
                "risk_level": "medium",
                "latency_ms": 5,
                "fallback_reason": "superseded_batch",
            },
            "llm",
        )
        # A real verdict reports no cause and must not touch the counter.
        fire_judge_verdict_metric(
            recorder,
            {"tier": "llm", "risk_level": "low", "latency_ms": 900, "fallback_reason": ""},
            "llm",
        )
        assert recorder.verdicts == [("llm_fallback", "medium", 5), ("llm", "low", 900)]
        assert recorder.fallbacks == ["superseded_batch"]

    def test_console_collector_mirrors_the_series(self):
        from turnstone.console.metrics import ConsoleMetrics

        collector = ConsoleMetrics()
        collector.record_judge_verdict("llm_fallback", "medium", 0)
        collector.record_judge_fallback("deadline")
        text = collector.generate_text()
        assert 'turnstone_judge_fallbacks_total{reason="deadline"} 1' in text
        assert 'turnstone_judge_verdicts_total{tier="llm_fallback",risk_level="medium"} 1' in text


class TestEndToEndThroughTheJudge:
    def test_superseded_batch_produces_durable_cause_and_batch_id(self):
        """The pinned reason flows from the judge into the verdict dict."""
        provider = _make_mock_provider('{"recommendation":"approve"}')
        judge = _make_judge(provider)
        cancel = threading.Event()
        _pin_judge_cancel(cancel, JudgeCancelReason.SUPERSEDED_BATCH)
        cancel.set()

        delivered: list[dict] = []
        done = threading.Event()

        def _cb(verdict) -> None:
            delivered.append(verdict.to_dict())

        judge.evaluate(
            [_make_item(call_id=f"tc_{i}") for i in range(3)],
            [{"role": "user", "content": "test"}],
            _cb,
            cancel_event=cancel,
            done_callback=done.set,
        )
        assert done.wait(5.0)
        assert len(delivered) == 3
        assert {v["fallback_reason"] for v in delivered} == {"superseded_batch"}
        assert len({v["batch_id"] for v in delivered}) == 1
        assert all(v["batch_id"] for v in delivered)


@pytest.fixture(autouse=True)
def _clean_storage():
    """Ensure a leftover registry backend never leaks between durability tests."""
    from turnstone.core.storage import reset_storage

    reset_storage()
    yield
    reset_storage()
