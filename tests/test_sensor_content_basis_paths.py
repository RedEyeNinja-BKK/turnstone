"""`content_basis` must survive every degraded observation path.

Found by the S6-t2 egress proof (2026-10-18): the driver forwarded
`content_basis` on the success path and on the unresolved-endpoint path, but
three `unavailable()` calls omitted it — budget-refused state, transport
failure, and a response with no `answers` mapping. Each silently recorded
`content_basis="text"`.

Why that mattered: those rows carry no available signals, so they never counted
as graded measurements, and the 100-observation gate was unaffected. But the
reported BASIS MIX was wrong — an attachment-only turn that failed to reach the
backend was recorded as if the operator had written text. The whole point of
the field is to say which material grounded the turn; defaulting it on the
failure paths is the same quiet dishonesty as fabricating a grade.

These tests drive the real `ShadowSensor.observe()` through each path with a
real attachment-shaped state and assert the RECORDED ROW, not the arguments.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from turnstone.core import shadow_driver, shadow_observation
from turnstone.core.shadow_driver import ShadowSensor


class _Recorder:
    """The smallest thing that is a store: captures the row the driver writes."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, path, record) -> None:  # signature of append_jsonl
        self.rows.append(json.loads(json.dumps(record)))


@pytest.fixture()
def sensor(tmp_path, monkeypatch):
    """A resolved sensor writing into a scratch store, with a canned transport."""
    rows = _Recorder()
    monkeypatch.setattr(shadow_driver, "append_jsonl", rows)
    sensor = ShadowSensor(
        store=tmp_path / "observations.jsonl",
        base_url="https://switchyard.example.invalid",
        decision_id="tier1-decision",
        timeout=0.2,
    )
    assert sensor._resolved is True
    sensor.rows = rows.rows  # type: ignore[attr-defined]
    return sensor


def _state() -> dict:
    return {
        "objective": ".",
        "phase": "operator_instruction",
        "attachments": "count=1 kinds=text=1 bytes=<1KB inline_text_verified=1 "
                       "deferred=0 basis=attachments_only",
    }


def _last(sensor) -> dict:
    assert sensor.rows, "the driver recorded nothing"
    return sensor.rows[-1]


# ------------------------------------------------------- success path (control)


def test_success_path_records_the_basis(sensor, monkeypatch):
    """The path that already worked, pinned so a refactor cannot lose it."""
    payload = {
        "answers": {
            name: {"available": True, "value": "moderate"}
            for name in (
                "task_complexity", "reasoning_demand", "tool_dependency",
                "agentic_complexity", "route_sufficiency",
            )
        },
        "backend": "capture",
        "fallback_used": False,
    }
    monkeypatch.setattr(shadow_driver.ShadowSensor, "_post", lambda self, body: (payload, None))

    sensor.observe(_state(), event="operator_instruction", decided=True,
                   workstream="ws", content_basis="attachments_only")

    assert _last(sensor)["content_basis"] == "attachments_only"


# ------------------------------------------------------------- degraded paths


def test_transport_failure_still_records_the_basis(sensor, monkeypatch):
    monkeypatch.setattr(
        shadow_driver.ShadowSensor, "_post",
        lambda self, body: (None, "transport:URLError"),
    )

    sensor.observe(_state(), event="operator_instruction", decided=True,
                   workstream="ws", content_basis="attachments_only")

    row = _last(sensor)
    assert row["reason"] == "transport:URLError"
    assert row["content_basis"] == "attachments_only"


def test_malformed_response_still_records_the_basis(sensor, monkeypatch):
    monkeypatch.setattr(
        shadow_driver.ShadowSensor, "_post",
        lambda self, body: ({"answers": ["not", "a", "mapping"]}, None),
    )

    sensor.observe(_state(), event="operator_instruction", decided=True,
                   workstream="ws", content_basis="attachments_only")

    row = _last(sensor)
    assert row["reason"] == "invalid_response:no_answers"
    assert row["content_basis"] == "attachments_only"


def test_budget_refusal_still_records_the_basis(sensor, monkeypatch):
    """A state the tokenizer refuses never reaches the wire; it still records."""
    class _Refused:
        admitted = False
        reason = "state_over_budget"
        serialized_state_tokens = 999

    monkeypatch.setattr(shadow_driver, "enforce_budget", lambda text: _Refused())
    monkeypatch.setattr(
        shadow_driver.ShadowSensor, "_post",
        lambda self, body: (_ for _ in ()).throw(AssertionError("must not post")),
    )

    sensor.observe(_state(), event="operator_instruction", decided=True,
                   workstream="ws", content_basis="attachments_only")

    row = _last(sensor)
    assert row["reason"] == "state_over_budget"
    assert row["content_basis"] == "attachments_only"


def test_unresolved_endpoint_still_records_the_basis(tmp_path, monkeypatch):
    rows = _Recorder()
    monkeypatch.setattr(shadow_driver, "append_jsonl", rows)
    unresolved = ShadowSensor(store=tmp_path / "o.jsonl", base_url="", decision_id="")
    unresolved.rows = rows.rows  # type: ignore[attr-defined]

    unresolved.observe(_state(), event="operator_instruction", decided=True,
                       workstream="ws", content_basis="attachments_only")

    assert rows.rows[-1]["reason"] == "decision_endpoint_unresolved"
    assert rows.rows[-1]["content_basis"] == "attachments_only"


# ------------------------------------------------------------ the invariant


def test_text_basis_is_still_the_default_when_none_is_supplied(sensor, monkeypatch):
    """Pre-existing callers keep the old behaviour: absent means text."""
    monkeypatch.setattr(
        shadow_driver.ShadowSensor, "_post",
        lambda self, body: (None, "transport:URLError"),
    )

    sensor.observe(_state(), event="operator_instruction", decided=True, workstream="ws")

    assert _last(sensor)["content_basis"] == "text"


def test_every_unavailable_call_forwards_the_basis():
    """Structural guard: a NEW unavailable() call in observe() must pass it too.

    The behavioural tests above cover today's four paths. This one fails if
    someone adds a fifth and forgets, which is exactly how the defect appeared.
    """
    import ast
    import inspect
    import textwrap

    # get_source returns an indented method body; dedent before parsing.
    source = textwrap.dedent(inspect.getsource(shadow_driver.ShadowSensor.observe))
    tree = ast.parse(source)
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "unavailable":
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "content_basis" not in keywords:
            missing.append(node.lineno)
    assert missing == [], (
        f"unavailable() called without content_basis at relative line(s) {missing}"
    )
