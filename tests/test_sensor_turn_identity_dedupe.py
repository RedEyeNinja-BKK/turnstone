"""A per-turn identity in the DEDUPE KEY, never in the graded projection.

The defect this pins (measured live on 2026-10-19, epoch s6-t2):

``ShadowSensor.should_sense()`` dedupes on ``state_fingerprint(state)``, and
the fingerprint hashes the BOUNDED PROJECTION. For an operator turn whose
instruction lives in an attachment, the only attachment-derived field in the
projection is the bounded descriptor::

    count=1 kinds=text=1 bytes=1-4KB inline_text_verified=1 deferred=0 basis=attachments_only

Two DIFFERENT documents that land in the same size bucket produce a
byte-identical descriptor, so their fingerprints are identical, and the second
turn is suppressed as ``unchanged_state``. Live proof: two pairs of store rows
carried 2427 B and 2654 B attachments (same bucket) and 8885 B and 11777 B
attachments (same bucket) respectively, and each pair was collapsed into one
event. All four rows had distinct ``client_send_ids``, so they were four
genuinely different operator turns, not redeliveries. The conversation ids
are not reproduced here: a published conversation id is a lookup key into the
live store.

The fix threads the admitted conversation row's ``commit_key`` — a per-turn
idempotency identity that already exists in the production path — into the
DEDUPE KEY only:

  * two distinct turns with identical projections are BOTH admitted;
  * a re-fire of the SAME turn identity is still suppressed (the same
    admitted row, observed again);
  * an event with no turn identity keeps plain fingerprint dedupe, so the
    pinned no-retry-storm contract is unchanged.

Note on the second point: it is a re-fire of the same IDENTITY, not a
browser re-send. ``commit_key`` is generated per admitted turn
(``uuid4()``), and the API's own schema states that ``client_send_id`` "is
not an idempotency key; repeated sends with the same value remain distinct
turns" — so a re-sent message gets a new commit key and is admitted as new
work. The session-level file
(``tests/test_sensor_turn_identity_session.py``) pins that at the seam
that matters. The dedupe state is also a single slot, not a set:
suppression is CONSECUTIVE-ONLY, so the same identity returning after an
intervening event is admitted again. Both are pre-existing contracts this
change did not move; see ``docs/design/s6-dedupe-identity.md`` §5.

And the identity must never leak: it is not part of the graded projection, so
the grader receives exactly the bytes it received before.
"""

from __future__ import annotations

import json

from turnstone.core import sensor_lifecycle as lifecycle
from turnstone.core.shadow_driver import ShadowSensor
from turnstone.core.shadow_observation import Trigger


# ------------------------------------------------------- the collision itself


def _attachment_state(bucket: str = "1-4KB") -> dict[str, str]:
    """The bounded projection of an attachment-only operator turn.

    Byte-identical for any two documents in the same size bucket - which is
    exactly why it is a bad dedupe key.
    """
    return {
        "objective": ".",
        "phase": "operator_instruction",
        "attachments": (
            f"count=1 kinds=text=1 bytes={bucket} "
            "inline_text_verified=1 deferred=0 basis=attachments_only"
        ),
    }


def test_two_distinct_turns_with_identical_descriptors_are_both_admitted():
    """RED against 5427ded: the second turn was suppressed as unchanged_state.

    This is the production defect, reproduced with the exact projection the
    live sensor produced for the two collapsed workstreams.
    """
    sensor = ShadowSensor(store=None)

    first, why_first = sensor.should_sense(
        _attachment_state(),
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-a",
    )
    second, why_second = sensor.should_sense(
        _attachment_state(),
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-b",
    )

    assert (first, why_first) == (True, Trigger.INITIAL.value)
    assert second is True, (
        "a DISTINCT turn must be observed even when its projection is "
        f"identical to the previous turn's; got {why_second!r}"
    )


