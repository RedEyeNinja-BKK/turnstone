# SPDX-License-Identifier: Apache-2.0
"""Typed-decision execution seam for the Output Guard semantic tier.

The Output Guard semantic stage normally evaluates a tool result by calling
``model_turn()`` with a chat/Responses model and parsing a JSON object back into
:class:`~turnstone.core.output_guard_judge.OutputJudgeVerdict`.

Some backends are NOT chat models. Switchyard's ``/v1/decisions`` surface
answers with a typed decision object (``noul`` / ``choice`` / ``score``) and
generates no assistant text, so a chat/Responses provider can never execute it.
A registry row for such a backend carries ``supports_typed_decision`` in its
capabilities and deliberately carries **no** ``api_surface`` - the same native
representation rerankers use - so the ordinary provider factory would default it
to Chat Completions and fail.

This module is the single place that knows how to execute such a backend. The
Output Guard identifies the kind from registry capabilities BEFORE any provider
construction and dispatches here; nothing else in the guard learns Switchyard
HTTP details.

Failure policy (deliberate, and the security floor depends on it): every
transport, provider, schema or adapter failure produces a labelled
:class:`TypedDecisionError`. The caller must degrade to a heuristic-only
disposition. A typed alias NEVER falls back to the session model.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# The decision contract this seam speaks. Anything else is refused rather than
# guessed at, so a mis-registered row cannot be executed under the wrong rules.
SUPPORTED_DECISION_CONTRACTS = ("switchyard-decision:v1",)

# Output Guard's own risk vocabulary. A typed ``choice`` must land on one of
# these exactly; no free-form risk strings are accepted.
VALID_RISK_LEVELS = ("none", "low", "medium", "high")

_DECISION_PATH = "/v1/decisions"


class TypedDecisionError(RuntimeError):
    """A typed-decision evaluation failed. Always degrades to heuristic-only."""


@dataclass(frozen=True)
class TypedDecisionSpec:
    """The typed-decision contract declared by a registry row."""

    contract: str
    endpoint: str
    decision_types: tuple[str, ...] = ("choice", "score", "noul")
    max_questions: int = 16

    @classmethod
    def from_capabilities(cls, caps: dict) -> "TypedDecisionSpec | None":
        """Return the spec when ``caps`` declares a typed-decision backend.

        ``None`` means the alias is an ordinary generative model and must keep
        the existing chat/Responses path.
        """
        if not isinstance(caps, dict):
            return None
        if not caps.get("supports_typed_decision"):
            return None
        contract = str(caps.get("decision_contract") or "").strip()
        if not contract:
            raise TypedDecisionError(
                "registry row declares supports_typed_decision without a decision_contract"
            )
        if contract not in SUPPORTED_DECISION_CONTRACTS:
            raise TypedDecisionError(
                f"unsupported decision contract {contract!r}"
            )
        types = caps.get("decision_types") or ("choice", "score", "noul")
        if isinstance(types, (list, tuple)):
            types = tuple(str(t) for t in types)
        else:
            raise TypedDecisionError("decision_types must be a sequence")
        return cls(
            contract=contract,
            endpoint=str(caps.get("decision_endpoint") or _DECISION_PATH),
            decision_types=types,
            max_questions=int(caps.get("max_questions") or 16),
        )


@dataclass(frozen=True)
class TypedDecisionResult:
    """A normalized typed decision, ready to become a guard verdict.

    ``provenance`` records HOW the decision was obtained. It is deliberately
    separate from any model reasoning: a typed decision carries none, and
    provenance must never be presented as if it were reasoning.
    """

    risk_level: str
    confidence: float | None
    provider: str = ""
    leg: str = ""
    contract: str = ""
    provenance: str = ""
    raw: dict = field(default_factory=dict)


def _clamp_confidence(value: object) -> float | None:
    """Clamp a confidence into [0, 1]; ``None`` when nothing trustworthy."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


def _risk_from_choice(choice: object) -> str:
    if not isinstance(choice, str):
        raise TypedDecisionError(f"choice must be a string, got {type(choice).__name__}")
    value = choice.strip().lower()
    if value not in VALID_RISK_LEVELS:
        raise TypedDecisionError(
            f"decision choice {choice!r} is not one of {VALID_RISK_LEVELS}"
        )
    return value


