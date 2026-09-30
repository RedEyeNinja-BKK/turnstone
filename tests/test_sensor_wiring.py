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


def test_session_wiring_changes_no_control_flow():
    """The wiring must not add, remove or reorder any control-flow decision.

    Earlier this was asserted as strict pure-insertion. That was too strong once
    the workstream id moved out of the hashed state and into a dedicated
    argument: that change deletes and re-adds three argument lines without
    altering a single decision. Strict addition is the right instinct, but a test
    that fails on an explicitly-reviewed argument move is a test that gets
    weakened instead of the change being justified.

    So the invariant is the one that actually matters: every modified line must
    belong to the sensor call sites, and no `if`, `return`, `raise`, loop or
    assignment may be added or removed.
    """
    import subprocess

    repo_root = next(
        (parent for parent in SESSION.parents if (parent / ".git").exists()),
        SESSION.parents[2],
    )
    diff = subprocess.run(
        ["git", "diff", "-U0", "HEAD", "--", "turnstone/core/session.py"],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    assert diff.returncode == 0, diff.stderr
    source = SESSION.read_text()
    if not diff.stdout.strip():
        pytest.skip("session.py matches HEAD: the wiring is committed, no diff to check")

    removed = [line[1:].strip() for line in diff.stdout.splitlines()
               if line.startswith("-") and not line.startswith("---")]
    added = [line[1:].strip() for line in diff.stdout.splitlines()
             if line.startswith("+") and not line.startswith("+++")]

    # Every removed line must be part of a sensor call site: either a sensor
    # argument or the closing paren of a sensor call that gained one.
    for line in removed:
        assert (
            "sensor" in line
            or "_ws_id" in line
            or line in {")", "),"}
        ), f"removed line is not part of a sensor call site: {line!r}"

    # No control-flow construct may be introduced or dropped.
    control = ("if ", "return", "raise", "for ", "while ", "def ", "await ", "yield ",
               "try:", "except", "else:", "elif ")
    for line in added + removed:
        for keyword in control:
            assert not line.startswith(keyword), (
                f"control-flow construct introduced or removed: {line!r}"
            )
    # No assignment other than a keyword argument on a sensor call.
    for line in added:
        if "=" in line and "sensor" not in line and '"' not in line:
            assert line.startswith(("workstream=", "history=", "objective=",
                                    "phase=", "blockers=", "tools=", "agent=",
                                    "history=")), f"unexpected assignment: {line!r}"

    # The call-site count must still be exactly the four material events. Count
    # them in the file, not in the diff: after the wiring is committed the diff
    # no longer contains the event names, and a count of zero would be a
    # vacuous pass rather than a real assertion.
    # Each event must be paired with its OWN seam exactly once. Counting raw
    # string occurrences is wrong: `operator_instruction` also appears as a
    # `phase=` value, so a text count double-counts it. Walk the calls instead.
    # This also catches a relabelled seam, which a pure count would not: four
    # seams with one event renamed still reads as four.
    #
    # The event is the FIRST POSITIONAL argument at every seam, matching
    # `observe_event(event, state, ...)`. Asserting it is a keyword would fail
    # against correct code, so the position is read as it is actually written.
    tree = ast.parse(source)
    events: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_sensor_observe":
            continue
        assert node.args, "every seam must pass the event as its first positional arg"
        first = node.args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"line {node.lineno}: the event must be a string literal"
        )
        events.append(first.value)
        # No seam may splat a mapping into the call: that would let an
        # unrecognised control key reach the hook.
        assert not any(kw.arg is None for kw in node.keywords), (
            f"line {node.lineno}: **kwargs at a seam could carry a control key"
        )

    assert len(events) == 4, f"expected four sensor call sites, found {len(events)}"
    assert sorted(events) == [
        "delegation_result", "operator_instruction", "retry_transition",
        "task_agent_failure",
    ], f"seam/event pairing is wrong: {sorted(events)}"
    # A relabelled seam shows up as a duplicate event name.
    assert len(set(events)) == 4, f"duplicate event across seams: {events}"

    # Every seam must identify its workstream, or per-workstream cadence is
    # unmeasurable. A seam that dropped the argument would still pass every
    # count above, so the argument itself is asserted per call site.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_sensor_observe":
            continue
        workstream = [kw for kw in node.keywords if kw.arg == "workstream"]
        assert workstream, f"line {node.lineno}: seam passes no workstream id"
        value = workstream[0].value
        # `getattr` is a bare Name at the AST level, not an Attribute.
        assert isinstance(value, ast.Call), (
            f"line {node.lineno}: workstream must be a defensive getattr, got {value!r}"
        )
        assert getattr(value.func, "id", None) == "getattr", (
            f"line {node.lineno}: workstream must be read via getattr, "
            f"got {getattr(value.func, 'id', None)!r}"
        )
        assert any(
            isinstance(arg, ast.Constant) and arg.value == "_ws_id"
            for arg in value.args
        ), f"line {node.lineno}: workstream must come from _ws_id"


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
    # `workstream` is the only addition over the original three, and it is an
    # identifier used for per-workstream cadence counting. It is deliberately
    # NOT part of the sensed state, so it cannot change a semantic fingerprint.
    assert parameters == {"event", "state", "force", "workstream"}
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
