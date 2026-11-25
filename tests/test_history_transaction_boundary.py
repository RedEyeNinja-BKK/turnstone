"""History selection must cut at transaction boundaries, not inside one.

The original incident: Turnstone built a history window that began midway
through a tool transaction, so the request carried a tool result whose
assistant call had been summarized away.  A strict upstream rejects that
(``400 No tool call found for tool output``), and a permissive one answers
from fabricated context.

These tests assert the structural invariant directly on the split index and on
the published history, so they need no provider: the summary text never
matters, only which turns survive verbatim.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests._session_helpers import make_registered_session
from turnstone.core.trajectory import Role, ToolCall, Turn


def _user(text: str) -> Turn:
    return Turn.user(text)


def _call(call_id: str, name: str = "lookup", arguments: str = "{}") -> Turn:
    return Turn.assistant("", tool_calls=(ToolCall(call_id, name, arguments),))


def _result(call_id: str, output: str = "ok") -> Turn:
    return Turn.tool(call_id, output)


def _transaction(call_id: str) -> list[Turn]:
    return [_call(call_id), _result(call_id)]


def _stray_results(turns) -> list[str]:
    """Return tool_call_ids whose declaring call is absent from *turns*."""
    declared = {
        call.id
        for turn in turns
        for call in getattr(turn, "tool_calls", ()) or ()
    }
    return [
        turn.tool_call_id
        for turn in turns
        if getattr(turn, "tool_call_id", None) and turn.tool_call_id not in declared
    ]


@pytest.fixture
def session(tmp_db, mock_openai_client):
    # Registered, so each compaction's checkpoint row lands: an unpublished
    # workstream id would latch "workstream gone" on the first ephemeral save
    # and refuse every later compaction in the tail-size loop.
    session = make_registered_session()
    session.auto_compact_pct = 0.5
    return session


def test_split_snaps_back_to_the_transaction_start(session):
    """A cut landing on a tool result moves back to its assistant call."""
    session.messages = [
        _user("go"),
        *_transaction("call_1"),
        _user("again"),
        *_transaction("call_2"),
    ]
    # A split at index 2 keeps call_1's result but summarizes its call away, so
    # it moves back to the declaring turn at index 1.
    assert session._preserve_split_index(2) == 1, "cut lands between call and result"
    # An ordinary boundary is left exactly where the caller asked.
    assert session._preserve_split_index(3) == 3
    assert session._preserve_split_index(0) == 0
    assert session._preserve_split_index(10) == len(session.messages)
    # Operator-context system turns ride between a call and its result on the
    # native path: the walk looks through them instead of treating them as a
    # boundary, so a cut on either side still snaps back to the declaring turn.
    session.messages = [
        _user("go"),
        _call("call_3"),
        Turn.system("operator context"),
        _result("call_3"),
    ]
    assert session._preserve_split_index(3) == 1, "cut on the result side"
    assert session._preserve_split_index(2) == 1, "cut on the system turn"


def test_split_keeps_parallel_siblings_together(session):
    """A parallel group is one unit: the cut may not split it either."""
    session.messages = [
        _user("go"),
        Turn.assistant(
            "",
            tool_calls=(
                ToolCall("p1", "lookup", "{}"),
                ToolCall("p2", "lookup", "{}"),
                ToolCall("p3", "lookup", "{}"),
            ),
        ),
        _result("p1"),
        _result("p2"),
        _result("p3"),
        _user("next"),
    ]
    for naive in range(1, 6):
        split = session._preserve_split_index(len(session.messages) - naive)
        declared = {
            call.id
            for turn in session.messages[split:]
            for call in turn.tool_calls
        }
        kept = {t.tool_call_id for t in session.messages[split:] if t.tool_call_id}
        assert kept <= declared, f"naive {naive} kept a result with no call"
    # Splits that land inside the group move back to the declaring turn.
    for naive in (2, 3, 4):
        assert session._preserve_split_index(len(session.messages) - naive) == 1
    # A split after the whole group is already a boundary.
    assert session._preserve_split_index(len(session.messages) - 1) == 5


def test_compaction_never_publishes_a_result_without_its_call(session):
    """The published history after a mid-transaction cut carries both halves."""
    session.messages = [
        _user("first"),
        *_transaction("call_a"),
        _user("second"),
        *_transaction("call_b"),
    ]
    session._msg_tokens = [1] * len(session.messages)
    with patch.object(
        session,
        "_utility_completion",
        return_value=SimpleNamespace(
            content="DENSE", finish_reason="stop", producer="test-summary-provider"
        ),
    ):
        assert session._compact_messages(auto=True, preserve_tail=3) is True
    assert _stray_results(session.messages) == [], (
        "compaction published a tool result whose call it summarized away"
    )


def test_ordinary_history_cut_is_unchanged(session):
    """Non-tool history keeps the caller's exact boundary."""
    session.messages = [
        _user("one"),
        Turn.assistant("a"),
        _user("two"),
        Turn.assistant("b"),
    ]
    assert session._preserve_split_index(2) == 2
    assert session._preserve_split_index(3) == 3


