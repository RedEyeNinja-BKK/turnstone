"""Behavioural proof on the REAL ``ChatSession.send()`` path, with attachments.

`test_sensor_attachment_coverage.py` drives the seam function and the pure
lifecycle helpers. That is necessary but not sufficient: the question the S6
audit could not answer from unit tests is whether a genuine attachment-bearing
send — the exact thing an operator does when they attach a document and type
"." — reaches the seam at all, exactly once, with the payload judged and none
of it transmitted.

These tests therefore call ``ChatSession.send()`` itself with real
``Attachment`` objects and real storage. The response loop is cut immediately
after the user turn is admitted (the same sentinel
``tests/test_session_attachments.py`` uses), because what is under test is the
ordering and cardinality of the observation, not the model exchange.

The contract, one test per clause:

  * attachment-only instruction  -> exactly one observation
  * text + attachment            -> exactly one observation, not two
  * several attachments          -> one operator-work unit
  * plain text                   -> unchanged
  * empty / unreadable payload   -> the turn still sends, but is NOT counted
  * failed materialization       -> no observation at all
  * from_wake                    -> never counted as operator work
  * privacy                      -> no body, filename, id or mime on the wire

Run against the predecessor overlay, the first three fail: the deployed seam
never sees ``attachments``. That is the red half of the proof.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pytest

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.attachments import Attachment
from turnstone.core.memory import register_workstream
from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase

DOC_BODY = (
    "Rotate the invoice service credentials and redeploy the ingest worker. "
    "The operator's real instruction lives in this document, not in the dots."
)
DOC_FILENAME = "quarterly-secret-plan.txt"
DOC_ID = "ab" * 32


class _NullUI(SessionUIBase):
    """Accepts every UI hook so the turn reaches the seam for the right reason."""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


def _attachment(
    *,
    kind: str = "text",
    data: bytes | None = None,
    name: str = DOC_FILENAME,
    attachment_id: str | None = None,
) -> Attachment:
    """A production-shaped attachment.

    ``attachment_id`` is the sha256 of the bytes, exactly as the upload path
    mints it (the id is a content hash). Fixtures that cheat this alias two
    different documents onto one id and the content-addressed store rejects
    the second write.
    """
    payload = DOC_BODY.encode() if data is None else data
    return Attachment(
        attachment_id=attachment_id or hashlib.sha256(payload).hexdigest(),
        filename=name,
        mime_type="text/plain" if kind == "text" else "application/pdf",
        kind=kind,
        content=payload,
    )


class _ExplodingContent(Attachment):
    """A real attachment whose bytes raise when touched.

    ``Attachment`` is frozen, so the raising property lives on a subclass while
    every other attribute stays the production shape. The seam must treat an
    unreadable payload as "no usable payload", and send() must not care.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "attachment_id", "cd" * 32)
        object.__setattr__(self, "filename", "hostile.pdf")
        object.__setattr__(self, "mime_type", "application/pdf")
        object.__setattr__(self, "kind", "pdf")
        object.__setattr__(self, "_content", b"")

    @property
    def content(self) -> bytes:  # type: ignore[override]
        raise RuntimeError("content unavailable")


def _session() -> ChatSession:
    session = ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=1000,
        tool_timeout=10,
        user_id="u1",
    )
    register_workstream(session._ws_id)
    # Cut the response loop immediately after the user turn is admitted. The
    # observation under test fires between admission and the model call.
    session._refresh_model_from_registry = lambda: None  # type: ignore[method-assign]
    session._full_messages = lambda: []  # type: ignore[method-assign]
    session._check_cancelled = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("stop after append")
    )
    return session


def _send(session: ChatSession, text: str, attachments=None, *, from_wake: bool = False) -> None:
    """Run send() up to the post-admission sentinel."""
    try:
        session.send(text, attachments=attachments, from_wake=from_wake)
    except RuntimeError as error:
        if "stop after append" not in str(error):
            raise


@pytest.fixture()
def observed(monkeypatch):
    """Record the exact arguments the live seam hands to the driver."""
    calls: list[dict] = []
    monkeypatch.setattr(lifecycle, "ENABLED", True)

    def _record(event, state=None, *, force=False, workstream="", registry=None,
                content_basis="text"):
        calls.append({
            "event": event,
            "state": dict(state or {}),
            "workstream": workstream,
            "content_basis": content_basis,
        })
        return None

    monkeypatch.setattr(lifecycle, "observe_event", _record)
    lifecycle.reset_hooks()
    return calls


def _operator_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["event"] == "operator_instruction"]


# ------------------------------------------------------------------ coverage


def test_attachment_only_send_is_observed_exactly_once(observed):
    """The audited case: visible text is ".", the instruction is the document."""
    session = _session()
    _send(session, ".", [_attachment()])

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    assert operator[0]["content_basis"] == "attachments_only"
    assert operator[0]["state"]["phase"] == "operator_instruction"
    assert "count=1" in operator[0]["state"]["attachments"]


def test_text_plus_attachment_is_one_event_not_two(observed):
    session = _session()
    _send(session, "Apply the migration described in the attached plan.",
          [_attachment()])

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    assert operator[0]["content_basis"] == "text_plus_attachments"


def test_several_attachments_remain_one_operator_work_unit(observed):
    session = _session()
    _send(session, ".", [
        _attachment(data=b"First document carrying a real instruction, long enough to count."),
        _attachment(data=b"Second document carrying a real instruction, long enough too.",
                    name="second.txt"),
        _attachment(data=b"Third document carrying a real instruction, also long enough.",
                    name="third.txt"),
    ])

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    assert "count=3" in operator[0]["state"]["attachments"]