def test_the_same_turn_refired_is_still_suppressed():
    """The identity must not become a licence to re-observe one turn forever."""
    sensor = ShadowSensor(store=None)

    first, _ = sensor.should_sense(
        _attachment_state(),
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-a",
    )
    second, why_second = sensor.should_sense(
        _attachment_state(),
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-a",
    )

    assert first is True
    assert (second, why_second) == (False, "unchanged_state"), (
        f"the same turn re-fired must stay suppressed; got {second!r}"
    )


def test_suppression_is_consecutive_only():
    """The dedupe state is a single slot, so A-B-A is admitted again.

    The state holds the workstream's LAST admitted key, not a set of seen
    keys. The same identity returning after an intervening event is
    therefore admitted, even when the projection is byte-identical
    throughout - the projection alone cannot suppress it either, because
    the intervening event reset the slot.

    This is the pre-existing contract, unchanged by the turn-identity work,
    and it is pinned here so a future change to a set-valued state is a
    deliberate decision rather than an accident.
    """
    sensor = ShadowSensor(store=None)

    outcomes = [
        sensor.should_sense(
            _attachment_state(),
            event="operator_instruction",
            workstream="ws",
            turn_id=turn_id,
        )
        for turn_id in ("turn-a", "turn-b", "turn-a")
    ]

    assert [allowed for allowed, _ in outcomes] == [True, True, True], (
        "A-B-A must be admitted: the dedupe state is a single slot, not a "
        f"set; got {outcomes!r}"
    )


def test_a_turn_without_an_identity_keeps_fingerprint_dedupe():
    """The no-retry-storm contract is pinned and must not move.

    Events with no turn identity (delegation results, retry transitions, and
    every direct ``should_sense`` caller) must keep plain fingerprint dedupe.
    """
    sensor = ShadowSensor(store=None)
    state = {"objective": "the same work, repeated"}

    outcomes = [
        sensor.should_sense(dict(state), event="operator_instruction", workstream="ws")
        for _ in range(4)
    ]

    assert [allowed for allowed, _ in outcomes] == [True, False, False, False]
    assert outcomes[1][1] == "unchanged_state"


def test_a_turn_identity_does_not_suppress_a_genuine_state_change():
    """A different state is a different event, identity or not."""
    sensor = ShadowSensor(store=None)

    sensor.should_sense(
        {"objective": "first"},
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-a",
    )
    allowed, why = sensor.should_sense(
        {"objective": "second, now blocked"},
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-b",
    )

    assert allowed is True
    assert why == Trigger.MATERIAL.value


def test_a_turn_identity_does_not_resurrect_a_non_material_event():
    """A non-material event stays non-material, whatever the identity is."""
    sensor = ShadowSensor(store=None)

    sensor.should_sense(
        {"objective": "x"},
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-a",
    )
    allowed, why = sensor.should_sense(
        {"objective": "y"},
        event="not_a_material_event",
        workstream="ws",
        turn_id="turn-b",
    )

    assert allowed is False
    assert why == "not_material:not_a_material_event"


def test_turn_identity_is_scoped_to_its_workstream():
    """Two workstreams must not see each other's turn identities."""
    sensor = ShadowSensor(store=None)
    state = _attachment_state()

    a1, _ = sensor.should_sense(
        dict(state), event="operator_instruction", workstream="ws-a", turn_id="turn-1"
    )
    b1, _ = sensor.should_sense(
        dict(state), event="operator_instruction", workstream="ws-b", turn_id="turn-1"
    )

    assert a1 is True
    assert b1 is True, "a different workstream has its own dedupe timeline"


# ------------------------------------------------------- the privacy contract


def test_the_turn_identity_never_enters_the_projection():
    """The graded projection is the bounded semantic view. It must not grow.

    The identity lives in the DEDUPE KEY. If it reached the projection, the
    grader would receive a per-turn token it has no use for, and the
    projection's pinned key set would change.
    """
    from turnstone.core.shadow_observation import state_fingerprint

    state = _attachment_state()
    keyed = dict(state)
    keyed["turn_id"] = "turn-a"

    # The fingerprint of the GRADED projection is unchanged by the identity.
    assert state_fingerprint(state) == state_fingerprint(
        {k: v for k, v in keyed.items() if k != "turn_id"}
    )
    # And the identity is not a projection field at all: build_state drops
    # keys it was not asked for, so it cannot be smuggled in.
    built = lifecycle.build_state(
        objective=state["objective"],
        phase=state["phase"],
        attachments=state["attachments"],
    )
    assert "turn_id" not in built
    assert set(built) == {"objective", "phase", "attachments"}