def test_open_transaction_is_preserved_whole(session):
    """An unfinished transaction (call, no result yet) stays verbatim.

    The split lands after the declaring turn, on the operator-context system
    turn that followed it.  Nothing proves the call will ever be answered, so
    the requesting boundary would summarize the call away and the results
    appended after compaction would arrive orphaned.
    """
    session.messages = [
        _user("go"),
        Turn.assistant("working"),
        _call("open_1"),
        Turn.system("operator context"),
    ]
    assert session._preserve_split_index(3) == 2, (
        "an unanswered call must cross the boundary with the turn declaring it"
    )


def _long_history(transactions: int = 61) -> list[Turn]:
    """The original incident shape: ~200 messages carrying 122 tool calls."""
    turns: list[Turn] = []
    # Shape: 17 plain user turns + 61 assistant turns carrying 122 parallel
    # calls + their 122 results = exactly 200 messages, 122 calls.  Calling
    # turns and results are contiguous 3-turn groups after the first 17.
    for n in range(transactions):
        a, b = f"call_{n:03d}", f"call_{n:03d}b"
        if n < 17:
            turns.append(_user(f"step {n}"))
        turns.append(
            Turn.assistant(
                "",
                tool_calls=(
                    ToolCall(a, "lookup", "{}"),
                    ToolCall(b, "lookup", "{}"),
                ),
            )
        )
        turns.append(_result(a, f"output {n}"))
        turns.append(_result(b, f"output {n}b"))
    assert len(turns) == 200
    return turns


def test_two_hundred_message_history_compacts_at_transaction_boundaries(session):
    """The original 200-message / 122-call fixture never loses a transaction."""
    session.messages = _long_history()
    calls = sum(len(t.tool_calls) for t in session.messages)
    assert len(session.messages) >= 200 and calls == 122

    for preserve_tail in (1, 2, 3, 5, 8, 13):
        session.messages = _long_history()
        session._msg_tokens = [1] * len(session.messages)
        with patch.object(
            session,
            "_utility_completion",
            return_value=SimpleNamespace(
                content="DENSE", finish_reason="stop", producer="test-summary-provider"
            ),
        ):
            assert session._compact_messages(auto=True, preserve_tail=preserve_tail) is True
        assert _stray_results(session.messages) == [], (
            f"preserve_tail={preserve_tail} published a result without its call"
        )
        # Every surviving call still has its result: no call was left dangling
        # into the summarized head either.
        declared = {c.id for t in session.messages for c in t.tool_calls}
        answered = {t.tool_call_id for t in session.messages if t.tool_call_id}
        assert declared == answered, (
            f"preserve_tail={preserve_tail} split a transaction the other way"
        )


def test_outbound_wire_carries_complete_transactions_only(session):
    """The wire projection of a compacted long history has no stray result."""
    session.messages = _long_history()
    session._msg_tokens = [1] * len(session.messages)
    with patch.object(
        session,
        "_utility_completion",
        return_value=SimpleNamespace(
            content="DENSE", finish_reason="stop", producer="test-summary-provider"
        ),
    ):
        assert session._compact_messages(auto=True, preserve_tail=4) is True
    wire = session._prepare_wire_messages(session._full_messages(), caps=None)
    declared = {
        call["id"]
        for message in wire
        for call in message.get("tool_calls") or ()
    }
    stray = [
        message.get("tool_call_id")
        for message in wire
        if message.get("tool_call_id") and message["tool_call_id"] not in declared
    ]
    assert stray == [], f"outbound request carries results with no call: {stray}"
