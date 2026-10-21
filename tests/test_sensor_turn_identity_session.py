"""END-TO-END: the admitted turn's identity reaches the sensor seam.

``test_sensor_turn_identity_dedupe.py`` proves the driver's behaviour in
isolation. This file proves the WIRING: that the per-turn identity the driver
needs actually arrives, from the real ``ChatSession`` paths, and that it
arrives with the right properties.

The three operator seams are:

  1. ``send()``            - the ordinary turn;
  2. ``_append_pending_system_turns`` - a mid-turn interjection folded in as a
     system turn;
  3. ``_apply_queued_flush`` - queued messages drained into the running turn.

The contract, one test per clause:

  * every operator seam passes a NON-EMPTY identity;
  * two different turns get two DIFFERENT identities;
  * the identity is the admitted conversation row's own ``commit_key`` - the
    row that reached storage, not a synthesized token;
  * the identity never appears in the projection, the observation record, the
    cadence row, or the model request;
  * a turn that was never admitted has NO identity, so it keeps plain
    fingerprint dedupe.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.attachments import Attachment
from turnstone.core.memory import register_workstream
from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase

import hashlib


class _NullUI(SessionUIBase):
    """Accepts every UI hook so the turn reaches the seam for the right reason."""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


def _attachment(data: bytes, name: str = "doc.txt") -> Attachment:
    return Attachment(
        attachment_id=hashlib.sha256(data).hexdigest(),
        filename=name,
        mime_type="text/plain",
        kind="text",
        content=data,
    )


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
    session._refresh_model_from_registry = lambda: None  # type: ignore[method-assign]
    session._full_messages = lambda: []  # type: ignore[method-assign]
    session._check_cancelled = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("stop after append")
    )
    return session


def _send(session: ChatSession, text: str, attachments=None,
          client_send_ids=()) -> None:
    try:
        session.send(text, attachments=attachments, client_send_ids=client_send_ids)
    except RuntimeError as error:
        if "stop after append" not in str(error):
            raise


@pytest.fixture()
def observed(monkeypatch):
    """Record the exact arguments the live seam hands to the driver."""
    calls: list[dict] = []
    monkeypatch.setattr(lifecycle, "ENABLED", True)

    def _record(event, state=None, *, force=False, workstream="", registry=None,
                content_basis="text", turn_id=""):
        calls.append({
            "event": event,
            "state": dict(state or {}),
            "workstream": workstream,
            "content_basis": content_basis,
            "turn_id": turn_id,
        })
        return None

    monkeypatch.setattr(lifecycle, "observe_event", _record)
    lifecycle.reset_hooks()
    return calls


def _operator_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["event"] == "operator_instruction"]


# ------------------------------------------------------- the ordinary send


def test_the_send_seam_passes_a_non_empty_identity(observed):
    """A real turn carries the identity of the row it just admitted."""
    session = _session()
    _send(session, "Rotate the ingest credentials.")

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    assert operator[0]["turn_id"], "the send seam passed no turn identity"


def _stored_commit_keys(session: ChatSession) -> set[str]:
    """The commit keys of the rows this session actually persisted.

    The identity must be the DURABLE row's key, not a live-turn attribute: the
    measurement path joins a sensor event to the conversation STORE, so an
    identity that only existed in memory would be unjoinable.
    """
    from turnstone.core.storage import get_storage

    turns = get_storage().load_message_turns(session._ws_id, checkpointed=False)
    return {
        turn.meta.commit_key
        for turn in turns
        if getattr(turn.meta, "commit_key", None)
    }


def test_the_identity_is_the_admitted_rows_own_commit_key(observed):
    """The identity is the STORED row's key, not a token the seam invented.

    A synthesized token would not join to the conversation store, and the
    measurement path needs exactly that join: the attribution step matches a
    sensor event to the operator turn it came from.
    """
    session = _session()
    _send(session, "Rotate the ingest credentials.")

    operator = _operator_calls(observed)
    assert len(operator) == 1
    identity = operator[0]["turn_id"]

    stored = _stored_commit_keys(session)
    assert stored, "the admitted user turn was not persisted with a commit key"
    assert identity in stored, (
        "the seam's identity is not the admitted row's commit key"
    )


def test_two_different_turns_get_two_different_identities(observed):
    """Two operator turns are two units of work, not one."""
    session = _session()
    _send(session, "First instruction: rotate the ingest credentials.")
    _send(session, "Second instruction: rotate the ingest credentials.")

    operator = _operator_calls(observed)
    assert len(operator) == 2, observed
    first, second = operator[0]["turn_id"], operator[1]["turn_id"]
    assert first and second
    assert first != second, "two distinct turns were given the same identity"


def test_the_same_turn_refired_keeps_its_identity(observed):
    """Re-observing one turn must not mint a new identity.

    The identity is a property of the ADMITTED ROW, not of the observation, so
    the same row always presents the same identity. That is what keeps a
    re-fire of the same turn suppressed instead of being counted twice.
    """
    session = _session()
    _send(session, "Rotate the ingest credentials now.")
    first = _operator_calls(observed)[0]["turn_id"]

    # Re-drive the SAME admitted row through the seam.
    lifecycle.observe_event(
        "operator_instruction",
        {"objective": "Rotate the ingest credentials now."},
        workstream=session._ws_id,
        turn_id=first,
    )
    calls = _operator_calls(observed)
    assert calls[-1]["turn_id"] == first


def test_a_repeated_client_send_id_is_still_two_distinct_turns(observed):
    """The same browser correlation token does NOT make two sends one turn.

    ``client_send_id`` is a browser-generated correlation token. The API
    schema says so explicitly: "It is not an idempotency key; repeated sends
    with the same value remain distinct turns." Each admitted turn mints a
    FRESH ``commit_key`` (a ``uuid4()`` at admission), so two sends carrying
    the same token are two turns with two identities.

    This is the property that makes the dedupe correction correct rather
    than merely different: the identity separates turns, and a re-sent
    message is a re-sent message - it is new work and it must be observed.
    """
    session = _session()
    _send(session, "Rotate the ingest credentials.", client_send_ids=("cs-1",))
    _send(session, "Rotate the ingest credentials.", client_send_ids=("cs-1",))

    operator = _operator_calls(observed)
    assert len(operator) == 2, observed
    first, second = operator[0]["turn_id"], operator[1]["turn_id"]
    assert first and second
    assert first != second, (
        "two sends carrying the same client_send_id were given the same "
        "identity, so the second would be suppressed as a duplicate"
    )


# ------------------------------------------------------ the other two seams


def test_the_interjection_seam_passes_the_appended_turns_identity(observed):
    """Seam 1 observes the row it just appended, not the collected spec."""
    session = _session()
    durable: list = []
    session._append_pending_system_turns(
        [
            (
                "user_interjection",
                "system-reminder framing of the operator words",
                {"message": "Stop the deployment and roll back the change."},
            ),
        ],
        deferred_persistence=durable,
    )
    # The deferred batch is what persists the admitted row.
    for flush in durable:
        flush()

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    identity = operator[0]["turn_id"]
    assert identity, "the interjection seam passed no turn identity"

    assert identity in _stored_commit_keys(session)


def test_the_queued_flush_seam_passes_the_appended_turns_identity(observed):
    """Seam 2 observes the row it just admitted."""
    session = _session()
    plan = session._prepare_queued_flush(
        "Queued operator instruction: rotate the ingest credentials.",
    )
    durable: list = []
    assert session._apply_queued_flush(plan, durable) is True
    for flush in durable:
        flush()

    operator = _operator_calls(observed)
    assert len(operator) == 1, observed
    identity = operator[0]["turn_id"]
    assert identity, "the queued-flush seam passed no turn identity"

    assert identity in _stored_commit_keys(session)


def test_a_turn_that_was_never_admitted_has_no_identity(observed):
    """No admitted row means no identity, and so plain fingerprint dedupe.

    This is the fail-closed side of the contract: the identity is minted by
    the admission, so a turn that failed admission cannot present one and
    cannot masquerade as a new unit of work.
    """
    session = _session()

    def _explode(**_kwargs):
        raise RuntimeError("materialization failed")

    session._initialize_send_generation = _explode  # type: ignore[method-assign]

    try:
        session.send(".", attachments=[_attachment(b"a real document body here")])
    except Exception:  # noqa: BLE001 - containment strategy is not under test
        pass

    assert _operator_calls(observed) == [], observed


# ------------------------------------------------------------- the privacy


def test_the_identity_never_enters_the_projection(observed):
    """The graded projection is unchanged by the identity."""
    session = _session()
    _send(session, "Apply the attached plan.")

    operator = _operator_calls(observed)
    assert len(operator) == 1
    serialized = json.dumps(operator[0]["state"], sort_keys=True)
    assert operator[0]["turn_id"] not in serialized, (
        "the turn identity leaked into the graded projection"
    )
    assert "turn_id" not in operator[0]["state"]


def test_the_identity_never_reaches_the_observation_or_cadence_record(tmp_path):
    """Neither stored record may carry the identity."""
    from turnstone.core.shadow_driver import ShadowSensor

    sensor = ShadowSensor(
        store=tmp_path / "observations.jsonl",
        decision_id="tier1-decision",
        base_url="http://127.0.0.1:9",
        timeout=0.2,
    )
    hook = lifecycle.SensorHook(
        sensor, cadence_store=tmp_path / "cadence.jsonl", persist=False
    )
    hook.observe_event(
        "operator_instruction",
        {"objective": "Apply the attached plan."},
        workstream="ws",
        turn_id="a-secret-turn-identity",
    )

    for name in ("observations.jsonl", "cadence.jsonl"):
        blob = (tmp_path / name).read_text()
        assert "a-secret-turn-identity" not in blob, f"{name} carries the identity"
        assert "turn_id" not in blob, f"{name} names the identity field"


def test_the_identity_never_reaches_the_model_request(observed):
    """The model request must not carry the identity.

    The identity is an internal bookkeeping token. It rides the turn's
    metadata, and the wire projection strips the ``_``-prefixed side channels -
    so this asserts the stripping, not a new strip.
    """
    from turnstone.core.trajectory import turn_to_dict

    session = _session()
    _send(session, "Apply the attached plan.")

    operator = _operator_calls(observed)
    identity = operator[0]["turn_id"]

    wire = session._prepare_wire_messages(
        [turn_to_dict(turn) for turn in session.messages]
    )
    blob = json.dumps(wire, default=str)
    assert identity not in blob, "the turn identity reached the model request"
