"""Operator text that arrives MID-TURN must be observed like any other turn.

The gap these pin (measured in production on 2026-10-17, in a workstream
whose id is deliberately not reproduced here - a published workstream id is a
lookup key into the live store):
``_sensor_observe_substantive`` had exactly one call site — inside
``ChatSession.send()``. A message queued while a turn was running is never
sent through ``send()``: it is either folded into a tool-result system turn
(Seam 1) or drained into a trailing USER row by ``_apply_queued_flush``
(Seams 2/3), and the send loop simply continues. Both paths therefore
produced no observation, so a sample of "operator instructions" silently
excluded every mid-turn steering message.

The tests below drive a real ``ChatSession`` with a recorder in place of
``observe_event``; they assert on what the seam does, not on how the source
text reads.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase

SESSION = pathlib.Path(__file__).resolve().parents[1] / "turnstone" / "core" / "session.py"


class _NullUI(SessionUIBase):
    """Minimal UI satisfying the SessionUIBase contract."""


def _registry():
    from turnstone.core.model_registry import ModelConfig, ModelRegistry

    return ModelRegistry(
        models={
            "tier1-decision": ModelConfig(
                alias="tier1-decision",
                base_url="https://switchyard.example.internal",
                api_key="synthetic-not-a-credential",
                model="synthetic-model",
                provider="openrouter",
                capabilities={"supports_typed_decision": True},
            )
        },
        default="tier1-decision",
    )


def _real_session():
    return ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        registry=_registry(),
    )


@pytest.fixture()
def observed(monkeypatch):
    """Record (event, objective) without touching the network or the store."""
    calls: list[tuple[str, str]] = []
    real = lifecycle.observe_event

    def _record(event, state=None, *, force=False, workstream="", registry=None,
                content_basis="text", turn_id=""):
        calls.append((event, str((state or {}).get("objective", ""))))
        return real(event, state, force=force, workstream=workstream,
                    registry=registry, content_basis=content_basis, turn_id=turn_id)

    monkeypatch.setattr(lifecycle, "observe_event", _record)
    # The seam is what is under test here; the enabled gate has its own tests.
    monkeypatch.setattr(lifecycle, "ENABLED", True)
    lifecycle.reset_hooks()
    return calls


# --------------------------------------------------------------- Seams 2/3


def test_queued_flush_emits_one_observation(observed):
    """RED before the seam: a flush returned True and emitted nothing."""
    session = _real_session()
    session.queue_message("Refactor the parser to stream and keep retry intact.")

    assert session.flush_queued_messages() is True

    assert len(observed) == 1, observed
    event, objective = observed[0]
    assert event == "operator_instruction"
    assert "Refactor the parser to stream" in objective


def test_queued_flush_observes_the_combined_turn_once(observed):
    """Two messages folded into ONE user row are one unit of model work."""
    session = _real_session()
    session.queue_message("First: split the writer out of the loop.")
    session.queue_message("Second: keep the retry budget unchanged.")

    assert session.flush_queued_messages() is True

    assert len(observed) == 1, observed
    objective = observed[0][1]
    assert "split the writer" in objective
    assert "retry budget" in objective


def test_content_free_queued_message_emits_nothing(observed):
    """A bare priority marker renders to no turn, so it is no instruction."""
    session = _real_session()
    session.queue_message("!!!")

    assert session.flush_queued_messages() is False

    assert observed == []


def test_non_substantive_queued_message_emits_nothing(observed):
    """'.' is an acknowledgement, not an instruction — same gate as send()."""
    session = _real_session()
    session.queue_message(".")

    assert session.flush_queued_messages() is True  # the row still lands
    assert observed == []


def test_flush_seam_cannot_break_the_flush(observed, monkeypatch):
    """The seam is inside the fail-open boundary."""
    session = _real_session()
    session.queue_message("Do the substantive thing, at length, now.")

    def _explode(*args, **kwargs):
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(lifecycle, "observe_event", _explode)

    assert session.flush_queued_messages() is True


def test_disabled_sensor_does_nothing_on_the_flush_seam(monkeypatch):
    """The enabled gate lives in observe_event; the seam must stay silent."""
    monkeypatch.setattr(lifecycle, "ENABLED", False)
    built: list[object] = []
    real_hook = lifecycle.hook

    def _hook(registry=None):
        built.append(registry)
        return real_hook(registry)

    monkeypatch.setattr(lifecycle, "hook", _hook)

    session = _real_session()
    session.queue_message("A perfectly substantive instruction for the model.")

    assert session.flush_queued_messages() is True
    assert built == [], "a disabled sensor must not even build a hook"


# ----------------------------------------------------------------- Seam 1


def _seam1_call_nodes(tree: ast.AST) -> list[ast.Call]:
    """Every ``_sensor_observe_substantive`` call fed by an interjection meta."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_sensor_observe_substantive":
            continue
        if not node.args:
            continue
        first = node.args[0]
        # The advisory seam observes ``meta.get("message", "")``.
        if (
            isinstance(first, ast.Call)
            and isinstance(first.func, ast.Attribute)
            and first.func.attr == "get"
            and isinstance(first.func.value, ast.Name)
            and first.func.value.id == "meta"
        ):
            found.append(node)
    return found


