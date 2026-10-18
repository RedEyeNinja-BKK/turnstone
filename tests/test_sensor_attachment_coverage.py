"""Attachment-delivered operator work must be observed, without content leaving.

The audit (2026-10-18) found that an operator turn whose visible text is "."
and whose real content is a document attachment produced NO observation: the
send seam passed only ``user_input`` to the sensor, so the substance filter
correctly rejected "." and the attachment was never considered.

These tests drive real ``ChatSession`` methods and real sensor functions. They
assert both halves of the contract:

  * COVERAGE — a genuine attachment-delivered instruction is recognized as work
    and produces exactly one observation;
  * PRIVACY — no attachment body, filename, path, attachment id or excerpt ever
    reaches the sensor projection, and therefore never reaches the endpoint or
    the stored telemetry.

The second half is the one that is easy to get wrong: it is not enough that
the happy path is quiet, so most of these tests assert on the *entire*
serialized projection and on the recorded observation row.
"""

from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.attachments import Attachment
from turnstone.core.session import ChatSession, _sensor_observe_substantive
from turnstone.core.session_ui_base import SessionUIBase

SECRET_DOC = (
    "Deploy the staging cluster and rotate the API key "
    "sk-live-DO-NOT-LEAK-1234567890 for the invoice service. "
    "Attachment filename must never appear: quarterly-secret-plan.txt"
)


class _NullUI(SessionUIBase):
    """A UI that accepts every hook.

    The send() drive reaches state publication, so a UI missing one hook would
    fail for reasons unrelated to the seam under test.
    """

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


def _attachment(kind: str = "text", data: bytes | None = None,
                name: str = "quarterly-secret-plan.txt") -> Attachment:
    if data is None:
        data = SECRET_DOC.encode()
    return Attachment(
        attachment_id="a" * 64,
        filename=name,
        mime_type="text/plain" if kind == "text" else "application/pdf",
        kind=kind,
        content=data,
    )


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


def _session():
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
    """Record every (event, state, content_basis) the seam would transmit."""
    calls: list[dict] = []
    real = lifecycle.observe_event

    def _record(event, state=None, *, force=False, workstream="", registry=None,
                content_basis="text"):
        calls.append({
            "event": event,
            "state": dict(state or {}),
            "workstream": workstream,
            "content_basis": content_basis,
        })
        return real(event, state, force=force, workstream=workstream,
                    registry=registry, content_basis=content_basis)

    monkeypatch.setattr(lifecycle, "observe_event", _record)
    monkeypatch.setattr(lifecycle, "ENABLED", True)
    lifecycle.reset_hooks()
    return calls


def _projected_text(call: dict) -> str:
    """The exact text the serializer would send for one recorded call."""
    state = lifecycle.build_state(
        objective=call["state"].get("objective", ""),
        phase=call["state"].get("phase", ""),
        attachments=call["state"].get("attachments", ""),
    )
    return json.dumps(state, sort_keys=True)


# --------------------------------------------------------------- coverage


def test_attachment_only_instruction_is_substantive():
    """"." plus a substantive text attachment is real operator work."""
    assert lifecycle.is_substantive_instruction(".") is False
    assert lifecycle.is_substantive_instruction(
        ".", attachments=[_attachment()]
    ) is True


def test_attachment_only_turn_produces_exactly_one_observation(observed):
    session = _session()
    session._ws_id = "ws-attach"

    _sensor_observe_substantive(
        ".", workstream="ws-attach", registry=_registry(),
        attachments=[_attachment()],
    )

    assert len(observed) == 1, observed
    assert observed[0]["event"] == "operator_instruction"
    assert observed[0]["content_basis"] == "attachments_only"


def test_text_plus_attachment_is_one_observation_not_two(observed):
    _sensor_observe_substantive(
        "Apply the migration in the attached plan.",
        workstream="ws", registry=_registry(),
        attachments=[_attachment()],
    )

    assert len(observed) == 1, observed
    assert observed[0]["content_basis"] == "text_plus_attachments"


def test_multiple_attachments_are_one_operator_work_unit(observed):
    _sensor_observe_substantive(
        ".",
        workstream="ws", registry=_registry(),
        attachments=[
            _attachment(name="a.txt"),
            _attachment(name="b.txt", data=b"Second document with real content."),
            _attachment(name="c.txt", data=b"Third document with real content."),
        ],
    )

    assert len(observed) == 1, observed
    projection = observed[0]["state"]["attachments"]
    assert "count=3" in projection


def test_plain_text_turn_is_unchanged(observed):
    """The existing text path keeps its behaviour and its basis."""
    _sensor_observe_substantive(
        "Refactor the writer loop and keep the retry budget.",
        workstream="ws", registry=_registry(),
    )

    assert len(observed) == 1
    assert observed[0]["content_basis"] == "text"
    assert observed[0]["state"].get("attachments", "") == ""


# ------------------------------------------------- invalid / empty inputs


def test_empty_attachment_does_not_create_an_observation(observed):
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment(data=b"")],
    )
    assert observed == []


def test_unreadable_text_attachment_does_not_create_an_observation(observed):
    """Bytes that are not UTF-8 cannot become an inline text part."""
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment(data=b"\xff\xfe\x00\x01not-utf8")],
    )
    assert observed == []


def test_trivial_text_attachment_is_not_work(observed):
    """A three-byte note is not an instruction, same threshold as text."""
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment(data=b"hi\n")],
    )
    assert observed == []


def test_acknowledgement_without_attachment_stays_suppressed(observed):
    _sensor_observe_substantive(
        "thanks", workstream="ws", registry=_registry(),
    )
    assert observed == []


