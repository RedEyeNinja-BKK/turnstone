"""Shadow observation contract: typed, advisory, and fail-open.

This module is the sensor's boundary. It produces a `SensorSnapshot` and nothing
else — no model, provider, route, reasoning effort, tool permission, agent
selection or Output Guard value is read or written anywhere in this file. That
is enforced structurally (see `test_shadow_observation.py::test_snapshot_has_no_routing_fields`)
rather than by convention.

Three properties this is built around:

* **Advisory.** A snapshot is a record. There is no consumer inside Turnstone,
  and `apply()` does not exist, so there is nothing to call by accident.
* **Fail open.** Every failure mode — serializer, Switchyard, timeout, invalid
  response, storage — becomes an *unavailable* observation. Sensing never fails a
  task.
* **Honest about absence.** A signal that could not be observed is
  ``available=False, value=None`` and never a zero or a carried-forward value.
  In particular ``local_suitability`` is never available from the fallback
  backend, and observability stays ``unknown`` rather than being enabled because
  an upstream happens to offer a confidence field.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping

SENSOR_VERSION = "turnstone-sensor-shadow:v1"

#: The one capability this module may name. It deliberately knows nothing about
#: OpenRouter, Span, the HTPC address, Laya, the fallback rule, or any
#: credential: failover is Switchyard's concern, not the sensor's.
#:
#: This is the RESILIENT lane: Span R1 with a one-hop Laya R2. Naming the
#: resilient lane is what keeps the sensor resilient to a Span outage without
#: the sensor holding any failover knowledge.
#:
#: Operator ruling 2026-10-01 fixes the aux topology as:
#:   switchyard-smartlocal-aux-turnstone = Laya only
#:   switchyard-smartfree-aux-turnstone  = Span only (no fallback leg)
#:   switchyard-smart-aux-turnstone      = Span R1, failover to Laya R2
#: and names switchyard-smart-aux-turnstone the operational live lane, which is
#: why the sensor and the Output Guard both resolve here.
DECISION_CAPABILITY = "switchyard-smart-aux-turnstone"

#: Signals requested on every observation. `local_suitability` is absent by
#: design — the fallback backend is not qualified for it and inferring it is
#: forbidden — so it is recorded as unavailable rather than asked for.
REQUESTED_SIGNALS = (
    "task_complexity",
    "reasoning_demand",
    "tool_dependency",
    "agentic_complexity",
    "route_sufficiency",
)
UNSUPPORTED_ON_FALLBACK = "local_suitability"

#: Fields a snapshot must never carry. Asserted at construction time, so a
#: future field cannot be added without tripping the guard.
FORBIDDEN_KEYS = frozenset(
    {
        "model",
        "provider",
        "route",
        "reasoning_effort",
        "reasoning",
        "target",
        "llm_client",
        "temperature",
        "agent",
        "tools",
        "permissions",
        "escalate",
        "retry",
        "output_guard",
    }
)


class Observability(str, Enum):
    """How much the backend actually commits to. Never invented."""

    UNKNOWN = "unknown"


class Trigger(str, Enum):
    """Why an observation was taken. Drives cadence and dedupe."""

    INITIAL = "initial_substantive_state"
    MATERIAL = "material_state_transition"
    MANUAL = "manual"


#: Event kinds that justify a fresh observation. Anything not listed is not
#: material by definition, which is what makes suppression a whitelist rather
#: than a judgement call.
MATERIAL_EVENTS = frozenset(
    {
        "operator_instruction",
        "delegation_result",
        "tool_failure",
        "task_agent_failure",
        "retry_transition",
        "escalation_transition",
        "new_evidence",
        "phase_transition",
    }
)


@dataclass(frozen=True)
class Signal:
    """One typed observation. Absence is explicit, never a zero."""

    name: str
    available: bool
    value: float | None
    backend: str
    reason: str | None = None
    primitive: str = "graded"
    observability: Observability = Observability.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["observability"] = self.observability.value
        return payload


@dataclass(frozen=True)
class SensorSnapshot:
    """An advisory observation. A record, not a control input."""

    sensor_version: str
    serializer_version: str
    trigger: Trigger
    state_fingerprint: str
    backend: str
    fallback_used: bool
    latency_ms: int
    timestamp: float
    #: Umbrella: any field was dropped, for either cause.
    state_truncated: bool
    serialized_chars: int
    serialized_state_tokens: int
    dropped_field_count: int
    #: Dropped because a higher tier was present, or the low-tier reserve had to
    #: be protected. Policy working as designed; NOT capacity pressure.
    evicted_by_tier: bool = False
    #: Dropped because the exact token budget was exhausted. This is the
    #: capacity-pressure signal and the one worth alerting on.
    overflowed_budget: bool = False
    signals: tuple[Signal, ...] = ()
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensor_version": self.sensor_version,
            "serializer_version": self.serializer_version,
            "trigger": self.trigger.value,
            "state_fingerprint": self.state_fingerprint,
            "backend": self.backend,
            "fallback_used": self.fallback_used,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
            "state_reduced": self.state_truncated,
            "evicted_by_tier": self.evicted_by_tier,
            "overflowed_budget": self.overflowed_budget,
            "serialized_chars": self.serialized_chars,
            "serialized_state_tokens": self.serialized_state_tokens,
            "dropped_field_count": self.dropped_field_count,
            "signals": [s.to_dict() for s in self.signals],
            "reason": self.reason,
            "extra": self.extra,
        }


def state_fingerprint(state: Mapping[str, Any]) -> str:
    """Deterministic fingerprint over the serializer-relevant state.

    Keyed on normalised name/value pairs only. A timestamp, a counter or any
    other volatile input would make identical state look new and defeat dedupe,
    so none is included. Two calls with the same meaningful state must produce
    the same fingerprint, whatever order the mapping was built in.
    """
    normalised = sorted(
        f"{str(name).strip()}={str(value).strip()}" for name, value in state.items()
    )
    return hashlib.sha256("\n".join(normalised).encode()).hexdigest()[:32]


def unavailable(
    *,
    trigger: Trigger,
    fingerprint: str,
    serializer_version: str,
    reason: str,
    latency_ms: int = 0,
    state_truncated: bool = False,
    serialized_chars: int = 0,
    serialized_state_tokens: int = 0,
    dropped_field_count: int = 0,
    evicted_by_tier: bool = False,
    overflowed_budget: bool = False,
    backend: str = "none",
    fallback_used: bool = False,
) -> SensorSnapshot:
    """The fail-open snapshot. A task must never fail because sensing did.

    Every signal is recorded unavailable with the reason, so a later reader can
    distinguish "not sensed" from "sensed and found nothing".
    """
    signals = tuple(
        Signal(
            name=name,
            available=False,
            value=None,
            backend=backend,
            reason=reason,
            observability=Observability.UNKNOWN,
        )
        for name in REQUESTED_SIGNALS
    )
    signals += (
        Signal(
            name=UNSUPPORTED_ON_FALLBACK,
            available=False,
            value=None,
            backend=backend,
            reason="backend_not_qualified_for_signal",
            observability=Observability.UNKNOWN,
        ),
    )
    return SensorSnapshot(
        sensor_version=SENSOR_VERSION,
        serializer_version=serializer_version,
        trigger=trigger,
        state_fingerprint=fingerprint,
        backend=backend,
        fallback_used=fallback_used,
        latency_ms=latency_ms,
        timestamp=time.time(),
        state_truncated=state_truncated,
        evicted_by_tier=evicted_by_tier,
        overflowed_budget=overflowed_budget,
        serialized_chars=serialized_chars,
        serialized_state_tokens=serialized_state_tokens,
        dropped_field_count=dropped_field_count,
        signals=signals,
        reason=reason,
    )


def build_snapshot(
    *,
    trigger: Trigger,
    fingerprint: str,
    serializer_version: str,
    answers: Mapping[str, Any],
    backend: str,
    fallback_used: bool,
    latency_ms: int,
    state_truncated: bool,
    serialized_chars: int,
    serialized_state_tokens: int,
    dropped_field_count: int,
    evicted_by_tier: bool = False,
    overflowed_budget: bool = False,
) -> SensorSnapshot:
    """Build a successful observation from a decision response.

    `local_suitability` is never derived from the other signals, never carried
    forward, and never proxied. It is recorded unavailable with the fixed
    reason, so its absence is visible rather than implied.
    """
    signals: list[Signal] = []
    for name in REQUESTED_SIGNALS:
        raw = answers.get(name)
        value = None
        if isinstance(raw, Mapping):
            for key in ("noul", "score", "value"):
                if isinstance(raw.get(key), (int, float)):
                    value = float(raw[key])
                    break
        signals.append(
            Signal(
                name=name,
                available=value is not None,
                value=value,
                backend=backend,
                reason=None if value is not None else "signal_absent_from_response",
                observability=Observability.UNKNOWN,
            )
        )
    signals.append(
        Signal(
            name=UNSUPPORTED_ON_FALLBACK,
            available=False,
            value=None,
            backend=backend,
            reason="backend_not_qualified_for_signal",
            observability=Observability.UNKNOWN,
        )
    )
    snapshot = SensorSnapshot(
        sensor_version=SENSOR_VERSION,
        serializer_version=serializer_version,
        trigger=trigger,
        state_fingerprint=fingerprint,
        backend=backend,
        fallback_used=fallback_used,
        latency_ms=latency_ms,
        timestamp=time.time(),
        state_truncated=state_truncated,
        evicted_by_tier=evicted_by_tier,
        overflowed_budget=overflowed_budget,
        serialized_chars=serialized_chars,
        serialized_state_tokens=serialized_state_tokens,
        dropped_field_count=dropped_field_count,
        signals=tuple(signals),
    )
    _assert_advisory(snapshot.to_dict())
    return snapshot


def _assert_advisory(payload: Mapping[str, Any]) -> None:
    """Refuse to emit anything that looks like a routing control."""
    blob = json.dumps(payload, default=str).lower()
    for key in FORBIDDEN_KEYS:
        if f'"{key}"' in blob:
            raise ValueError(f"snapshot must not carry routing field {key!r}")


def is_material(event: str) -> bool:
    """Whether an event justifies a fresh observation. Whitelist, not judgement."""
    return event in MATERIAL_EVENTS
