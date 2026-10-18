"""Retry classification for gateway 5xx responses.

A gateway reports "the request produced no usable output" as 502/503/504, which
the OpenAI SDK folds into ``InternalServerError``. The class alone also covers a
deterministic provider 500, so the status is the discriminator.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from turnstone.core.model_turn import (
    _TRANSIENT_GATEWAY_STATUSES,
    lane_error_is_retryable,
)
from turnstone.core.providers._openai_common import RETRYABLE_ERROR_NAMES


def _error(name: str, status_code: int | None = None) -> BaseException:
    cls = type(name, (Exception,), {})
    exc = cls(f"{name} {status_code}")
    if status_code is not None:
        exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


def _lane(retryable: frozenset[str] = RETRYABLE_ERROR_NAMES) -> SimpleNamespace:
    return SimpleNamespace(provider=SimpleNamespace(retryable_error_names=retryable))


@pytest.mark.parametrize("status", sorted(_TRANSIENT_GATEWAY_STATUSES))
def test_gateway_transient_statuses_are_retryable(status: int) -> None:
    """502/503/504 mean no output existed, so re-sending cannot duplicate work."""
    assert lane_error_is_retryable(_lane(), _error("InternalServerError", status)) is True


def test_a_provider_500_stays_terminal() -> None:
    """500 is the same class but a different fact: the body may be deterministically bad."""
    assert lane_error_is_retryable(_lane(), _error("InternalServerError", 500)) is False


def test_a_status_bearing_error_without_a_status_stays_terminal() -> None:
    assert lane_error_is_retryable(_lane(), _error("InternalServerError")) is False


def test_declared_retryable_classes_are_unchanged() -> None:
    """The status rule is additive: it must not narrow the provider's own set."""
    for name in ("RateLimitError", "APITimeoutError", "IncompleteStreamError", "Timeout"):
        assert lane_error_is_retryable(_lane(), _error(name)) is True


def test_an_undeclared_non_status_class_is_not_retried() -> None:
    """Only the upstream-error class is re-examined; nothing else becomes retryable."""
    assert lane_error_is_retryable(_lane(), _error("AuthenticationError", 401)) is False
    assert lane_error_is_retryable(_lane(), _error("BadRequestError", 400)) is False
