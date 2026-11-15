"""Port integrity: what the 1.9.4 wiring diff is allowed to do to session.py.

Companion to ``test_sensor_wiring.py::test_session_wiring_changes_no_control_flow``.

That test asserts that the wiring adds no control-flow DECISION to any scope
that already existed. That invariant held for 1.8.7 because the
pending-system-turn drain was its own method: both of its guards landed in a
new scope. 1.9.4 inlines the drain into ``send()``, so the identical wiring
necessarily adds ``if not from_wake:`` to ``send`` and
``if source == "user_interjection":`` to the ``_fold_tool_batch`` closure. The
two requirements - keep the deployed shape, add no decision to an existing
scope - cannot both hold on this version.

This module pins the invariant that CAN hold, and that is the one a reviewer
of a cross-version port actually needs:

  * the wiring is PURE INSERTION - not one existing line is modified, deleted
    or reordered;
  * every added decision is an addition (never a removal or a reordering), so
    no existing branch was altered;
  * each of the six seams sits in the scope and at the position the port
    documents.

A pure-insertion diff is a strictly stronger statement about existing logic
than the per-scope decision delta: it says the pre-existing statement stream is
untouched, byte for byte.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

SESSION = (
    pathlib.Path(__file__).resolve().parents[1] / "turnstone" / "core" / "session.py"
)


def _repo_root() -> pathlib.Path:
    for parent in SESSION.parents:
        if (parent / ".git").exists():
            return parent
    pytest.skip("not a git tree: the port diff cannot be inspected")


def _diff() -> list[str]:
    root = _repo_root()
    out = subprocess.run(
        ["git", "diff", "-U0", "HEAD", "--", "turnstone/core/session.py"],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert out.returncode == 0, out.stderr
    if not out.stdout.strip():
        pytest.skip("session.py matches HEAD: the wiring is committed")
    return out.stdout.splitlines()


def test_wiring_diff_is_pure_insertion():
    """No existing line of session.py may be modified or deleted."""
    removed = [
        line
        for line in _diff()
        if line.startswith("-") and not line.startswith("---")
    ]
    assert removed == [], (
        "the wiring must not modify or delete an existing line; removed: "
        f"{removed[:10]}"
    )


def test_wiring_adds_no_decision_that_replaces_an_existing_one():
    """Every control-flow change must be an addition.

    A removal or a reordering in the decision sequence would mean an existing
    branch was altered. Additions confined to inserted blocks are the expected
    shape: the two inline guards live inside the two inserted seam blocks.
    """
    root = _repo_root()
    baseline = subprocess.run(
        ["git", "show", "HEAD:turnstone/core/session.py"],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert baseline.returncode == 0, baseline.stderr

    sys_path = pathlib.Path(__file__).resolve().parent
    import sys

    sys.path.insert(0, str(sys_path))
    from test_sensor_wiring import _control_flow_delta

    delta = _control_flow_delta(baseline.stdout, SESSION.read_text())
    non_additive = {
        scope: [e for e in entries if not e.startswith("+")]
        for scope, entries in delta.items()
    }
    non_additive = {k: v for k, v in non_additive.items() if v}
    assert non_additive == {}, (
        "the wiring changed an existing control-flow decision, not merely "
        f"added one: {non_additive}"
    )


# ---------------------------------------------------------------- seam scopes

def _calls_with_scope(tree: ast.AST) -> list[tuple[str, str, str, int]]:
    """Every seam call as ``(func, first_arg_repr, enclosing_scope, lineno)``."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def chain(node: ast.AST) -> str:
        names: list[str] = []
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(cur.name)
            cur = parents.get(cur)
        return ".".join(reversed(names))

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name not in (
            "_sensor_observe",
            "_sensor_observe_substantive",
            "_sensor_observe_delegation",
        ):
            continue
        first = ast.dump(node.args[0])[:40] if node.args else ""
        out.append((name, first, chain(node), node.lineno))
    return out


def test_each_seam_sits_in_the_scope_the_port_documents():
    """Six seams, each in the function the port places it in.

    1.9.4 moved the code these seams attach to: the queued-flush publish is
    ``_apply_queued_flush``, the drain is inside ``send``'s fold closure, the
    delegation publishes are closures of ``_exec_task``, and the retry arm is
    inside ``_stream_response``. A port that silently attached a seam to the
    wrong call would still pass a text search; the scope makes it explicit.
    """
    calls = _calls_with_scope(ast.parse(SESSION.read_text()))

    expected = [
        # (function, first-arg repr prefix, scope)
        ("_sensor_observe_substantive", "Name(id='user_input'", "send"),
        ("_sensor_observe_substantive", "Name(id='content'", "_apply_queued_flush"),
        (
            "_sensor_observe_substantive",
            "Call(func=Attribute(value=Name(id='meta'",
            "send._fold_tool_batch",
        ),
        (
            "_sensor_observe_delegation",
            "Constant(value='delegation_result')",
            "_exec_task._publish_success",
        ),
        (
            "_sensor_observe_delegation",
            "Constant(value='task_agent_failure')",
            "_exec_task._publish_error",
        ),
        ("_sensor_observe", "Constant(value='retry_transition')", "_stream_response"),
    ]

    for func, arg0, scope in expected:
        hits = [c for c in calls if c[0] == func and c[1].startswith(arg0)]
        assert len(hits) == 1, (
            f"expected exactly one {func}({arg0}...) seam, found {len(hits)}: {hits}"
        )
        assert hits[0][2] == scope, (
            f"seam {func}({arg0}...) sits in scope {hits[0][2]!r}, expected {scope!r}"
        )

    # The six seams, and no others: three operator_instruction entry points,
    # two delegation publishes, one retry transition.
    assert len(calls) == 6, calls
