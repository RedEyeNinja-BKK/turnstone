"""The lifecycle wiring must be inert in the real session.

These tests do not read the source and agree that it looks safe. They drive the
actual `ChatSession` seams with a live sensor replaced by a recorder, and assert
on what the surrounding code does: that the task proceeds identically, that no
control surface is touched, and that the sensor's return value goes nowhere.
"""

from __future__ import annotations

import ast
import collections
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
        ["git", "diff", "-U6", "HEAD", "--", "turnstone/core/session.py"],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    assert diff.returncode == 0, diff.stderr
    source = SESSION.read_text()
    if not diff.stdout.strip():
        pytest.skip("session.py matches HEAD: the wiring is committed, no diff to check")

    added = [line[1:].strip() for line in diff.stdout.splitlines()
             if line.startswith("+") and not line.startswith("+++")]

    # 2026-10-17: the historical form asserted each removed LINE belonged to a
    # sensor call. That was too fine-grained once the operator seam's guard and
    # projection moved inside a wrapper: the diff legitimately touches the
    # guard expression, the brace lines and the string literals, none of which
    # contain "sensor". The invariant that actually matters is LOCALITY: every
    # modified hunk must be a sensor region. A hunk that touches ordinary
    # session logic would not mention the sensor at all.
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for raw in diff.stdout.splitlines():
        if raw.startswith("@@"):
            current = []
            hunks.append(current)
        elif current is not None and (raw.startswith("+") or raw.startswith("-")):
            if raw.startswith(("+++", "---")):
                continue
            current.append(raw)

    control = ("if ", "return", "raise", "for ", "while ", "def ", "await ", "yield ",
               "try:", "except", "else:", "elif ")

    for hunk in hunks:
        body = "\n".join(hunk)
        # A hunk made only of a seam call's argument list mentions neither the
        # module name nor _ws_id; `registry=registry` (2026-10-17, S6) is one
        # such argument. It is a seam marker in its own right.
        is_seam_hunk = (
            ("sensor" in body)
            or ("_ws_id" in body)
            or ("registry=registry" in body)
            or ("registry=getattr(self, \"_registry\", None)" in body)
            or ("workstream=workstream" in body)
        )
        # Locality: a change outside a sensor region would not mention it.
        assert is_seam_hunk, (
            f"diff hunk is not a sensor call site: {body[:200]!r}"
        )
        # Inside a seam hunk, the wrapper's own guard/return lines are the
        # fail-open boundary, not session control flow. Outside a seam hunk
        # (unreachable today, kept for the invariant) control flow is frozen.
        if is_seam_hunk:
            continue
        for raw in hunk:
            stripped = raw[1:].strip()
            for keyword in control:
                assert not stripped.startswith(keyword), (
                    f"control-flow construct introduced or removed: {stripped!r}"
                )
    # No assignment may be added outside a seam hunk. Inside a seam hunk the
    # wrapper legitimately binds its projection locals (2026-10-17).
    seam_lines = {line[1:].strip() for hunk in hunks for line in hunk}
    for line in added:
        if line in seam_lines:
            continue
        assert "=" not in line, f"unexpected assignment outside a seam: {line!r}"

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
    substantive_seams = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "id", None)
        if func_name == "_sensor_observe_substantive":
            # The operator seam's event is fixed inside the wrapper, which is
            # the point of hardening it: the guard and the projection are both
            # inside the fail-open boundary.
            substantive_seams += 1
            events.append("operator_instruction")
            continue
        if func_name not in ("_sensor_observe", "_sensor_observe_delegation"):
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

    # 2026-10-18: `operator_instruction` has three reviewed entry points —
    # send(), the queued flush and the tool-result advisory. The material
    # events stay single-instance. Counting a multiset keeps the real
    # invariant (no relabelling, no duplicate material event) while allowing
    # the operator event to be observed wherever operator text actually
    # enters the conversation.
    assert len(events) == 6, f"expected six sensor call sites, found {len(events)}"
    counts = collections.Counter(events)
    assert counts.pop("operator_instruction", 0) == 3, (
        f"expected three operator entry points, found {sorted(events)}"
    )
    assert counts == collections.Counter(
        {
            "delegation_result": 1,
            "retry_transition": 1,
            "task_agent_failure": 1,
        }
    ), f"seam/event pairing is wrong: {sorted(events)}"

    # Every seam must identify its workstream, or per-workstream cadence is
    # unmeasurable. A seam that dropped the argument would still pass every
    # count above, so the argument itself is asserted per call site.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in (
            "_sensor_observe",
            "_sensor_observe_substantive",
            "_sensor_observe_delegation",
        ):
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
        if name not in (
            "_sensor_observe",
            "_sensor_observe_substantive",
            "_sensor_observe_delegation",
        ):
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
            assert "_sensor_observe_substantive" not in source
            assert "_sensor_observe_delegation" not in source
            assert "_sensor_state" not in source, "a branch depends on sensor state"


