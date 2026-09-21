"""C repair regressions — the two blockers the terminal full suite caught.

B-1 · ``test_chat_session_has_no_raw_provider_facing_holders``
    ChatSession must scope C2 from NEUTRAL lane values — ``lane.model`` plus the
    provider's DECLARED identity — never by introspecting the plant handle.
    The load-bearing proof lives here: a lane whose provider *class* is named
    ``OpenAIResponsesProvider`` is still refused when it declares something
    else, and a duck-typed stand-in that declares ``"openai"`` is accepted.
    That is a semantic property, not a rename.

B-2 · ``test_fallback_driver_uses_exact_primary_obo_lane``
    The per-call ``max_tokens`` override must reach the driver call ONLY when
    the caller supplied one.  With no override the call shape must be exactly
    the pre-C2 contract (the OBO guard is an exact-call assertion), so the
    keyword is omitted rather than passed as ``None``.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import MagicMock

from tests._session_helpers import make_session, replace_session_lane
from turnstone.core.model_turn import ModelLane
from turnstone.core.providers import ModelCapabilities
from turnstone.core.session import ChatSession

PROVEN_MODEL = "comfyninja/qwen3.8-27b-q3"


class _DeclaredProvider:
    """A plant handle whose ONLY readable surface is its declared identity.

    Any other attribute access fails the test: that is how these tests prove
    the session layer stops introspecting implementations instead of merely
    moving the read somewhere cosmetic.
    """

    def __init__(self, name: str) -> None:
        self.provider_name = name

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(f"session touched an undeclared provider attribute: {item!r}")


#: A provider whose PYTHON CLASS NAME is the string the old check matched.
#: Nothing may ever consult it again.
_ClassNamedOpenAIResponsesProvider = type("OpenAIResponsesProvider", (_DeclaredProvider,), {})


# --------------------------------------------------------------------------
# B-1 · capability scope is a neutral-value decision
# --------------------------------------------------------------------------


def test_proven_lane_is_eligible() -> None:
    session = make_session()
    replace_session_lane(
        session, provider=_DeclaredProvider("openai"), model=PROVEN_MODEL
    )
    assert session._continuation_capable(session._primary_lane()) is True


def test_declared_identity_decides_not_the_class_name() -> None:
    """Both directions: the class name is neither necessary nor sufficient."""
    session = make_session()

    # Class name matches the old check, declared identity does not -> refused.
    wrongly_named = _ClassNamedOpenAIResponsesProvider("openai-compatible")
    assert type(wrongly_named).__name__ == "OpenAIResponsesProvider"
    replace_session_lane(session, provider=wrongly_named, model=PROVEN_MODEL)
    assert session._continuation_capable(session._primary_lane()) is False

    # Duck-typed stand-in, no such class name, declared identity correct -> accepted.
    replace_session_lane(
        session, provider=_DeclaredProvider("openai"), model=PROVEN_MODEL
    )
    lane = session._primary_lane()
    assert type(lane.provider).__name__ != "OpenAIResponsesProvider"
    assert session._continuation_capable(lane) is True


def test_wrong_protocol_identity_is_not_eligible() -> None:
    session = make_session()
    for declared in ("openai-compatible", "xai", "anthropic", "anthropic-compatible", "google"):
        replace_session_lane(
            session, provider=_DeclaredProvider(declared), model=PROVEN_MODEL
        )
        assert session._continuation_capable(session._primary_lane()) is False, declared


def test_wrong_backend_model_family_is_not_eligible() -> None:
    session = make_session()
    replace_session_lane(
        session, provider=_DeclaredProvider("openai"), model="openai/gpt-5.6-luna"
    )
    assert session._continuation_capable(session._primary_lane()) is False


def test_missing_neutral_metadata_fails_closed() -> None:
    session = make_session()
    base_lane = session._primary_lane()

    # Empty declared identity.
    replace_session_lane(session, provider=_DeclaredProvider(""), model=PROVEN_MODEL)
    assert session._continuation_capable(session._primary_lane()) is False

    # Empty backend model id.
    replace_session_lane(session, provider=_DeclaredProvider("openai"), model="")
    assert session._continuation_capable(session._primary_lane()) is False

    # A lane carrying no declared identity at all is unproven, hence refused.
    unproven = dataclasses.replace(base_lane, provider_name="", model=PROVEN_MODEL)
    assert session._continuation_capable(unproven) is False


def test_the_plant_handle_is_never_consulted() -> None:
    """The proven neutral values decide, even with NO handle on the lane.

    The superseded implementation reached for ``lane.provider``; this proves
    the decision no longer depends on a handle existing at all.  Negative
    control for the same property: the same handleless lane with an unproven
    declared identity is refused.
    """
    session = make_session()
    handleless_proven = dataclasses.replace(
        session._primary_lane(), provider=None, provider_name="openai", model=PROVEN_MODEL
    )
    assert session._continuation_capable(handleless_proven) is True

    handleless_unproven = dataclasses.replace(
        handleless_proven, provider_name="openai-compatible"
    )
    assert session._continuation_capable(handleless_unproven) is False


def test_scope_check_reads_nothing_but_the_declared_identity() -> None:
    """Touching any undeclared provider attribute raises inside the fake."""
    session = make_session()
    replace_session_lane(
        session, provider=_DeclaredProvider("openai"), model=PROVEN_MODEL
    )
    assert session._continuation_capable(session._primary_lane()) is True


# --------------------------------------------------------------------------
# B-2 · the per-call override changes no call shape when it is absent
# --------------------------------------------------------------------------


def _mock_session() -> tuple[MagicMock, MagicMock, MagicMock]:
    sess = MagicMock()
    lane = MagicMock(spec=ModelLane)
    lane.alias = "oboagent"
    sess._primary_lane.return_value = lane
    tracker = sess._get_health_tracker.return_value
    consumer = MagicMock()
    return sess, lane, consumer, tracker  # type: ignore[return-value]


def _prepare(wire: list[dict[str, Any]], _lane: ModelLane) -> list[dict[str, Any]]:
    return wire


def test_driver_call_without_override_keeps_the_established_shape() -> None:
    sess, lane, consumer, tracker = _mock_session()
    result = MagicMock()
    sess._model_turn_with_retry.return_value = result

    assert ChatSession._model_turn_with_fallback(sess, consumer, _prepare) is result
    sess._model_turn_with_retry.assert_called_once_with(
        lane,
        tracker,
        consumer,
        _prepare,
        0,
        principal_id=None,
    )


def test_driver_call_carries_the_override_when_one_is_supplied() -> None:
    sess, lane, consumer, tracker = _mock_session()
    result = MagicMock()
    sess._model_turn_with_retry.return_value = result

    assert (
        ChatSession._model_turn_with_fallback(sess, consumer, _prepare, max_tokens=4096)
        is result
    )
    sess._model_turn_with_retry.assert_called_once_with(
        lane,
        tracker,
        consumer,
        _prepare,
        0,
        principal_id=None,
        max_tokens=4096,
    )
