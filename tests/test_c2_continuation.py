"""C2 continuation contract — arithmetic, budgeting, and eligibility.

One test per clause of the operator ruling, so reverting any single clause
fails its own test and no others.  These are pure-logic tests: no provider,
no session, no I/O.
"""

from turnstone.core.continuation import (
    C2_REQUIRED_INCOMPLETE_REASON,
    CONFIGURED_CONTINUATION_CAP,
    CONTINUATION_MULTIPLIER,
    DEFAULT_PROVIDER_LEG_CEILING,
    MAX_CONTINUATION_LEGS,
    MAX_MODE2_CONTINUATION_LEGS,
    ContinuationBudget,
    ContinuationMode,
    ContinuationStop,
    compute_c_total,
    leg_budget,
    reasoning_case,
    remaining_total,
)

BIG_CONTEXT = 10_000_000


class TestCTotalArithmetic:
    """``C_total = min(2 * R_effective, 65_536)`` — operator examples."""

    def test_r_1000_gives_2000(self):
        assert compute_c_total(1_000) == 2_000

    def test_r_4096_gives_8192(self):
        assert compute_c_total(4_096) == 8_192

    def test_r_16384_gives_32768(self):
        assert compute_c_total(16_384) == 32_768

    def test_r_32768_gives_65536(self):
        """The current maximum caller request lands exactly on the cap."""
        assert compute_c_total(32_768) == 65_536

    def test_multiplier_is_two_not_four(self):
        """The 4R proposal was rejected; the superseded value must not return."""
        assert CONTINUATION_MULTIPLIER == 2
        assert compute_c_total(32_768) == 65_536  # not 131_072

    def test_huge_r_is_capped_and_not_inflated(self):
        """A caller asking for 384k (e.g. the deepseek row pin) gets the cap."""
        assert compute_c_total(384_000) == CONFIGURED_CONTINUATION_CAP

    def test_small_requests_are_not_inflated_to_the_global_cap(self):
        for r in (1, 10, 100, 1_000):
            assert compute_c_total(r) == 2 * r < CONFIGURED_CONTINUATION_CAP

    def test_zero_and_negative_r_grant_no_budget(self):
        assert compute_c_total(0) == 0
        assert compute_c_total(-5) == 0


class TestRemainingTotal:
    def test_remaining_subtracts_used(self):
        assert remaining_total(65_536, 32_768) == 32_768

    def test_remaining_never_goes_negative(self):
        assert remaining_total(2_000, 5_000) == 0


class TestLegBudget:
    """Each of the four terms must be able to bind on its own."""

    def test_r_effective_can_bind(self):
        assert (
            leg_budget(
                r_effective=100,
                remaining=10_000,
                provider_leg_ceiling=64_000,
                endpoint_context_remaining=10_000,
            )
            == 100
        )

    def test_remaining_total_can_bind(self):
        assert (
            leg_budget(
                r_effective=32_768,
                remaining=7,
                provider_leg_ceiling=64_000,
                endpoint_context_remaining=10_000,
            )
            == 7
        )

    def test_provider_leg_ceiling_can_bind(self):
        assert (
            leg_budget(
                r_effective=100_000,
                remaining=100_000,
                provider_leg_ceiling=DEFAULT_PROVIDER_LEG_CEILING,
                endpoint_context_remaining=100_000,
            )
            == 64_000
        )

    def test_endpoint_context_can_bind(self):
        assert (
            leg_budget(
                r_effective=32_768,
                remaining=32_768,
                provider_leg_ceiling=64_000,
                endpoint_context_remaining=900,
            )
            == 900
        )

    def test_missing_context_measurement_yields_zero(self):
        """Fail closed: no safe accounting => no continuation."""
        assert (
            leg_budget(
                r_effective=32_768,
                remaining=32_768,
                provider_leg_ceiling=64_000,
                endpoint_context_remaining=None,
            )
            == 0
        )

    def test_negative_context_remaining_yields_zero(self):
        assert (
            leg_budget(
                r_effective=32_768,
                remaining=32_768,
                provider_leg_ceiling=64_000,
                endpoint_context_remaining=-1,
            )
            == 0
        )

    def test_leg_budget_never_exceeds_r_effective(self):
        """No leg ever receives a fresh unbounded R."""
        for remaining in (1, 500, 32_768, 65_536, 10**9):
            for ctx in (1, 900, 65_536, 10**9):
                assert (
                    leg_budget(
                        r_effective=1_000,
                        remaining=remaining,
                        provider_leg_ceiling=64_000,
                        endpoint_context_remaining=ctx,
                    )
                    <= 1_000
                )