def _append_system_turn_statements(body: list[ast.stmt]) -> list[int]:
    """Indices of the loop-body statements that call ``_append_system_turn``.

    2026-10-19: the append is now BOUND to a name (``turn_id =
    self._append_system_turn(...)``) so the advisory seam can pass the
    admitted row's per-turn identity to the sensor. The call is therefore an
    ``ast.Assign`` rather than a bare ``ast.Expr``. The invariant this test
    protects - the append happens BEFORE the observation - is unaffected, so
    the shape check accepts both forms.
    """
    indices = []
    for i, stmt in enumerate(body):
        value = None
        if isinstance(stmt, ast.Expr):
            value = stmt.value
        elif isinstance(stmt, ast.Assign):
            value = stmt.value
        if isinstance(value, ast.Call) and getattr(value.func, "attr", None) == "_append_system_turn":
            indices.append(i)
    return indices


def test_advisory_interjection_is_observed_after_the_turn_is_appended():
    """Seam 1 must observe the committed turn, not the collected spec.

    A spec that is built and then lost before ``_append_system_turn`` never
    reached the model; observing at collection time would claim an
    instruction the model never saw. This is a placement assertion because
    driving a full tool-call send loop is not something this suite does.
    """
    tree = ast.parse(SESSION.read_text())
    calls = _seam1_call_nodes(tree)
    assert len(calls) == 1, "the advisory interjection seam is not wired once"

    # Walk enclosing loops: the call must sit in the ``for source, content,
    # meta in pending_system_turns`` loop, AFTER the append.
    target = calls[0]
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if target not in list(ast.walk(node)):
            continue
        body = node.body
        append_indices = _append_system_turn_statements(body)
        assert append_indices, "the interjection append is not in the same loop body"
        append_index = append_indices[0]
        observe_index = next(
            i for i, stmt in enumerate(body) if target in list(ast.walk(stmt))
        )
        assert observe_index > append_index, (
            "the interjection is observed before it is appended"
        )
        # Guarded on the interjection source, so guard findings and nudges
        # in the same loop are not counted as operator instructions.
        guard = body[observe_index]
        assert isinstance(guard, ast.If)
        assert "user_interjection" in ast.dump(guard.test)
        return
    pytest.fail("the advisory interjection seam is not inside a for loop")


def test_send_seam_remains_the_only_user_input_entry_point():
    """The mid-turn seams must not replace or duplicate the send seam."""
    tree = ast.parse(SESSION.read_text())
    user_input_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_sensor_observe_substantive":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id == "user_input":
            user_input_calls += 1
    assert user_input_calls == 1, (
        "send() must observe user_input exactly once; mid-turn seams observe "
        "their own content and must not re-observe the send path"
    )
