"""C2 — bounded automatic continuation of truncated model output.

A model response that ends on ``finish_reason == "length"`` has produced a
*prefix* of what it was going to say.  C2 lets the harness ask the model to
continue from that prefix, under a hard, pre-computable budget, instead of
handing the caller a silently truncated answer.

Scope of THIS module: the policy arithmetic and the eligibility decision.
It performs no I/O, touches no session state, and never calls a provider —
so every clause of the contract is unit-testable in isolation and a reviewer
can check the numbers without standing up a session.

Contract (operator ruling 2026-09-21, v1):

  * ``R_effective`` is the caller's requested output **after** existing
    Turnstone / provider clamping — i.e. the value actually sent on the wire,
    not the session's raw setting.
  * ``C_total = min(2 * R_effective, 65_536)`` and it **includes the initial
    generation**.  So a single truncated leg at the maximum request leaves at
    most one further request's worth of output — this is a safety boundary on
    *autonomous additional model work*, not a restatement of a theoretical
    ceiling.
  * Small requests are NOT inflated to the global cap.
  * At most ``MAX_CONTINUATION_LEGS`` further legs, but the token budget is
    independently authoritative: whichever binds first stops the run.
  * A continuation leg never receives a fresh unbounded ``R``.
  * Context safety is recomputed **per leg** and is ANCHORED to a real
    provider prompt measurement — ``usage.prompt_tokens`` of the request that
    was actually accepted — so only the text added since that measurement is
    ever converted from characters.  With no trustworthy anchor there is no
    number worth acting on, and the run fails closed rather than guessing.

Accounting (proven live 2026-09-21 — see
``operations/c2-token-accounting-proof-2026-09-21.md``): the canonical
counter is ``usage.completion_tokens``, which counts **newly generated tokens
only** (prefill-independent: a 280-char prefill change moved it by 0) and
**includes reasoning tokens** (thinking ON reported 3 visible chars but 68
completion tokens).  The returned message content is *not* a usable counter:
it is prefill + suffix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

# --------------------------------------------------------------------------
# v1 policy constants
# --------------------------------------------------------------------------

#: Absolute ceiling on a C2 run's total output, in tokens.  Applied on top of
#: the multiplier so that a very large ``R_effective`` still cannot authorize
#: an unbounded amount of autonomous work.
CONFIGURED_CONTINUATION_CAP = 65_536

#: Automatic continuation may extend a truncated answer by at most one
#: additional original request's worth of output.
CONTINUATION_MULTIPLIER = 2

#: Maximum number of continuation legs.  The token budget remains
#: independently authoritative, so this is a secondary bound: several short
#: legs are allowed, but they all draw down the same ``C_total``.
MAX_CONTINUATION_LEGS = 3

#: Mode-2 shares the same total budget but is limited to a SINGLE
#: reasoning-bearing continuation in v1.  The D-cell proved exactly one
#: reasoning-bearing continuation; an arbitrary
#: reasoning -> content -> reasoning -> ... chain was NOT proven, so v1 stays
#: inside the empirical proof instead of assuming it generalises.  A Mode-2
#: continuation that itself ends on ``length`` therefore STOPS rather than
#: spawning another reasoning leg.
MAX_MODE2_CONTINUATION_LEGS = 1

#: The ONLY provider reason that authorizes automatic continuation, in BOTH
#: modes.  C2 acts solely on a TRUTHFUL output-budget termination: a
#: ``length`` finish on its own is NOT sufficient.  A provider can misreport
#: other terminations as a completed/length response (the pre-A resident
#: llama.cpp serializer reports an output-budget stop as
#: ``response.completed`` and therefore emits no reason at all), so inferring
#: truncation from ``length`` alone would continue a turn for the wrong
#: reason.  Absent / None / ``context_capacity`` / ``content_filter`` /
#: ``time_limit`` / ``indent_limit`` / anything else => ZERO continuation.
#: Kept as a named constant so a future ruling can widen the accepted
#: vocabulary in exactly one place; this module never broadens it silently.
C2_REQUIRED_INCOMPLETE_REASON = "max_output_tokens"

#: The ONE provider implementation C2 v1 is enabled for.  Turnstone's
#: Responses provider is shared by OpenAI, DeepSeek and every migrated local
#: consumer, so the wire format alone cannot scope this — see
#: :func:`continuation_capability_ok`, which requires the backend model id as
#: well.  The code is reusable; the ENABLEMENT is not generic.
C2_PROVEN_PROVIDER = "OpenAIResponsesProvider"

#: Backend model id prefix of the lane the D-cell proved the continuation
#: contract on: the local ComfyNinja Qwen3.8-27B family.
C2_PROVEN_BACKEND_MODEL_PREFIX = "comfyninja/qwen3.8-27b-"

#: The resident endpoint's ACTUAL context capacity, in tokens.
#:
#: Deliberately NOT the 262,144 the model row advertises.  The advertised
#: figure is the route's promise; the loaded llama.cpp ``n_ctx`` is what the
#: server will really accept.  Substituting the advertised value would let a
#: continuation budget overrun the real wall, and there is no context shifting
#: to rescue an overrun.
C2_ENDPOINT_CONTEXT_TOKENS = 155_648


def continuation_capability_ok(*, backend_model_id: str, provider_name: str) -> bool:
    """Whether *this* serving lane is inside the ONE proven C2 v1 capability.

    Fails closed: anything not positively matched returns False, so an
    unrelated Responses lane, a cloud provider, or an unproven local model
    is never continued.
    """
    return (
        provider_name == C2_PROVEN_PROVIDER
        and backend_model_id.startswith(C2_PROVEN_BACKEND_MODEL_PREFIX)
    )


def validated_suffix(*, returned: str, prefill: str) -> str | None:
    """THE exact-prefix strip, in exactly one place.

    Returns the leg's genuinely new output, or ``None`` when the returned text
    does not begin with the exact submitted prefill — in which case the run
    stops and no suffix is exposed.

    The runner and the session both call THIS function, so the bytes the
    operator is shown and the bytes the budget is charged for can never come
    from two independently-written arithmetic rules that drift apart.
    """
    if not returned.startswith(prefill):
        return None
    return returned[len(prefill):]

#: Per-leg provider ceiling for the ``openai-compatible`` lane family.
#: ``ModelCapabilities.max_output_tokens`` defaults to this value and the
#: compat lane (``OPENAI_COMPAT_DEFAULT``) keeps it, so it is the clamp
#: ``session.py`` applies via ``min(max_tokens, caps.max_output_tokens)``.
#: It is NOT binding at the current maximum request (32,768 < 64,000) but it
#: becomes binding for any larger caller request.
DEFAULT_PROVIDER_LEG_CEILING = 64_000

#: Ceiling when the caller's request itself is unusable.
_MIN_R_EFFECTIVE = 1


class ContinuationStop(Enum):
    """Why a C2 run stopped.

    Deliberately a SEPARATE vocabulary from the provider's
    ``incomplete_reason`` (C0).  The provider's value is preserved verbatim
    and is never overwritten or synthesized here; these are Turnstone's own
    internal reasons and are not exposed as a new wire field in v1.
    """

    COMPLETED = "completed"
    LEG_LIMIT = "leg_limit"
    TOTAL_BUDGET_LIMIT = "total_budget_limit"
    ZERO_PROGRESS = "zero_progress"
    PREFIX_MISMATCH = "prefix_mismatch"
    CANCELLATION = "cancellation"
    UNSUPPORTED_TOOL_PARTIAL = "unsupported_tool_partial"
    UNSUPPORTED_REASONING_PARTIAL = "unsupported_reasoning_partial"
    PROVIDER_FAILURE = "provider_failure"
    CONTEXT_CAPACITY = "context_capacity"


class ContinuationMode(Enum):
    """Which continuation shape a truncated leg qualifies for."""

    MODE1_VISIBLE_ONLY = "mode1_visible_only"
    MODE2_REASONING_CONTINUATION = "mode2_reasoning_continuation"


@dataclass(frozen=True)
class ReasoningCase:
    """Whether the truncated leg is one C2 v1 may continue, and how."""

    eligible: bool
    stop: ContinuationStop | None = None
    #: True when the leg's reasoning phase is complete and a stable visible
    #: partial exists, so the replayed reasoning must be carried too.
    replay_reasoning: bool = False
    mode: ContinuationMode | None = None


def compute_c_total(r_effective: int) -> int:
    """Total output a C2 run may spend, initial generation included.

    ``min(2 * R_effective, CONFIGURED_CONTINUATION_CAP)``.

    A degenerate ``r_effective`` (0 or negative) yields 0 — the caller asked
    for no output, so there is nothing to continue and no budget to grant.
    """
    if r_effective < _MIN_R_EFFECTIVE:
        return 0
    return min(CONTINUATION_MULTIPLIER * r_effective, CONFIGURED_CONTINUATION_CAP)


def remaining_total(c_total: int, used_total: int) -> int:
    """Unspent run budget.  Never negative."""
    return max(0, c_total - used_total)


def leg_budget(
    *,
    r_effective: int,
    remaining: int,
    provider_leg_ceiling: int = DEFAULT_PROVIDER_LEG_CEILING,
    endpoint_context_remaining: int | None,
) -> int:
    """Suffix allowance for ONE continuation leg.

    ``min(R_effective, remaining, provider_leg_ceiling,
    endpoint_context_remaining)``, floored at 0.

    ``endpoint_context_remaining`` is **required** and is intentionally not
    defaulted: it must be recomputed for each leg from that leg's actual
    request representation (the real measured prompt of the last accepted
    call plus the text accumulated on top of it), because each continuation
    carries more state than the last.  It is never a value computed once for
    the run.  A caller that cannot compute it safely must not call this
    function — there is no context shifting to rescue an overrun, so the only
    safe answer is to stop.
    """
    if endpoint_context_remaining is None:
        return 0
    return max(
        0,
        min(r_effective, remaining, provider_leg_ceiling, endpoint_context_remaining),
    )


@dataclass
class ContinuationBudget:
    """Accumulates a C2 run's spend and hands out per-leg allowances.

    ``used_total`` is seeded with the INITIAL generation's
    ``completion_tokens`` — the budget covers the initial answer too, so a
    caller that already spent its whole allowance gets no continuation at all.
    """

    r_effective: int
    mode: ContinuationMode = ContinuationMode.MODE1_VISIBLE_ONLY
    c_total: int = field(init=False)
    used_total: int = 0
    legs_used: int = 0
    stop: ContinuationStop | None = None

    def __post_init__(self) -> None:
        self.c_total = compute_c_total(self.r_effective)

    @property
    def remaining(self) -> int:
        return remaining_total(self.c_total, self.used_total)

    @property
    def exhausted(self) -> bool:
        return self.stop is not None

    @property
    def accounting_failed(self) -> bool:
        """Whether the leg just recorded could not be charged.

        A leg with missing or untrustworthy usage is refused outright rather
        than merely left unbudgeted: "fail closed" means its output is not
        accepted either.  The executor reads this to decide whether a leg's
        suffix may be committed, so the accounting rule and the acceptance
        rule cannot disagree.
        """
        return self.stop is ContinuationStop.PROVIDER_FAILURE

    @property
    def max_legs(self) -> int:
        """Leg ceiling for this run's mode.

        Mode 2 is deliberately tighter: v1 only proved ONE reasoning-bearing
        continuation, so a second one is refused with ``leg_limit``.
        """
        if self.mode is ContinuationMode.MODE2_REASONING_CONTINUATION:
            return MAX_MODE2_CONTINUATION_LEGS
        return MAX_CONTINUATION_LEGS

    def next_leg_budget(self, *, endpoint_context_remaining: int | None) -> int:
        """Allowance for the next leg, or 0 when the run must stop.

        Stops on, in order: an already-recorded stop, the leg limit, an
        exhausted total budget, and an unusable context measurement.
        """
        if self.exhausted:
            return 0
        if self.legs_used >= self.max_legs:
            self.stop = ContinuationStop.LEG_LIMIT
            return 0
        if self.remaining <= 0:
            self.stop = ContinuationStop.TOTAL_BUDGET_LIMIT
            return 0
        if endpoint_context_remaining is None:
            # No safe context accounting => fail closed.
            self.stop = ContinuationStop.CONTEXT_CAPACITY
            return 0

        budget = leg_budget(
            r_effective=self.r_effective,
            remaining=self.remaining,
            provider_leg_ceiling=DEFAULT_PROVIDER_LEG_CEILING,
            endpoint_context_remaining=endpoint_context_remaining,
        )
        if budget <= 0:
            # Context (or the provider clamp) left no room for any suffix.
            self.stop = ContinuationStop.CONTEXT_CAPACITY
            return 0
        return budget

    def record_leg(self, *, generated_tokens: int | None, new_visible_suffix: bool = True) -> int:
        """Charge a completed leg's generation to the run budget.

        ``generated_tokens`` is the leg's ``usage.completion_tokens``.  A
        missing value fails closed: character-count estimates are not
        acceptable accounting, so the run ends rather than silently
        under-charging.

        ``new_visible_suffix`` is whether the leg actually extended the
        visible answer after the exact-prefix strip.  A Mode-2 leg can spend
        real reasoning tokens and produce NO new visible text; because
        reasoning is charged to the same budget, allowing that to repeat would
        let a run burn its whole allowance on hidden thinking while the answer
        never grows.  Such a leg stops the run with ``zero_progress``.
        """
        if self.exhausted:
            return self.remaining
        if generated_tokens is None:
            self.stop = ContinuationStop.PROVIDER_FAILURE
            return 0
        self.used_total += max(0, generated_tokens)
        self.legs_used += 1
        if not new_visible_suffix:
            self.stop = ContinuationStop.ZERO_PROGRESS
            return self.remaining
        if self.remaining <= 0:
            self.stop = ContinuationStop.TOTAL_BUDGET_LIMIT
        return self.remaining

    def record_prefix_mismatch(self) -> None:
        """The returned text did not begin with the exact submitted prefill.

        No fuzzy overlap, no semantic repair, no longest-prefix heuristic:
        the only safe disposition is to stop.
        """
        self.stop = ContinuationStop.PREFIX_MISMATCH


def reasoning_case(
    *,
    finish_reason: str | None,
    has_tool_calls: bool,
    visible_content: str,
    reasoning_content: str,
    incomplete_reason: str | None = None,
) -> ReasoningCase:
    """Decide whether a truncated leg is continuable, and in which mode.

    Mode 1 — thinking OFF, visible text only.
    Mode 2 — completed reasoning + stable visible partial (Case A).

    BOTH modes share one mandatory trigger: ``finish_reason == "length"``
    **and** ``incomplete_reason == C2_REQUIRED_INCOMPLETE_REASON``.  Neither
    mode ever continues on ``length`` alone.

    Unsupported, and therefore no continuation:

      * ``unsupported_tool_partial`` — an incomplete tool construction.  C2 v1
        never continues tool calls and never executes them, so the truncated
        call is dropped exactly as it is today.
      * ``unsupported_reasoning_partial`` — the reasoning phase was cut, or a
        reasoning-bearing leg lacks a truthful output-budget reason.  With
        reasoning and visible sharing one token budget this is the observable
        signature of Case B, and Mode 1 must NOT be inferred from it.
      * ``completed`` — the leg was not truncated after all.

    A leg that fails the trigger with NO reasoning at stake (Mode 1) returns
    ``stop=None``: C2 simply does not run, so there is no continuation to
    terminate and the existing truncated behaviour is retained unchanged.
    """
    if finish_reason != "length":
        return ReasoningCase(eligible=False, stop=ContinuationStop.COMPLETED)
    if has_tool_calls:
        return ReasoningCase(
            eligible=False, stop=ContinuationStop.UNSUPPORTED_TOOL_PARTIAL
        )
    if not visible_content:
        # Truncated with no stable visible partial: either the reasoning phase
        # ate the whole budget (Case B) or the model produced nothing usable.
        # Both are unsupported — never degrade to Mode 1 here.
        return ReasoningCase(
            eligible=False, stop=ContinuationStop.UNSUPPORTED_REASONING_PARTIAL
        )

    # ONE trigger, shared by both modes: a truthful output-budget stop.
    # ``length`` alone is not enough — see C2_REQUIRED_INCOMPLETE_REASON.
    truthful = incomplete_reason == C2_REQUIRED_INCOMPLETE_REASON

    if not reasoning_content:
        # Mode 1.  No reasoning phase is at stake, so an untruthful/absent
        # reason is not an "unsupported reasoning" case: C2 just does not run
        # and the existing truncated behaviour stands.
        if not truthful:
            return ReasoningCase(eligible=False, stop=None)
        return ReasoningCase(
            eligible=True,
            stop=None,
            replay_reasoning=False,
            mode=ContinuationMode.MODE1_VISIBLE_ONLY,
        )

    # Reasoning-bearing (Mode 2).  Without a truthful output-budget reason
    # this is a hard refusal and must NOT silently downgrade to Mode 1 —
    # dropping the reasoning would change the turn's meaning rather than
    # merely extending it.
    if not truthful:
        return ReasoningCase(
            eligible=False,
            stop=ContinuationStop.UNSUPPORTED_REASONING_PARTIAL,
        )
    return ReasoningCase(
        eligible=True,
        stop=None,
        replay_reasoning=True,
        mode=ContinuationMode.MODE2_REASONING_CONTINUATION,
    )
# --------------------------------------------------------------------------
# C2 continuation EXECUTOR — the request/merge half of the policy
# --------------------------------------------------------------------------
#
# The policy section above owns the arithmetic; the executor owns the loop
# that spends it.  It is deliberately dependency-injected: the caller
# supplies a callable that actually issues a leg, so the whole loop —
# prefix validation, suffix merge, accounting, stop selection — is testable
# with fakes and contains no provider, no session and no I/O.
#
# Why a separate object rather than logic inside ``session.py``: the operator
# ruling requires the policy module stay the accounting authority and the
# session integration stay thin.  Everything that could be got wrong
# numerically or structurally lives here, where it has direct tests.
#
# MODE 1 ONLY.  Mode 2 (reasoning-bearing continuation) is DEFERRED, and the
# executor cannot reach it in two independent ways rather than one:
#
#   1. ``ContinuationRunner.__init__`` REFUSES any budget whose mode is not
#      ``MODE1_VISIBLE_ONLY`` — an unreachable-by-construction guard.
#   2. The executor carries no reasoning machinery at all: no replayed
#      reasoning prefill, no per-leg reasoning block, and no reasoning field
#      on :class:`ContinuationOutcome`.  There is nothing for a Mode-2 run to
#      do, which is why the guard can only ever raise.
#
# The Mode-2 POLICY is retained (``ContinuationMode``,
# ``MAX_MODE2_CONTINUATION_LEGS``, :func:`reasoning_case`, and the mode rule
# in ``ContinuationBudget.max_legs``) because it is pure arithmetic with its
# own tests and a future ruling re-uses it verbatim.  Retaining the arithmetic
# is not the same as shipping an execution path: nothing in the turn loop
# consults :func:`reasoning_case`, so a reasoning-bearing turn performs no
# continuation at all.


@dataclass(frozen=True)
class ContinuationLegResult:
    """What one continuation request returned.

    ``visible_content`` is the provider's returned visible text **including
    the submitted prefill** (proven behaviour: the provider echoes the
    prefill and appends the new suffix), so the caller must NOT treat it as
    the new output.

    There is deliberately NO reasoning field: a C2 v1 leg is visible-text
    only, and a leg that returned reasoning is not a leg this executor may
    accept.
    """

    visible_content: str = ""
    completion_tokens: int | None = None
    finish_reason: str | None = None
    incomplete_reason: str | None = None


@dataclass
class ContinuationOutcome:
    """Merged result of a Mode-1 C2 run.

    Visible text only.  A reasoning-carrying outcome is not expressible here,
    so a caller cannot mistake a Mode-2 merge for a Mode-1 one.
    """

    visible_content: str
    used_total: int = 0
    legs_used: int = 0
    stop: ContinuationStop = ContinuationStop.COMPLETED


#: ``issue_leg(leg_index, visible_prefill, leg_budget) ->
#: ContinuationLegResult``.  ``leg_index`` is 1-based.  The leg re-enters the
#: caller's own sanctioned turn rail; the executor never reaches a provider.
IssueLeg = Callable[[int, str, int], ContinuationLegResult]

#: ``context_remaining_for_leg(leg_index) -> int | None``.  MUST be computed
#: from that leg's actual request representation — a REAL provider prompt
#: measurement for everything that was already sent, plus only the text added
#: since that measurement — and MUST return None when it cannot be computed
#: safely.
ContextRemaining = Callable[[int], int | None]


#: ``on_leg_accepted(leg_index, suffix)`` — called once per leg that was
#: BOTH prefix-validated and successfully charged, and therefore may be
#: committed.  Never called for a leg that failed either check.
OnLegAccepted = Callable[[int, str], None]


class ContinuationRunner:
    """Drives bounded continuation legs until the policy stops the run."""

    def __init__(
        self,
        *,
        budget: ContinuationBudget,
        issue_leg: IssueLeg,
        context_remaining_for_leg: ContextRemaining,
        initial_visible: str,
        on_leg_accepted: OnLegAccepted | None = None,
    ) -> None:
        # Mode 2 is not implemented by this executor and must not be
        # reachable through it.  Refusing at construction means a Mode-2
        # budget can never produce a running runner, whatever a future
        # caller believes — the guard is structural, not a policy check.
        if budget.mode is not ContinuationMode.MODE1_VISIBLE_ONLY:
            raise ValueError(
                "ContinuationRunner executes Mode 1 only "
                f"(got mode={budget.mode.value!r}); Mode 2 is deferred"
            )
        self._budget = budget
        self._issue_leg = issue_leg
        self._context_remaining = context_remaining_for_leg
        self._visible = initial_visible
        self._on_leg_accepted = on_leg_accepted

    @property
    def budget(self) -> ContinuationBudget:
        return self._budget

    def run(self) -> ContinuationOutcome:
        while not self._budget.exhausted:
            leg_index = self._budget.legs_used + 1
            remaining = self._context_remaining(leg_index)

            leg_budget = self._budget.next_leg_budget(
                endpoint_context_remaining=remaining
            )
            if leg_budget <= 0:
                break

            # The exact submitted visible text IS the prefill for this leg;
            # the accumulated answer grows by one suffix per leg.
            prefill = self._visible

            leg = self._issue_leg(leg_index, prefill, leg_budget)

            # --- exact-prefix invariant: non-negotiable -------------------
            # No fuzzy overlap, no semantic repair, no longest-prefix
            # heuristic.  A provider that stops echoing the prefill exactly
            # has changed contract, so the run stops rather than guessing.
            if not leg.visible_content.startswith(prefill):
                self._budget.record_prefix_mismatch()
                break

            suffix = validated_suffix(returned=leg.visible_content, prefill=prefill)
            if suffix is None:
                self._budget.record_prefix_mismatch()
                break

            extended = bool(suffix)

            remaining_after = self._budget.record_leg(
                generated_tokens=leg.completion_tokens,
                new_visible_suffix=extended,
            )

            # A leg is ACCEPTED only when it both continued the exact prefill
            # and could be charged.  An unaccountable leg's output is
            # discarded, not merely unbudgeted — fail closed applies to the
            # text as well as to the wallet, and the commit hook below never
            # fires for it.
            accepted = extended and not self._budget.accounting_failed

            if accepted:
                self._visible += suffix
                if self._on_leg_accepted is not None:
                    self._on_leg_accepted(leg_index, suffix)

            if self._budget.exhausted:
                break
            if remaining_after <= 0:
                break
            if leg.finish_reason != "length":
                # The leg finished naturally: the answer is complete.
                break

        return ContinuationOutcome(
            visible_content=self._visible,
            used_total=self._budget.used_total,
            legs_used=self._budget.legs_used,
            stop=self._budget.stop or ContinuationStop.COMPLETED,
        )