class TestContinuationBudget:
    def _budget(self, r=32_768, initial=0):
        b = ContinuationBudget(r_effective=r)
        b.used_total = initial
        return b

    def test_c_total_is_computed_on_construction(self):
        assert self._budget(r=1_000).c_total == 2_000

    def test_total_counts_the_initial_generation(self):
        """A leg that already spent the whole allowance leaves nothing."""
        b = self._budget(r=32_768, initial=65_536)
        assert b.c_total == 65_536
        assert b.remaining == 0
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0
        assert b.stop is ContinuationStop.TOTAL_BUDGET_LIMIT

    def test_initial_generation_halves_the_run_budget(self):
        """32,768 initial leaves exactly one more request's worth."""
        b = self._budget(r=32_768, initial=32_768)
        assert b.remaining == 32_768
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 32_768

    def test_leg_limit_is_three(self):
        b = self._budget(r=32_768)
        for _ in range(MAX_CONTINUATION_LEGS):
            budget = b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
            assert budget > 0
            b.record_leg(generated_tokens=1)
        assert b.stop is None
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0
        assert b.stop is ContinuationStop.LEG_LIMIT

    def test_multiple_short_legs_stay_within_2r(self):
        """Three tiny legs are allowed; the run still cannot exceed 2R.

        Here the LEG limit binds first (correctly) because the legs are small
        — the budget is a separate, independently authoritative bound.
        """
        b = self._budget(r=32_768)
        spent = 0
        while True:
            budget = b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
            if budget == 0:
                break
            spent += 5
            b.record_leg(generated_tokens=5)
        assert b.legs_used == MAX_CONTINUATION_LEGS
        assert b.stop is ContinuationStop.LEG_LIMIT
        assert spent <= b.c_total == 65_536
        assert b.remaining > 0  # budget was never the binding constraint here

    def test_short_legs_exhaust_budget_before_leg_limit(self):
        """Budget is independently authoritative: it can stop first."""
        b = self._budget(r=100)
        b.used_total = 150  # one leg's worth already spent of C_total=200
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 50
        b.record_leg(generated_tokens=50)
        assert b.stop is ContinuationStop.TOTAL_BUDGET_LIMIT
        assert b.legs_used == 1 < MAX_CONTINUATION_LEGS

    def test_leg_limit_can_stop_before_budget(self):
        b = self._budget(r=32_768)
        for _ in range(MAX_CONTINUATION_LEGS):
            b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
            b.record_leg(generated_tokens=1)
        assert b.remaining > 0
        assert b.stop is None
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        assert b.stop is ContinuationStop.LEG_LIMIT

    def test_missing_completion_tokens_fails_closed(self):
        """Character counts are not acceptable accounting."""
        b = self._budget(r=32_768)
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        b.record_leg(generated_tokens=None)
        assert b.stop is ContinuationStop.PROVIDER_FAILURE
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0

    def test_unusable_context_fails_closed(self):
        b = self._budget(r=32_768)
        assert b.next_leg_budget(endpoint_context_remaining=None) == 0
        assert b.stop is ContinuationStop.CONTEXT_CAPACITY

    def test_context_recomputed_per_leg_can_stop_a_later_leg(self):
        """A growing prefill shrinks each leg's allowance."""
        b = self._budget(r=32_768)
        first = b.next_leg_budget(endpoint_context_remaining=5_000)
        b.record_leg(generated_tokens=5_000)
        second = b.next_leg_budget(endpoint_context_remaining=400)
        assert first == 5_000
        assert second == 400
        b.record_leg(generated_tokens=400)
        assert b.next_leg_budget(endpoint_context_remaining=0) == 0
        assert b.stop is ContinuationStop.CONTEXT_CAPACITY

    def test_route_advertised_context_cannot_mask_a_small_endpoint(self):
        """The endpoint measurement governs, not a route's advertised ctx.

        A route may advertise 266,000 while the resident endpoint serves
        155,648.  Because the per-leg value is passed in from the endpoint,
        a tiny real measurement bounds the leg regardless.
        """
        b = self._budget(r=32_768)
        budget = b.next_leg_budget(endpoint_context_remaining=1_200)
        assert budget == 1_200  # not 32,768, and not 266,000-derived

    def test_exhaustion_is_detectable(self):
        """Burning the cap exactly must leave an observable stop."""
        b = self._budget(r=1_000)
        b.record_leg(generated_tokens=2_000)
        assert b.remaining == 0
        assert b.stop is ContinuationStop.TOTAL_BUDGET_LIMIT
        assert b.exhausted is True

    def test_already_stopped_budget_yields_no_further_legs(self):
        b = self._budget(r=32_768)
        b.stop = ContinuationStop.PREFIX_MISMATCH
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0
        assert b.stop is ContinuationStop.PREFIX_MISMATCH


