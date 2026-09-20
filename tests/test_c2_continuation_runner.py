"""C2 runner — prefix invariant, merge, reasoning ordering, stop selection.

Fakes only: the runner is dependency-injected, so every structural clause of
the ruling is exercised without a provider or a session.
"""

from turnstone.core.continuation import (
    ContinuationBudget,
    ContinuationMode,
    ContinuationRunner,
    ContinuationStop,
    ContinuationLegResult,
)

BIG = 10_000_000


class FakeIssuer:
    """Returns canned legs, recording what the runner asked for."""

    def __init__(self, legs):
        self._legs = list(legs)
        self.calls = []

    def __call__(self, leg_index, prefill, reasoning_prefill, leg_budget):
        self.calls.append(
            {
                "leg_index": leg_index,
                "prefill": prefill,
                "reasoning_prefill": reasoning_prefill,
                "leg_budget": leg_budget,
            }
        )
        return self._legs[leg_index - 1]


def make_runner(
    legs,
    *,
    initial_visible="1\n2\n3\n",
    initial_reasoning="",
    r_effective=32_768,
    mode=ContinuationMode.MODE1_VISIBLE_ONLY,
    used_total=0,
    context=BIG,
):
    budget = ContinuationBudget(r_effective=r_effective, mode=mode)
    budget.used_total = used_total
    issuer = FakeIssuer(legs)
    runner = ContinuationRunner(
        budget=budget,
        issue_leg=issuer,
        context_remaining_for_leg=lambda i: context,
        initial_visible=initial_visible,
        initial_reasoning=initial_reasoning,
    )
    return runner, issuer


def leg(prefill, suffix, **kw):
    kw.setdefault("completion_tokens", 10)
    kw.setdefault("finish_reason", "length")
    return ContinuationLegResult(visible_content=prefill + suffix, **kw)


class TestMode1Runner:
    def test_one_continuation_then_natural_completion(self):
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n", finish_reason="stop")]
        )
        out = runner.run()
        assert out.visible_content == "1\n2\n3\n4\n"
        assert out.legs_used == 1
        assert out.stop is ContinuationStop.COMPLETED

    def test_multiple_short_continuations(self):
        runner, _ = make_runner(
            [
                leg("1\n2\n3\n", "4\n"),
                leg("1\n2\n3\n4\n", "5\n"),
                leg("1\n2\n3\n4\n5\n", "6\n", finish_reason="stop"),
            ]
        )
        out = runner.run()
        assert out.visible_content == "1\n2\n3\n4\n5\n6\n"
        assert out.legs_used == 3
        assert out.stop is ContinuationStop.COMPLETED

    def test_three_leg_ceiling(self):
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n"), leg("1\n2\n3\n4\n", "5\n"), leg("1\n2\n3\n4\n5\n", "6\n")]
        )
        out = runner.run()
        assert out.legs_used == 3
        assert out.stop is ContinuationStop.LEG_LIMIT

    def test_budget_binds_before_leg_ceiling(self):
        runner, issuer = make_runner(
            [leg("1\n2\n3\n", "4\n", completion_tokens=10)],
            r_effective=1_000,
            used_total=1_990,  # C_total = 2_000, so only 10 remain
        )
        out = runner.run()
        assert out.legs_used == 1 < 3
        assert out.stop is ContinuationStop.TOTAL_BUDGET_LIMIT
        assert issuer.calls[0]["leg_budget"] == 10

    def test_prefix_mismatch_stops_without_merging(self):
        runner, _ = make_runner(
            [ContinuationLegResult(visible_content="totally different", completion_tokens=5)]
        )
        out = runner.run()
        assert out.visible_content == "1\n2\n3\n"  # unchanged
        assert out.stop is ContinuationStop.PREFIX_MISMATCH
        assert out.legs_used == 0

    def test_zero_progress_stops(self):
        runner, _ = make_runner([leg("1\n2\n3\n", "", completion_tokens=7)])
        out = runner.run()
        assert out.visible_content == "1\n2\n3\n"
        assert out.stop is ContinuationStop.ZERO_PROGRESS
        assert out.used_total == 7  # still charged

    def test_provider_failure_missing_completion_tokens(self):
        runner, _ = make_runner(
            [ContinuationLegResult(visible_content="1\n2\n3\n4\n", completion_tokens=None)]
        )
        out = runner.run()
        assert out.stop is ContinuationStop.PROVIDER_FAILURE

    def test_unusable_context_never_issues_a_leg(self):
        runner, issuer = make_runner([leg("1\n2\n3\n", "4\n")], context=None)
        out = runner.run()
        assert issuer.calls == []
        assert out.stop is ContinuationStop.CONTEXT_CAPACITY

    def test_leg_budget_is_passed_through_never_a_fresh_r(self):
        runner, issuer = make_runner(
            [leg("1\n2\n3\n", "4\n", finish_reason="stop")],
            r_effective=1_000,
            used_total=1_950,
        )
        runner.run()
        assert issuer.calls[0]["leg_budget"] == 50  # not 1_000

    def test_prefill_is_the_accumulated_answer(self):
        runner, issuer = make_runner(
            [leg("1\n2\n3\n", "4\n"), leg("1\n2\n3\n4\n", "5\n", finish_reason="stop")]
        )
        runner.run()
        assert issuer.calls[0]["prefill"] == "1\n2\n3\n"
        assert issuer.calls[1]["prefill"] == "1\n2\n3\n4\n"