def test_the_turn_identity_never_reaches_the_stored_observation(tmp_path):
    """Nothing about the identity may be written to the telemetry store."""
    sensor = ShadowSensor(
        store=tmp_path / "observations.jsonl",
        decision_id="tier1-decision",
        base_url="http://127.0.0.1:9",
        timeout=0.2,
    )
    sensor.observe(
        _attachment_state(),
        event="operator_instruction",
        workstream="ws",
        turn_id="turn-secret-value",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "observations.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows, "the observation must have been recorded"
    blob = json.dumps(rows[0])
    assert "turn-secret-value" not in blob
    assert "turn_id" not in blob


def test_the_turn_identity_never_reaches_the_cadence_row(tmp_path):
    """Cadence is bookkeeping: it carries no state and no identity."""
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
        _attachment_state(),
        workstream="ws",
        turn_id="turn-secret-value",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "cadence.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows
    blob = json.dumps(rows)
    assert "turn-secret-value" not in blob
    assert "turn_id" not in blob


# ------------------------------------------------------------- the hook path


def test_the_hook_passes_the_turn_identity_through(tmp_path):
    """The hook is the production call path; the identity must survive it."""
    seen: list[str] = []

    class _SpySensor:
        def should_sense(self, _state, *, event, force=False, workstream="", turn_id=""):
            seen.append(turn_id)
            return True, Trigger.MATERIAL.value

        def observe(self, *_a, **_k):
            return None

    hook = lifecycle.SensorHook(sensor=_SpySensor(), persist=False)
    hook.observe_event(
        "operator_instruction",
        _attachment_state(),
        workstream="ws",
        turn_id="turn-a",
    )

    assert seen == ["turn-a"]


def test_the_hook_keeps_dedupe_when_the_identity_is_repeated(tmp_path):
    """End to end through the real ShadowSensor: one turn, one observation."""
    sensor = ShadowSensor(
        store=tmp_path / "observations.jsonl",
        decision_id="tier1-decision",
        base_url="http://127.0.0.1:9",
        timeout=0.2,
    )
    hook = lifecycle.SensorHook(
        sensor, cadence_store=tmp_path / "cadence.jsonl", persist=False
    )
    state = _attachment_state()

    hook.observe_event(
        "operator_instruction", dict(state), workstream="ws", turn_id="turn-a"
    )
    hook.observe_event(
        "operator_instruction", dict(state), workstream="ws", turn_id="turn-a"
    )
    hook.observe_event(
        "operator_instruction", dict(state), workstream="ws", turn_id="turn-b"
    )

    outcomes = [
        json.loads(line)["outcome"]
        for line in (tmp_path / "cadence.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert outcomes == ["sensor_call", "dedupe_suppressed", "sensor_call"], outcomes


# ------------------------------------------------------- the driver signature


def test_should_sense_accepts_a_turn_id_and_keeps_its_old_shape():
    """The old call shape must keep working: turn_id is optional."""
    sensor = ShadowSensor(store=None)
    allowed, why = sensor.should_sense(
        {"objective": "x"}, event="operator_instruction", workstream="ws"
    )
    assert (allowed, why) == (True, Trigger.INITIAL.value)


def test_observe_accepts_a_turn_id_and_keeps_its_old_shape(tmp_path):
    sensor = ShadowSensor(
        store=tmp_path / "o.jsonl",
        decision_id="tier1-decision",
        base_url="http://127.0.0.1:9",
        timeout=0.2,
    )
    snapshot = sensor.observe(
        {"objective": "x"}, event="operator_instruction", workstream="ws"
    )
    assert snapshot.reason and snapshot.reason.startswith("transport")