class TestReasoningCase:
    def test_not_truncated_is_not_continuable(self):
        c = reasoning_case(
            finish_reason="stop",
            has_tool_calls=False,
            visible_content="done",
            reasoning_content="",
        )
        assert c.eligible is False
        assert c.stop is ContinuationStop.COMPLETED

    def test_mode1_thinking_off_visible_only(self):
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="1\n2\n3\n",
            reasoning_content="",
            incomplete_reason=C2_REQUIRED_INCOMPLETE_REASON,
        )
        assert c.eligible is True
        assert c.replay_reasoning is False
        assert c.mode is ContinuationMode.MODE1_VISIBLE_ONLY

    def test_mode1_requires_the_truthful_reason(self):
        """C2 never continues on ``length`` alone — in EITHER mode."""
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="partial",
            reasoning_content="",
            incomplete_reason=C2_REQUIRED_INCOMPLETE_REASON,
        )
        assert c.eligible is True
        assert c.mode is ContinuationMode.MODE1_VISIBLE_ONLY

    def test_mode1_non_truthful_reasons_give_zero_continuation(self):
        for reason in (
            None,
            "",
            "context_capacity",
            "content_filter",
            "time_limit",
            "indent_limit",
            "unknown_reason",
        ):
            c = reasoning_case(
                finish_reason="length",
                has_tool_calls=False,
                visible_content="partial",
                reasoning_content="",
                incomplete_reason=reason,
            )
            assert c.eligible is False, reason
            # No C2 run exists, so no continuation stop is recorded: the
            # existing truncated behaviour is retained unchanged.
            assert c.stop is None, reason
            assert c.mode is None, reason

    def test_mode1_stop_finish_reason_is_not_eligible(self):
        c = reasoning_case(
            finish_reason="stop",
            has_tool_calls=False,
            visible_content="partial",
            reasoning_content="",
            incomplete_reason=C2_REQUIRED_INCOMPLETE_REASON,
        )
        assert c.eligible is False
        assert c.stop is ContinuationStop.COMPLETED

    def test_mode2_same_trigger_matrix(self):
        """The trigger is shared: Mode 2 refuses on the same vocabulary."""
        for reason in (
            None,
            "",
            "context_capacity",
            "content_filter",
            "time_limit",
            "indent_limit",
            "unknown_reason",
        ):
            c = reasoning_case(
                finish_reason="length",
                has_tool_calls=False,
                visible_content="partial",
                reasoning_content="chain",
                incomplete_reason=reason,
            )
            assert c.eligible is False, reason
            assert c.stop is ContinuationStop.UNSUPPORTED_REASONING_PARTIAL, reason

    def test_mode2_stop_finish_reason_is_not_eligible(self):
        c = reasoning_case(
            finish_reason="stop",
            has_tool_calls=False,
            visible_content="partial",
            reasoning_content="chain",
            incomplete_reason=C2_REQUIRED_INCOMPLETE_REASON,
        )
        assert c.eligible is False
        assert c.stop is ContinuationStop.COMPLETED

    def test_mode2_completed_reasoning_plus_stable_partial(self):
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="1\n2\n3\n",
            reasoning_content="I will count upward.",
            incomplete_reason="max_output_tokens",
        )
        assert c.eligible is True
        assert c.replay_reasoning is True
        assert c.mode is ContinuationMode.MODE2_REASONING_CONTINUATION

    def test_truncated_tool_call_is_never_continued(self):
        """C2 v1 does not continue tool calls and executes none."""
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=True,
            visible_content="partial",
            reasoning_content="",
        )
        assert c.eligible is False
        assert c.stop is ContinuationStop.UNSUPPORTED_TOOL_PARTIAL

    def test_reasoning_truncated_with_no_visible_partial_is_unsupported(self):
        """Case B — reasoning ate the budget.  Must NOT degrade to Mode 1."""
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="",
            reasoning_content="thinking and thinking",
        )
        assert c.eligible is False
        assert c.stop is ContinuationStop.UNSUPPORTED_REASONING_PARTIAL

    def test_no_visible_and_no_reasoning_is_unsupported(self):
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="",
            reasoning_content="",
        )
        assert c.eligible is False
        assert c.stop is ContinuationStop.UNSUPPORTED_REASONING_PARTIAL

    def test_reasoning_present_without_visible_is_not_mode2(self):
        """Thinking-ON-with-prefill must not be read as Mode 2 eligibility."""
        c = reasoning_case(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="",
            reasoning_content="long chain of thought",
        )
        assert c.replay_reasoning is False


