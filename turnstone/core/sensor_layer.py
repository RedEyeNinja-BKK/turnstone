# SPDX-License-Identifier: Apache-2.0
"""Advisory task-state sensing over the native typed-decision seam.

This module is PERCEPTION, not routing. It turns bounded, already-persisted
workstream state into a typed observation object by asking a typed-decision
backend (Span/RESPAN) to grade it. It never selects a model, a client, a
reasoning effort, or a route: :class:`SensorSnapshot` carries no field that can
be applied to a routing decision, by construction. A future policy layer reads
the snapshot; this module has no output that reaches a lane.

Design constraints this module holds itself to:

* **Reuse where reuse is real, and claim nothing else.** The genuinely shared
  surface is :class:`typed_decision.TypedDecisionSpec` — the contract
  vocabulary, the question-type bounds and ``max_questions`` all come from
  there, and an unsupported contract is refused there rather than guessed at.

  What is NOT shared is the request body, the transport call, and the answer
  read. :func:`typed_decision.execute_decision` has no parameter for a caller
  question set (it hardcodes :func:`typed_decision.build_request_body`, which
  builds the Output Guard's single categorical ``risk`` question), and
  :func:`typed_decision.normalize_decision` projects answers onto the guard's
  risk vocabulary. Routing a sensor through either would mean overwriting a
  security control's question or reinterpreting a task-state grade as a risk
  grade. So the sensor builds its own questions, posts its own request and
  reads its own graded values, and this module states plainly that it is a
  sibling of the guard's typed-decision path, not a caller of it.

* **Boring failure.** Every transport, schema, contract or timeout failure is a
  labelled :class:`SensorUnavailable` or an unobservable signal. Nothing raises
  into a caller's request path, and no failure mode can manufacture a confident
  observation. The caller is expected to simply continue without a snapshot.

* **Honest absence.** A signal that could not be observed is recorded as
  ``value=None`` with ``observable`` False — never a low score. Collapsing "we
  did not measure this"
  into "we measured this and it is low" is the specific dishonesty that would
  make a future routing policy act on a number that was never observed.

* **No content in the schema.** A snapshot carries bounded *typed* state
  (counts, ratios, enum membership) plus optional short operator-authored
  intent. It never carries conversation text, tool output, file contents,
  paths, or URLs. Free-text entered by a caller is truncated to a hard cap
  before it is sent, and the cap is enforced here rather than trusted to the
  caller.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from turnstone.core.typed_decision import TypedDecisionSpec

# Bump when the meaning of any field changes in a way a consumer could notice.
# Consumers are told to refuse an unknown version rather than guess.
SENSOR_SCHEMA_VERSION = "turnstone-sensor:v1"

# The one contract this module speaks. Anything else is refused, exactly as
# typed_decision refuses an unrecognised contract.
SENSOR_CONTRACT = "switchyard-decision:v1"

# Hard cap on any free-text the caller may contribute. Enforced here because
# the caller is not necessarily the module that talks to the network.
MAX_INTENT_CHARS = 2000

# Bounds on the whole rendered state string. The upstream capability executor
# also enforces a server-side ``max_state_chars``, but a caller able to push an
# unbounded body toward that limit is a content-boundary defect on this side
# regardless of what the server finally accepts.
MAX_STATE_CHARS = 8000
MAX_EVIDENCE_FIELDS = 24
MAX_KEY_CHARS = 64

# The Respan/Span decision surface accepts ONLY `noul` questions and answers
# with a graded score. The Output Guard already proved this upstream
# constraint empirically (a `choice` question is rejected with HTTP 400), so
# the sensor asks graded questions and reads the grade directly. No risk-style
# remapping is applied: a sensor score means "how much of X", and reusing the
# guard's categorical vocabulary would corrupt that meaning.
# `criteria.true` / `criteria.false` are the two poles the grade is asked
# between; they are part of the question's meaning, not decoration.
_SENSOR_QUESTIONS: dict[str, tuple[str, str, str]] = {
    "task_complexity": (
        "How complex is this task?",
        "the task needs many interdependent steps, deep domain knowledge, or "
        "hard decomposition to complete correctly",
        "the task is simple and direct, with few steps and little decomposition",
    ),
    "reasoning_demand": (
        "How much does this task demand multi-step reasoning?",
        "solving this requires holding many interacting constraints and "
        "deriving conclusions that are not stated anywhere in the input",
        "solving this is largely a lookup or a mechanical application of a "
        "known procedure",
    ),
    "tool_dependency": (
        "How much does completing this task depend on external tools?",
        "the task cannot be completed from its own description and needs "
        "tools to read, execute, search, or verify",
        "the task can be completed from the description alone with no tools",
    ),
    "agentic_complexity": (
        "How much coordination does this task need?",
        "this needs several ordered steps with dependencies, or delegation to "
        "child agents whose results must be integrated",
        "this is a single self-contained step needing no coordination",
    ),
    "local_suitability": (
        "How well suited is a small local model for this task?",
        "a small local model can plausibly do this task acceptably, with no "
        "cloud-only capability required",
        "this task needs large-scale capability, long reliable reasoning, or "
        "broad knowledge that a small local model should not be trusted with",
    ),
    "route_sufficiency": (
        "Is the current approach still making adequate progress?",
        "the current approach is failing, repeating, or stuck, and the task "
        "warrants a change of approach or capability",
        "the current approach is progressing normally toward the goal",
    ),
}


class SensorUnavailable(RuntimeError):
    """The sensor could not produce a snapshot. Never a routing signal."""


@dataclass(frozen=True)
class Observation:
    """One graded signal with its provenance.

    ``value`` is the graded score the backend returned, or ``None`` when the
    signal was not observable. ``None`` is never a zero and never a low score.
    """

    value: float | None
    confidence: float | None
    source: str
    observed_at: float

    @property
    def observable(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            # ``confidence`` mirrors ``value`` because the graded noul carries a
            # single number; a backend that reports a separate confidence would
            # be a schema change, not a silent reinterpretation of this field.
            "confidence": self.confidence,
            "source": self.source,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class SensorSnapshot:
    """A normalised, typed observation of one bounded engineering moment.

    Deliberately contains no routing directive: there is no ``model``,
    ``route``, ``client``, ``reasoning_effort`` or ``target`` field, so a
    consumer cannot read a routing command out of this object even by mistake.
    """

    schema_version: str
    observations: dict[str, Observation]
    confidence: float | None
    provenance: str
    latency_seconds: float
    intent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observations": {
                k: v.to_dict() for k, v in self.observations.items()
            },
            "confidence": self.confidence,
            "provenance": self.provenance,
            "latency_seconds": self.latency_seconds,
            "intent": self.intent,
        }

    def value(self, name: str) -> float | None:
        """The graded value of one signal, or ``None`` when unobservable."""
        obs = self.observations.get(name)
        return obs.value if obs is not None else None

    def is_observable(self, name: str) -> bool:
        return name in self.observations and self.observations[name].observable


def _truncate_intent(intent: str | None) -> str:
    """Bound operator-supplied free text before it can reach a network call."""
    if not intent:
        return ""
    cleaned = intent.strip()
    if len(cleaned) <= MAX_INTENT_CHARS:
        return cleaned
    return cleaned[:MAX_INTENT_CHARS]


def build_sensor_request(
    *,
    state: str,
    decision_id: str,
    signal_names: tuple[str, ...],
) -> dict:
    """Build a multi-signal decision request from the sensor question set.

    Mirrors :func:`typed_decision.build_request_body`'s contract - non-empty
    state, ``noul`` questions only, explicit criteria - without reusing it,
    because its question is the Output Guard's and overwriting it would alter a
    security control. Passing a multi-question body through the shared transport
    is what makes the two consumers converge on one execution path rather than
    two.
    """
    if not state.strip():
        raise SensorUnavailable("sensor state must be non-empty")
    unknown = [n for n in signal_names if n not in _SENSOR_QUESTIONS]
    if unknown:
        raise SensorUnavailable(f"unknown sensor signals: {sorted(unknown)}")
    if not signal_names:
        raise SensorUnavailable("at least one sensor signal is required")

    questions: dict[str, dict[str, Any]] = {}
    for name in signal_names:
        instructions, criteria_true, criteria_false = _SENSOR_QUESTIONS[name]
        questions[name] = {
            "type": "noul",
            "instructions": instructions,
            "criteria": {"true": criteria_true, "false": criteria_false},
        }
    return {"model": decision_id, "state": state, "questions": questions}


def render_sensor_state(evidence: dict[str, Any], *, intent: str = "") -> str:
    """Render bounded typed state as the decision ``state`` string.

    ``evidence`` is a mapping of signal name to value. Only scalar, bounded,
    caller-chosen facts belong here. The renderer emits a stable, sorted,
    key:value projection and never inspects or serialises a non-scalar, so a
    caller that mistakenly hands over a transcript gets an exception rather
    than a silent content leak onto a third-party endpoint.
    """
    if not isinstance(evidence, dict):
        raise SensorUnavailable("evidence must be a mapping of bounded scalars")
    parts: list[str] = []
    clean_intent = _truncate_intent(intent)
    if clean_intent:
        parts.append(f"Task intent: {clean_intent}")
    if evidence:
        if len(evidence) > MAX_EVIDENCE_FIELDS:
            raise SensorUnavailable(
                f"evidence carries {len(evidence)} fields; at most "
                f"{MAX_EVIDENCE_FIELDS} are sent"
            )
        for key in sorted(evidence, key=str):
            if not isinstance(key, str):
                raise SensorUnavailable("evidence keys must be strings")
            if len(key) > MAX_KEY_CHARS:
                raise SensorUnavailable(
                    f"evidence key {key[:24]!r}... exceeds {MAX_KEY_CHARS} chars"
                )
            value = evidence[key]
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value.strip()
                if len(rendered) > MAX_INTENT_CHARS:
                    raise SensorUnavailable(
                        f"evidence field {key!r} is free text too long to bound safely"
                    )
            else:
                raise SensorUnavailable(
                    f"evidence field {key!r} is {type(value).__name__}; sensor state "
                    "accepts only bounded scalars, never nested task content"
                )
            parts.append(f"{key}: {rendered}")
    if not parts:
        raise SensorUnavailable("sensor state would be empty")
    rendered_state = "\n".join(parts)
    if len(rendered_state) > MAX_STATE_CHARS:
        raise SensorUnavailable(
            f"rendered sensor state is {len(rendered_state)} chars; at most "
            f"{MAX_STATE_CHARS} are sent"
        )
    return rendered_state


# ``normalize_one_decision`` in the Switchyard capability executor defaults an
# ABSENT numeric answer to 1.0. That default is correct for the Output Guard,
# where a fabricated high value fails safe by inflating a risk grade. Applied to
# a sensor it fails the other way: a fallback that returned no usable number
# would be recorded as a maximally-confident observation of the property asked
# about. A sensor must never invent a confident value, so the generative-fallback
# leg is treated as UNOBSERVABLE and its fabricated 1.0 is discarded. Re-derive
# this if the executor ever stops defaulting, and only then allow the leg in.
_GENERATIVE_FALLBACK_LEG = "generative_fallback"


def normalize_sensor_answers(
    payload: dict,
    spec: TypedDecisionSpec,
    signal_names: tuple[str, ...],
) -> dict[str, Observation]:
    """Map a multi-signal decision response onto per-signal observations.

    A signal that is absent from the response becomes an explicit
    unobservable observation rather than a zero. A response that is malformed
    at the object level, or whose answers are unusable, raises
    :class:`SensorUnavailable` so the caller can continue without a snapshot -
    a partially-understood response is never presented as a complete one.

    The contract check and the graded-noul read are deliberately the SAME code
    the Output Guard uses (:func:`typed_decision.normalize_decision`): the
    contract is the thing that makes an answer interpretable, and duplicating
    the check would let the two consumers drift apart.

    Two properties of the live executor shape this honestly. Answers are keyed
    by the CALLER's question ids (the server preserves them so a model cannot
    rename the contract), so a signal's answer is looked up by the id that was
    sent. And the generative-fallback leg fabricates a 1.0 for an absent
    numeric, so that leg is recorded unobservable rather than believed.
    """
    if not isinstance(payload, dict):
        raise SensorUnavailable("decision response is not an object")
    if payload.get("error"):
        raise SensorUnavailable("decision upstream error")

    answers = payload.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise SensorUnavailable("decision response carries no answers")

    contract = str(payload.get("contract") or spec.contract)
    if contract != spec.contract:
        raise SensorUnavailable(
            f"decision contract mismatch: expected {spec.contract!r}, got {contract!r}"
        )

    # Only the primary leg answers a real graded question. The discriminator is
    # ``provider``, not ``leg``: on the real primary path the response is the
    # upstream body plus ``contract``/``served_model`` and carries NO ``leg`` key
    # at all, while the generative-fallback body carries ``leg`` and NO
    # ``provider``. A missing provider therefore means "not the primary leg" and
    # is refused, rather than being read as benign. See the module note on
    # _GENERATIVE_FALLBACK_LEG for why that leg is not believed.
    leg = str(payload.get("leg") or "").strip().lower()
    if leg == _GENERATIVE_FALLBACK_LEG or not str(payload.get("provider") or "").strip():
        raise SensorUnavailable(
            "answers did not come from a primary typed-decision leg "
            f"(leg={leg!r}); the generative-fallback leg's absent-numeric default "
            "is not a measurement, so it is not reported as an observation"
        )

    observed_at = time.time()
    observations: dict[str, Observation] = {}
    for name in signal_names:
        answer = answers.get(name)
        graded = _graded_noul(answer)
        observations[name] = (
            Observation(
                value=graded, confidence=graded, source=SENSOR_CONTRACT,
                observed_at=observed_at,
            )
            if graded is not None
            else _unobservable(observed_at)
        )
    return observations


def _unobservable(observed_at: float) -> Observation:
    """An explicitly unobservable signal — never a zero, never a low score."""
    return Observation(
        value=None, confidence=None, source=SENSOR_CONTRACT, observed_at=observed_at
    )


def _as_finite(value: object) -> float | None:
    """Coerce a JSON scalar to a finite float, or ``None`` when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _graded_noul(answer: object) -> float | None:
    """Read one graded ``noul`` answer, or ``None`` when it is not a grade.

    ``None`` is returned for a missing answer, a type this consumer does not
    understand (a type is never reinterpreted as a score), a non-numeric or
    non-finite value, and a value outside [0,1]. The last case matters most:
    the module cannot verify the grader's scale, so an out-of-range reading is
    unobservable rather than clamped to a pole, which would turn a nonsensical
    answer into a maximally-confident observation.
    """
    if not isinstance(answer, dict):
        return None
    if str(answer.get("type") or "").strip().lower() != "noul":
        return None
    graded = _as_finite(answer.get("noul", answer.get("value")))
    if graded is None or graded < 0.0 or graded > 1.0:
        return None
    return graded