class TestMode2Runner:
    def _mode2(self, legs, **kw):
        kw.setdefault("initial_reasoning", "leg0 reasoning")
        return make_runner(
            legs, mode=ContinuationMode.MODE2_REASONING_CONTINUATION, **kw
        )

    def test_completed_reasoning_plus_partial_yields_one_continuation(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="leg1 reasoning",
                 finish_reason="stop")]
        )
        out = runner.run()
        assert out.visible_content == "1\n2\n3\n4\n"
        assert out.legs_used == 1
        assert out.stop is ContinuationStop.COMPLETED

    def test_new_distinct_reasoning_item_is_accepted_and_ordered(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="leg1 reasoning",
                 finish_reason="stop")]
        )
        out = runner.run()
        assert [k for k, _ in out.reasoning_items] == ["original", "continuation-1"]
        assert out.reasoning_items[0][1] == "leg0 reasoning"
        assert out.reasoning_items[1][1] == "leg1 reasoning"

    def test_replayed_reasoning_is_not_duplicated(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="leg1 reasoning",
                 finish_reason="stop")]
        )
        out = runner.run()
        texts = [t for _, t in out.reasoning_items]
        assert texts.count("leg0 reasoning") == 1
        assert texts.count("leg1 reasoning") == 1

    def test_continuation_reasoning_identity_is_distinct(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="leg0 reasoning",
                 finish_reason="stop")]
        )
        out = runner.run()
        # Same text, different identity: the original is never overwritten.
        assert out.reasoning_items[0][0] == "original"
        assert out.reasoning_items[1][0] == "continuation-1"

    def test_reasoning_is_never_flattened_into_visible_text(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="SECRET COT",
                 finish_reason="stop")]
        )
        out = runner.run()
        assert "SECRET COT" not in out.visible_content

    def test_visible_output_has_no_duplicated_prefill(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="r", finish_reason="stop")]
        )
        out = runner.run()
        assert out.visible_content == "1\n2\n3\n4\n"
        assert out.visible_content.count("1\n2\n3\n") == 1

    def test_original_reasoning_is_replayed_to_the_leg(self):
        runner, issuer = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="r", finish_reason="stop")]
        )
        runner.run()
        assert issuer.calls[0]["reasoning_prefill"] == "leg0 reasoning"

    def test_mode2_second_leg_refused_at_one_leg_boundary(self):
        """The continuation itself truncated -> STOP, do not respawn."""
        runner, issuer = self._mode2(
            [leg("1\n2\n3\n", "4\n", reasoning_content="r", finish_reason="length")]
        )
        out = runner.run()
        assert out.legs_used == 1
        assert out.stop is ContinuationStop.LEG_LIMIT
        assert len(issuer.calls) == 1

    def test_reasoning_but_no_visible_suffix_is_zero_progress(self):
        runner, _ = self._mode2(
            [leg("1\n2\n3\n", "", reasoning_content="MORE COT", completion_tokens=900)]
        )
        out = runner.run()
        assert out.stop is ContinuationStop.ZERO_PROGRESS
        assert out.used_total == 900
        assert len(out.reasoning_items) == 1  # no new block recorded for a no-op leg

    def test_mode2_still_never_downgrades_prefix_mismatch(self):
        runner, _ = self._mode2(
            [ContinuationLegResult(visible_content="nope", completion_tokens=5,
                                   reasoning_content="r")]
        )
        out = runner.run()
        assert out.stop is ContinuationStop.PREFIX_MISMATCH


class TestRunnerAccounting:
    def test_prefill_is_not_double_counted(self):
        """Returned text = prefill + suffix, but only the suffix is charged."""
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n", completion_tokens=3, finish_reason="stop")]
        )
        out = runner.run()
        assert out.used_total == 3  # not len("1\n2\n3\n4\n")

    def test_reasoning_tokens_are_counted(self):
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n", completion_tokens=900,
                 reasoning_content="cot", finish_reason="stop")],
            mode=ContinuationMode.MODE2_REASONING_CONTINUATION,
            initial_reasoning="leg0",
        )
        out = runner.run()
        assert out.used_total == 900

    def test_initial_generation_counts_toward_the_total(self):
        runner, issuer = make_runner(
            [leg("1\n2\n3\n", "4\n")], r_effective=1_000, used_total=2_000
        )
        out = runner.run()
        assert issuer.calls == []
        assert out.stop is ContinuationStop.TOTAL_BUDGET_LIMIT

    def test_exact_absolute_ceiling_is_65536(self):
        runner, _ = make_runner([], r_effective=1_000_000)
        assert runner.budget.c_total == 65_536

    def test_multiplier_stays_two(self):
        runner, _ = make_runner([], r_effective=32_768)
        assert runner.budget.c_total == 65_536