def test_failed_send_does_not_reach_the_seam(observed):
    """A raising send must not be recorded as model-visible attachment work."""
    session = _session()

    def _explode(*args, **kwargs):
        raise RuntimeError("admission failed")

    session._initialize_send_generation = _explode  # type: ignore[method-assign]

    # send() contains the failure and records it; it does not re-raise. What
    # matters here is that the seam was never reached.
    try:
        session.send(".", attachments=[_attachment()])
    except Exception:  # noqa: BLE001 - containment strategy is not under test
        pass

    assert observed == []


# ------------------------------------------------------------------ privacy


def test_no_attachment_body_reaches_the_projection(observed):
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment()],
    )

    wire = _projected_text(observed[0])
    assert "sk-live-DO-NOT-LEAK-1234567890" not in wire
    assert "quarterly-secret-plan" not in wire
    assert "Deploy the staging cluster" not in wire
    # The attachment id is a content hash and must not be transmitted either.
    assert "a" * 64 not in wire


def test_no_filename_or_path_reaches_the_projection(observed):
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment(name="/etc/secrets/prod.env")],
    )

    wire = _projected_text(observed[0])
    assert "prod.env" not in wire
    assert "/etc/secrets" not in wire
    assert "mime" not in wire.casefold()


def test_projection_is_bounded(observed):
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment(data=SECRET_DOC.encode() * 200)],
    )

    projection = observed[0]["state"]["attachments"]
    assert len(projection) <= 120, projection
    # Size is bucketed, never exact.
    assert str(len(SECRET_DOC.encode()) * 200) not in projection


# ------------------------------------------------------- structural facts


def test_projection_dict_carries_only_the_reviewed_keys():
    """A new field here is a privacy decision, not an implementation detail.

    Pinned as an exact set so adding one requires deliberately updating this
    test and justifying it. Filenames, paths, mime types, ids and bodies are
    all absent by construction.
    """
    projection = lifecycle.attachment_projection(
        [_attachment(), _attachment(kind="pdf", data=b"%PDF" + b"x" * 300)],
        user_input=".",
    )
    assert set(projection) == {
        "count", "kinds", "bytes", "inline_text_verified", "deferred", "basis",
    }


def test_rendered_state_carries_only_the_reviewed_keys():
    projection = lifecycle.attachment_projection([_attachment()], user_input=".")
    rendered = lifecycle.render_attachment_state(projection)
    keys = {part.split("=", 1)[0] for part in rendered.split()}
    assert keys == {
        "count", "kinds", "bytes", "inline_text_verified", "deferred", "basis",
    }


def test_projection_records_kind_and_verification():
    projection = lifecycle.attachment_projection([_attachment()])
    assert projection["count"] == 1
    assert projection["kinds"] == "text=1"
    assert projection["inline_text_verified"] == 1
    assert projection["deferred"] == 0


def test_binary_kind_is_deferred_not_claimed_verified():
    """A PDF's wire form depends on the target caps; never claim it verified."""
    projection = lifecycle.attachment_projection(
        [_attachment(kind="pdf", data=b"%PDF-1.7 objective stream" * 20)]
    )
    assert projection["deferred"] == 1
    assert projection["inline_text_verified"] == 0


def test_attachment_basis_is_reported_on_the_state(observed):
    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment()],
    )
    projection = observed[0]["state"]["attachments"]
    assert "basis=attachments_only" in projection
    assert "count=1" in projection


# ------------------------------------------------------------- containment


def test_exploding_attachment_object_cannot_break_send(observed):
    """The seam is fail-open against a hostile attachment object."""
    class _Hostile:
        kind = "text"
        filename = "x.txt"

        @property
        def content(self):  # pragma: no cover - must not be reached unguarded
            raise RuntimeError("boom")

    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_Hostile()],
    )
    # No raise, and nothing falsely admitted.
    assert observed == []


def test_sensor_failure_does_not_affect_attachment_handling(observed, monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(lifecycle, "observe_event", _explode)

    _sensor_observe_substantive(
        ".", workstream="ws", registry=_registry(),
        attachments=[_attachment()],
    )


def test_no_control_authority_is_added():
    """The attachment metadata must not smuggle a control field."""
    projection = lifecycle.attachment_projection([
        _attachment(), _attachment(kind="pdf", data=b"%PDF" + b"x" * 200)
    ])
    forbidden = {
        "model", "provider", "route", "reasoning_effort", "output_guard",
        "permissions", "approval", "temperature", "tools", "tool_choice",
        "max_tokens", "system_prompt", "api_key", "base_url",
    }
    assert not (set(projection) & forbidden)
    for value in projection.values():
        assert not any(f in str(value) for f in forbidden)


# ------------------------------------------------- integration through send()


def test_send_forwards_attachments_to_the_seam(observed, monkeypatch):
    """The production entry point must pass attachments, not just the text.

    This is the defect the audit found: send() called the seam with
    ``user_input`` only, so a document-delivered instruction was invisible.
    Driving send() itself (rather than the helper) is what proves the wiring.
    """
    session = _session()

    # The seam is reached before the model loop; a client that fails afterwards
    # cannot hide that. Swallow whatever the downstream path does.
    try:
        session.send(".", attachments=[_attachment()])
    except Exception:  # noqa: BLE001 - only the seam call is under test
        pass

    assert observed, "send() did not reach the sensor seam at all"
    assert observed[0]["event"] == "operator_instruction"
    assert observed[0]["content_basis"] == "attachments_only"
    assert "count=1" in observed[0]["state"]["attachments"]


def test_send_without_attachments_keeps_text_basis(observed):
    session = _session()
    try:
        session.send("Reconcile the ledger against the attached statement.")
    except Exception:  # noqa: BLE001
        pass

    assert observed, "send() did not reach the sensor seam at all"
    assert observed[0]["content_basis"] == "text"
    assert observed[0]["state"].get("attachments", "") == ""