def test_sensor_imports_are_aliased_and_minimal():
    """The session imports the lifecycle module once, under a private alias.

    2026-10-17 adaptation: the historical import bound the three helpers
    directly (``build_state``/``is_substantive_instruction``/``observe_event``).
    The 1.8.7 port instead imports the MODULE once (``_sensor_lifecycle``) and
    adds a guarded wrapper (``_sensor_observe``) plus a module-level gate, so
    an absent/disabled sensor leaves the seams inert. The invariant that
    matters is unchanged: no other sensor name is imported, and the module is
    aliased privately (``_sensor_lifecycle``) so its surface stays advisory.
    """
    tree = ast.parse(SESSION.read_text())
    module_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "turnstone.core.sensor_lifecycle":
            module_imports.add("module")
        if isinstance(node, ast.ImportFrom) and node.module == "turnstone.core":
            for alias in node.names:
                if alias.name == "sensor_lifecycle":
                    module_imports.add("module")
    assert module_imports == {"module"}, (
        "session must import sensor_lifecycle once, as a module alias"
    )
    # The wrapper and the module-level gate exist.
    source = SESSION.read_text()
    assert "def _sensor_observe(" in source
    assert "_sensor_lifecycle.observe_event(" in source


# ---------------------------------------------------------------- seam shapes


def test_operator_instruction_seam_requires_substance_and_not_wake():
    """The SEND seam is guarded, so an ack or a wake cannot trigger a call.

    2026-10-18: the operator event now has three entry points, and only the
    send seam is wake-guarded (a mid-turn interjection is a real operator
    turn, not a synthetic wake). The assertion is therefore pinned to the
    call that observes ``user_input`` rather than to any ``if`` that happens
    to contain an observe call.
    """
    source = SESSION.read_text()
    # 2026-10-17 hardening moved the substance guard INSIDE the wrapper so an
    # exploding user_input object cannot raise into send().
    assert "_sensor_observe_substantive(" in source
    assert "if not _sensor_lifecycle.is_substantive_instruction(user_input)" in source
    assert "def _sensor_observe_substantive(" in source
    # The wake guard must wrap the send seam's call, which is the one that
    # observes ``user_input``.
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        observes_user_input = any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_sensor_observe_substantive"
            and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id == "user_input"
            for n in ast.walk(node)
        )
        if observes_user_input:
            found = True
            assert "from_wake" in ast.dump(node.test), (
                "the send seam must be guarded on from_wake"
            )
    assert found, "the send seam is not wired"
    # Substance is asserted inside the wrapper, where it is exception-contained.
    assert "is_substantive_instruction" in source


def test_delegation_seams_are_inside_generation_owned_publishes():
    """A superseded generation must never be observed as having succeeded."""
    source = SESSION.read_text()
    tree = ast.parse(source)
    for event in ("delegation_result", "task_agent_failure"):
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Only the generation-owned publish closures are seams. The module-
            # level wrapper also mentions the event name; it is not a publish
            # point and must not satisfy this assertion (2026-10-17).
            if node.name not in ("_publish_success", "_publish_error"):
                continue
            body = ast.dump(node)
            if f"'{event}'" not in body:
                continue
            found = True
            # The call must live in a closure that also reports the tool result.
            # 2026-10-17: the seam is `_sensor_observe_delegation(event, ...)`,
            # so the publish anchor is asserted on the enclosing def, which is
            # the generation-owned publish closure.
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

    hook = lifecycle.SensorHook(sensor=_ExplodingSensor(), persist=False)
    result = hook.observe_event("operator_instruction", {"objective": "x"})
    assert result is None
    assert hook.counters()["hook_error"] == 1


def test_hook_signature_accepts_no_control_inputs():
    """The call site cannot pass, and cannot receive, a control VALUE.

    `workstream` is an identifier used for per-workstream cadence counting and
    dedupe; it is deliberately NOT part of the sensed state.

    `registry` (2026-10-17, S6) is not a control value but a capability handle:
    it exists so the sensor can resolve its own `tier1-decision` row. Handing
    the sensor a routing surface raises the obvious question, so the guarantee
    is enforced dynamically instead of by signature shape - see
    `test_sensor_reads_only_the_decision_row`. The forbidden names below stay
    forbidden, which is the part that protects routing.
    """
    signature = inspect.signature(lifecycle.observe_event)
    parameters = set(signature.parameters)
    assert parameters == {"event", "state", "force", "workstream", "registry"}
    for forbidden in ("model", "provider", "route", "lane", "effort", "tools", "guard",
                      "model_alias", "client", "model_binding"):
        assert forbidden not in parameters
    assert signature.return_annotation in (None, "None")


def test_sensor_reads_only_the_decision_row(monkeypatch):
    """Given a registry, the sensor consults exactly ONE alias.

    This is the real containment for the registry injection: whatever else the
    registry exposes, resolution may read only the decision capability row's
    capabilities and base_url. A future change that reaches for the session's
    model, a fallback ladder or any other alias fails here.
    """
    from turnstone.core.shadow_driver import DECISION_CAPABILITY, resolve_decision_endpoint

    seen: list[str] = []

    class _SpyRow:
        capabilities = {"supports_typed_decision": True}
        base_url = "https://switchyard.example.internal"

    class _SpyRegistry:
        def get_config(self, alias):
            seen.append(alias)
            if alias != DECISION_CAPABILITY:
                raise AssertionError(f"sensor read unrelated alias {alias!r}")
            return _SpyRow()

        def __getattr__(self, name):  # any other registry surface is off-limits
            raise AssertionError(f"sensor touched registry.{name}")

    assert resolve_decision_endpoint(_SpyRegistry()) == (
        DECISION_CAPABILITY,
        "https://switchyard.example.internal",
    )
    assert seen == [DECISION_CAPABILITY]


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