def aggregate_confidence(observations: dict[str, Observation]) -> float | None:
    """Snapshot-level confidence, or ``None`` when nothing was observable.

    Defined as the MEAN graded confidence of the observable signals: a snapshot
    resting on one weakly-graded signal is not as trustworthy as one resting on
    several, and reporting the max would overstate the sparse case.
    """
    graded = [o.confidence for o in observations.values() if o.value is not None]
    if not graded:
        return None
    return sum(graded) / len(graded)


def sense(
    *,
    base_url: str,
    model: str,
    caps: dict,
    evidence: dict[str, Any],
    signal_names: tuple[str, ...],
    intent: str = "",
    timeout: float = 20.0,
) -> SensorSnapshot:
    """Observe one bounded engineering moment through a typed-decision backend.

    This is the whole public surface. It returns a snapshot or raises
    :class:`SensorUnavailable`; it never returns a partial snapshot, and it
    never touches routing state of any kind.
    """
    spec = TypedDecisionSpec.from_capabilities(caps)
    if spec is None:
        raise SensorUnavailable("model is not a typed-decision backend")
    if spec.contract != SENSOR_CONTRACT:
        raise SensorUnavailable(f"unsupported decision contract {spec.contract!r}")

    state = render_sensor_state(evidence, intent=intent)
    body = build_sensor_request(
        state=state, decision_id=model, signal_names=signal_names
    )

    # Everything that can raise on caller-supplied input lives inside the guard
    # below, so an ordinary caller mistake (a malformed base_url, a non-mapping
    # evidence value) surfaces as SensorUnavailable rather than escaping into a
    # request path.
    start = time.monotonic()
    try:
        url = f"{base_url.rstrip('/')}{spec.endpoint}"
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - base_url is operator-configured
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # type: ignore[operator]
            raw = response.read()
    except Exception as exc:
        raise SensorUnavailable(f"decision transport failure: {exc}") from exc
    latency = time.monotonic() - start

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SensorUnavailable(f"decision response is not JSON: {exc}") from exc

    observations = normalize_sensor_answers(parsed, spec, signal_names)

    provider = str(parsed.get("provider") or "")
    leg = str(parsed.get("leg") or ("primary" if provider else ""))
    provenance = f"sensor {SENSOR_SCHEMA_VERSION} via {spec.contract}"
    if provider:
        provenance += f" ({provider})"
    if leg:
        provenance += f" [{leg}]"
    provenance += f" latency_ms={int(latency * 1000)}"

    return SensorSnapshot(
        schema_version=SENSOR_SCHEMA_VERSION,
        observations=observations,
        confidence=aggregate_confidence(observations),
        provenance=provenance,
        latency_seconds=latency,
        intent=_truncate_intent(intent),
    )


__all__ = [
    "MAX_EVIDENCE_FIELDS",
    "MAX_INTENT_CHARS",
    "MAX_KEY_CHARS",
    "MAX_STATE_CHARS",
    "SENSOR_CONTRACT",
    "SENSOR_SCHEMA_VERSION",
    "Observation",
    "SensorSnapshot",
    "SensorUnavailable",
    "aggregate_confidence",
    "build_sensor_request",
    "normalize_sensor_answers",
    "render_sensor_state",
    "sense",
]