class TestMode2Boundary:
    """Mode 2 is gated on the visible answer having hit the OUTPUT limit."""

    def _case(self, **kw):
        base = dict(
            finish_reason="length",
            has_tool_calls=False,
            visible_content="1\n2\n",
            reasoning_content="chain of thought",
        )
        base.update(kw)
        return reasoning_case(**base)

    def test_absent_incomplete_reason_refuses_mode2(self):
        c = self._case(incomplete_reason=None)
        assert c.eligible is False
        assert c.stop is ContinuationStop.UNSUPPORTED_REASONING_PARTIAL

    def test_unknown_incomplete_reason_refuses_mode2(self):
        c = self._case(incomplete_reason="content_filter")
        assert c.eligible is False
        assert c.stop is ContinuationStop.UNSUPPORTED_REASONING_PARTIAL

    def test_mode2_never_downgrades_to_mode1(self):
        """Dropping the reasoning changes the turn's meaning, not its length."""
        c = self._case(incomplete_reason=None)
        assert c.mode is None
        assert c.replay_reasoning is False
        assert c.eligible is False

    def test_exact_ruled_literal_is_accepted(self):
        c = self._case(incomplete_reason=C2_REQUIRED_INCOMPLETE_REASON)
        assert c.eligible is True
        assert c.replay_reasoning is True

    def test_no_visible_partial_never_continues_even_with_output_reason(self):
        c = self._case(visible_content="", incomplete_reason="max_output_tokens")
        assert c.eligible is False
        assert c.stop is ContinuationStop.UNSUPPORTED_REASONING_PARTIAL


class TestMode2DepthLimit:
    """C2 v1 allows exactly ONE reasoning-bearing continuation leg."""

    def test_mode2_leg_ceiling_is_one(self):
        assert MAX_MODE2_CONTINUATION_LEGS == 1

    def test_second_reasoning_leg_is_refused_with_leg_limit(self):
        b = ContinuationBudget(
            r_effective=32_768,
            mode=ContinuationMode.MODE2_REASONING_CONTINUATION,
        )
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) > 0
        b.record_leg(generated_tokens=100)
        assert b.legs_used == 1
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0
        assert b.stop is ContinuationStop.LEG_LIMIT

    def test_mode2_self_truncation_stops_at_the_one_leg_boundary(self):
        """A Mode-2 leg that itself ends on length must NOT respawn."""
        b = ContinuationBudget(
            r_effective=32_768,
            mode=ContinuationMode.MODE2_REASONING_CONTINUATION,
        )
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        b.record_leg(generated_tokens=32_768)  # hit its own cap
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0
        assert b.stop is ContinuationStop.LEG_LIMIT

    def test_mode1_still_allows_three(self):
        b = ContinuationBudget(r_effective=32_768)
        assert b.max_legs == MAX_CONTINUATION_LEGS == 3

    def test_mode2_shares_the_same_two_r_budget(self):
        b = ContinuationBudget(
            r_effective=32_768,
            mode=ContinuationMode.MODE2_REASONING_CONTINUATION,
        )
        assert b.c_total == 65_536


class TestZeroProgressAndMismatch:
    def test_reasoning_without_new_visible_suffix_stops(self):
        """A hidden reasoning-only leg must not burn the budget in a loop."""
        b = ContinuationBudget(
            r_effective=32_768,
            mode=ContinuationMode.MODE2_REASONING_CONTINUATION,
        )
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        b.record_leg(generated_tokens=5_000, new_visible_suffix=False)
        assert b.stop is ContinuationStop.ZERO_PROGRESS
        assert b.exhausted is True

    def test_zero_progress_leg_is_still_charged(self):
        b = ContinuationBudget(r_effective=32_768)
        b.used_total = 1_000
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        b.record_leg(generated_tokens=250, new_visible_suffix=False)
        assert b.used_total == 1_250

    def test_prefix_mismatch_stops_the_run(self):
        b = ContinuationBudget(r_effective=32_768)
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        b.record_prefix_mismatch()
        assert b.stop is ContinuationStop.PREFIX_MISMATCH
        assert b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT) == 0

    def test_normal_leg_with_suffix_does_not_stop(self):
        b = ContinuationBudget(r_effective=32_768)
        b.next_leg_budget(endpoint_context_remaining=BIG_CONTEXT)
        b.record_leg(generated_tokens=10, new_visible_suffix=True)
        assert b.stop is None
