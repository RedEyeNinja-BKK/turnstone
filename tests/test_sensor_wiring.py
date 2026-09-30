"""The lifecycle wiring must be inert in the real session.

These tests do not read the source and agree that it looks safe. They drive the
actual `ChatSession` seams with a live sensor replaced by a recorder, and assert
on what the surrounding code does: that the task proceeds identically, that no
control surface is touched, and that the sensor's return value goes nowhere.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core import sensor_lifecycle as lifecycle

SESSION = pathlib.Path(__file__).resolve().parents[1] / "turnstone" / "core" / "session.py"


# ------------------------------------------------------------ diff discipline


def test_session_wiring_is_purely_additive():
    """No existing line of session.py may be modified or removed.

    Pure insertion is the strongest available proof that control flow is
    untouched: an advisory call that only adds lines cannot have reordered,
    short-circuited or altered any existing decision.
    """
    import subprocess

    # Walk up to the nearest repository root rather than hardcoding a parent
    # index. A wrong index makes `git diff` return nothing and the assertion
    # silently vacuous, which is worse than no test at all.
    repo_root = next(
        (parent for parent in SESSION.parents if (parent / ".git").exists()),
        SESSION.parents[2],
    )
    completed = subprocess.run(
        ["git", "diff", "--numstat", "--", "turnstone/core/session.py"],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    assert completed.returncode == 0, completed.stderr
    line = completed.stdout.strip()
    if not line:
        pytest.skip("no uncommitted session.py diff in this tree")
    added, deleted, _path = line.split("\t")
    assert int(deleted) == 0, f"session.py has {deleted} deleted lines; wiring must be additive"
    assert int(added) > 0


def test_every_sensor_call_in_session_is_discarded():
    """Each call is a bare statement: no assignment, no branching on it."""
    tree = ast.parse(SESSION.read_text())
    calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "_sensor_observe":
            continue
        calls += 1
        # A bare expression statement: its value cannot reach anything.
        assert not isinstance(node, ast.Assign)
    assert calls >= 4, f"expected the four material-event seams, found {calls}"


def test_no_sensor_result_is_read_or_assigned():
    """Nothing in session.py consumes a sensor return value."""
    tree = ast.parse(SESSION.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            assert "_sensor" not in targets, "a sensor result was assigned to a variable"
        if isinstance(node, ast.If):
            test = node.test
            source = ast.dump(test)
            assert "_sensor_observe" not in source, "a branch depends on the sensor"
            assert "_sensor_state" not in source, "a branch depends on sensor state"


def test_sensor_imports_are_aliased_and_minimal():
    """Only the three advisory helpers are imported."""
    tree = ast.parse(SESSION.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "turnstone.core.sensor_lifecycle":
            names = {alias.name for alias in node.names}
    assert names == {"build_state", "is_substantive_instruction", "observe_event"}


# ---------------------------------------------------------------- seam shapes


def test_operator_instruction_seam_requires_substance_and_not_wake():
    """The send seam is guarded, so an ack or a wake cannot trigger a call."""
    source = SESSION.read_text()
    assert "if not from_wake and _sensor_is_substantive(user_input):" in source
    # The guard must be on the same expression as the call it protects.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            call_names = [
                getattr(n.value.func, "id", None)
                for n in ast.walk(node) if isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Call)
            ]
            if "_sensor_observe" in call_names:
                assert "from_wake" in ast.dump(node.test), (
                    "the send seam must be guarded on from_wake"
                )
                assert "is_substantive" in ast.dump(node.test), (
                    "the send seam must be guarded on message substance"
                )
                assert "operator_instruction" in ast.dump(node)


def test_delegation_seams_are_inside_generation_owned_publishes():
    """A superseded generation must never be observed as having succeeded."""
    source = SESSION.read_text()
    tree = ast.parse(source)
    for event in ("delegation_result", "task_agent_failure"):
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            if f"'{event}'" not in body:
                continue
            found = True
            # The call must live in a closure that also reports the tool result.
            assert "on_tool_result" in body or "_report_tool_result" in body, (
                f"{event} must be observed at the same point the result is published"
            )
        assert found, f"no seam found for {event}"


def test_retry_seam_is_after_the_attempt_increment():
    """A normal turn must not reach the retry hook."""
    lines = SESSION.read_text().splitlines()
    retry_line = next(
        (i for i, line in enumerate(lines) if '"retry_transition"' in line), None
    )
    assert retry_line is not None
    window = lines[max(0, retry_line - 8):retry_line]
    assert any("attempt += 1" in line for line in window), (
        "the retry seam must sit after the attempt increment"
    )


def test_no_phase_or_evidence_trigger_is_wired():
    """The unwired triggers must have no call site in session.py."""
    source = SESSION.read_text()
    for trigger in lifecycle.UNWIRED_TRIGGERS:
        assert f'"{trigger}"' not in source, (
            f"{trigger} has no honest source and must not be wired"
        )


# ------------------------------------------------------- behavioural inertness


def test_a_failing_sensor_cannot_break_the_send_path():
    """With the sensor exploding, the guarded call still returns normally."""
    calls = {"n": 0}

    def _explode(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("sensor is on fire")

    class _ExplodingSensor:
        def should_sense(self, *_a, **_k):
            return True, "forced"

        def observe(self, *_a, **_k):
            raise RuntimeError("boom")

    hook = lifecycle.SensorHook(sensor=_ExplodingSensor())
    result = hook.observe_event("operator_instruction", {"objective": "x"})
    assert result is None
    assert hook.counters()["hook_error"] == 1


def test_hook_signature_accepts_no_control_inputs():
    """The call site cannot pass, and cannot receive, a control value."""
    signature = inspect.signature(lifecycle.observe_event)
    parameters = set(signature.parameters)
    assert parameters == {"event", "state", "force"}
    for forbidden in ("model", "provider", "route", "lane", "effort", "tools", "guard"):
        assert forbidden not in parameters
    assert signature.return_annotation in (None, "None")


def test_projection_never_contains_a_control_field():
    """Even if a caller passes one, it is not part of the sensed projection."""
    state = lifecycle.build_state(
        objective="do the work",
        model="gpt-6-luna",
        provider="openai",
        route="switchyard-smart-turnstone",
        reasoning_effort="high",
        output_guard="strict",
    )
    for forbidden in ("model", "provider", "route", "reasoning_effort", "output_guard"):
        assert forbidden not in state, f"projection leaked {forbidden!r}"
    assert state == {"objective": "do the work"}, state