def normalize_decision(payload: dict, spec: TypedDecisionSpec) -> TypedDecisionResult:
    """Map a ``/v1/decisions`` response onto the guard's typed result.

    A response is acceptable only if it carries at least one real answer. A
    missing, empty or unrecognised answer is an ERROR, never a silent "none" -
    otherwise an unparseable response would downgrade a suspicious heuristic
    finding to clean.
    """
    if not isinstance(payload, dict):
        raise TypedDecisionError("decision response is not an object")
    if payload.get("error"):
        raise TypedDecisionError(f"decision upstream error: {payload['error']}")

    answers = payload.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise TypedDecisionError("decision response carries no answers")

    risk: str | None = None
    confidence: float | None = None

    for _key, answer in answers.items():
        if not isinstance(answer, dict):
            raise TypedDecisionError("each decision answer must be an object")
        kind = str(answer.get("type") or "").strip().lower()

        if kind == "choice":
            candidate = _risk_from_choice(answer.get("choice"))
            # A categorical answer is authoritative; the strongest one wins so a
            # multi-question decision can never understate risk.
            risk = candidate if risk is None else max(
                risk, candidate, key=VALID_RISK_LEVELS.index
            )
        elif kind == "score":
            # A score alone is not a risk level. It is only used for confidence
            # when a categorical answer is present; on its own it is NOT
            # silently interpreted as a risk level.
            score = _clamp_confidence(answer.get("score", answer.get("value")))
            if score is not None and (confidence is None or score > confidence):
                confidence = score
        elif kind == "noul":
            # `noul` is a graded "nothing of concern" signal, not a risk level.
            # It is never mapped onto risk: doing so would let a model express
            # risk through a vocabulary the guard does not validate.
            continue
        else:
            raise TypedDecisionError(f"unsupported decision answer type {kind!r}")

    if risk is None:
        raise TypedDecisionError(
            "decision response carried no categorical risk answer; refusing to "
            "treat it as clean"
        )

    provider = str(payload.get("provider") or "")
    leg = str(payload.get("leg") or ("primary" if provider else ""))
    contract = str(payload.get("contract") or spec.contract)
    if contract != spec.contract:
        raise TypedDecisionError(
            f"decision contract mismatch: expected {spec.contract!r}, got {contract!r}"
        )
    return TypedDecisionResult(
        risk_level=risk,
        confidence=confidence,
        provider=provider,
        leg=leg,
        contract=contract,
        provenance=f"typed decision via {spec.contract}"
        + (f" ({provider})" if provider else "")
        + (f" [{leg}]" if leg else ""),
        raw=payload,
    )


def build_request_body(
    state: str,
    *,
    spec: TypedDecisionSpec,
    decision_id: str,
) -> dict:
    """Build one Output Guard decision request.

    Two questions: a categorical risk grade and, when the contract supports it,
    a bounded confidence. The criteria strings reuse the Output Guard's own risk
    vocabulary so a decision is semantically aligned with the LLM judge's.
    """
    if not state.strip():
        raise TypedDecisionError("decision state must be non-empty")
    risk_labels = ", ".join(VALID_RISK_LEVELS)
    return {
        "model": decision_id,
        "state": state,
        "questions": {
            "risk": {
                "type": "choice",
                "instructions": (
                    "Classify the risk this tool output presents. Answer with "
                    f"exactly one of: {risk_labels}."
                ),
                "criteria": {
                    "true": "a risk level of {none, low, medium, high} is present",
                    "false": "the output is safe and no risk level applies",
                },
            },
        },
    }


def execute_decision(
    *,
    base_url: str,
    model: str,
    caps: dict,
    state: str,
    decision_id: str,
    timeout: float,
    opener: object | None = None,
) -> TypedDecisionResult:
    """Execute one typed decision and normalize it.

    Every failure path raises :class:`TypedDecisionError`; none of them can
    produce a "clean" result.
    """
    spec = TypedDecisionSpec.from_capabilities(caps)
    if spec is None:
        raise TypedDecisionError(
            f"alias {model!r} is not a typed-decision backend; refusing to execute it here"
        )
    body = build_request_body(state, spec=spec, decision_id=decision_id)
    url = f"{base_url.rstrip('/')}{spec.endpoint}"
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - base_url is operator-configured
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    open_fn = opener if opener is not None else urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:  # type: ignore[operator]
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise TypedDecisionError(f"decision upstream HTTP {exc.code}") from exc
    except Exception as exc:  # transport, DNS, refused, malformed
        raise TypedDecisionError(f"decision transport failure: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise TypedDecisionError(f"decision response is not JSON: {exc}") from exc
    return normalize_decision(parsed, spec)