def test_duplicate_attachment_bytes_do_not_multiply_events(observed):
    """Two references to the same bytes are still one operator turn."""
    session = _session()
    same = _attachment()
    _send(session, ".", [same, same])

    assert len(_operator_calls(observed)) == 1, observed


def test_plain_text_send_is_unchanged(observed):
    """No behavioural drift on the path that already worked."""
    session = _session()
    _send(session, "Refactor the writer loop and keep the retry budget.")

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    assert operator[0]["content_basis"] == "text"
    assert operator[0]["state"].get("attachments", "") == ""


def test_observation_is_attributed_to_the_sending_workstream(observed):
    """Correlation: the event carries the session's workstream, not a guess."""
    session = _session()
    _send(session, ".", [_attachment()])

    operator = _operator_calls(observed)
    assert len(operator) == 1
    assert operator[0]["workstream"] == session._ws_id


def test_disabled_sensor_does_no_attachment_work_at_all(monkeypatch):
    """The seam must cost nothing when deactivated - including document decoding.

    ``sensor_lifecycle``'s stated invariant is that a disabled seam is a no-op
    that costs nothing. Reading attachment bytes for a feature nobody activated
    would break it on the operator's send() thread, so the gate is asserted
    behaviourally: the inspection function must never be called.
    """
    inspected: list[object] = []
    monkeypatch.setattr(lifecycle, "ENABLED", False)

    def _spy(attachments):
        inspected.append(attachments)
        raise AssertionError("attachment bytes inspected while disabled")

    monkeypatch.setattr(lifecycle, "_attachment_facts", _spy)

    session = _session()
    _send(session, ".", [_attachment()])

    assert inspected == []


def test_from_wake_is_not_operator_work(observed):
    """A delivered wake is the system moving, not the operator instructing."""
    session = _session()
    _send(session, ".", [_attachment()], from_wake=True)

    assert _operator_calls(observed) == [], observed


# ------------------------------------------------------- no false positives


def test_empty_attachment_bytes_are_not_work(observed):
    """The send still succeeds; the turn is simply not substantive."""
    session = _session()
    _send(session, ".", [_attachment(data=b"")])

    assert _operator_calls(observed) == [], observed
    assert session.messages, "the user turn must still have been admitted"


def test_unreadable_attachment_bytes_are_not_work(observed):
    session = _session()
    _send(session, ".", [_attachment(data=b"\xff\xfe\x00\x01not-utf8")])

    assert _operator_calls(observed) == [], observed


def test_unreadable_attachment_fails_the_send_and_counts_nothing(observed):
    """A payload that cannot be read is a failed turn, not an observation.

    The seam sits downstream of materialization on purpose: when the bytes
    cannot even be persisted, the model never receives the turn, so counting it
    as operator work would report work that did not happen. The task still
    fails loudly - sensor fail-open is not task-outcome suppression.
    """
    session = _session()

    with pytest.raises(RuntimeError, match="content unavailable"):
        session.send(".", attachments=[_ExplodingContent()])

    assert _operator_calls(observed) == [], observed


def test_failed_materialization_produces_no_observation(observed, monkeypatch):
    """If the user turn is never admitted, nothing may be counted.

    This is the ordering guarantee: the observation sits AFTER
    ``_initialize_send_generation`` precisely so a turn the model never
    receives cannot be reported as work the model received.
    """
    session = _session()

    def _fail(**_kwargs):
        raise RuntimeError("attachment materialization failed")

    monkeypatch.setattr(session, "_initialize_send_generation", _fail)

    with pytest.raises(RuntimeError, match="materialization failed"):
        session.send(".", attachments=[_attachment()])

    assert _operator_calls(observed) == [], observed


# ------------------------------------------------------------------ privacy


def test_projection_carries_no_document_content_or_identity(observed):
    """The whole serialized projection is checked, not just a few keys."""
    session = _session()
    _send(session, ".", [_attachment()])

    operator = _operator_calls(observed)
    assert len(operator) == 1

    serialized = json.dumps(operator[0]["state"], sort_keys=True)
    for forbidden in (
        DOC_FILENAME,
        DOC_ID,
        "quarterly",
        "Rotate the invoice service",
        "text/plain",
        DOC_BODY[:40],
    ):
        assert forbidden not in serialized, (forbidden, serialized)


def test_projection_key_set_is_exactly_the_contract(observed):
    """Pinned: adding a field is a deliberate contract change, not a drift."""
    session = _session()
    _send(session, ".", [_attachment()])

    state = _operator_calls(observed)[0]["state"]
    projection = lifecycle.attachment_projection([_attachment()], user_input=".")

    assert set(projection) == {
        "count", "kinds", "bytes", "inline_text_verified", "deferred", "basis",
    }
    rendered = state["attachments"]
    assert rendered.startswith("count=1 ")
    assert "filename" not in rendered
    assert "attachment_id" not in rendered
    assert "mime" not in rendered


def test_state_token_budget_still_bounds_the_projection(observed):
    """A huge document must not inflate the serialized state."""
    session = _session()
    _send(session, ".", [_attachment(data=b"instruction " * 200_000)])

    operator = _operator_calls(observed)
    assert len(operator) == 1

    from turnstone.core.bounded_state import STATE_CHAR_BUDGET, serialize_state

    state = lifecycle.build_state(
        objective=operator[0]["state"].get("objective", ""),
        phase=operator[0]["state"].get("phase", ""),
        attachments=operator[0]["state"].get("attachments", ""),
    )
    serialized = serialize_state(state).text
    # A 2.4 MB document must not change the wire size by a byte: the projection
    # is a bucket, and the bounded serializer has the last word.
    assert len(serialized) <= STATE_CHAR_BUDGET
    assert "instruction instruction" not in serialized
