"""C2 runner — prefix invariant, merge, accounting, stop selection.

Fakes only: the runner is dependency-injected, so every structural clause of
the ruling is exercised without a provider or a session.

This file is deliberately Mode 1 only.  The executor has no Mode-2 machinery
at all, and the class below exists to hold that line: a Mode-2 budget must not
be able to produce a running runner.
"""

import pytest

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

    def __call__(self, leg_index, prefill, leg_budget):
        self.calls.append(
            {
                "leg_index": leg_index,
                "prefill": prefill,
                "leg_budget": leg_budget,
            }
        )
        return self._legs[leg_index - 1]


def make_runner(
    legs,
    *,
    initial_visible="1\n2\n3\n",
    r_effective=32_768,
    mode=ContinuationMode.MODE1_VISIBLE_ONLY,
    used_total=0,
    context=BIG,
    on_leg_accepted=None,
):
    budget = ContinuationBudget(r_effective=r_effective, mode=mode)
    budget.used_total = used_total
    issuer = FakeIssuer(legs)
    runner = ContinuationRunner(
        budget=budget,
        issue_leg=issuer,
        context_remaining_for_leg=lambda i: context,
        initial_visible=initial_visible,
        on_leg_accepted=on_leg_accepted,
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

    def test_a_failed_leg_suffix_is_never_committed(self):
        """Fail-closed covers the TEXT, not only the budget."""
        committed: list[tuple[int, str]] = []
        runner, _ = make_runner(
            [ContinuationLegResult(visible_content="1\n2\n3\n4\n", completion_tokens=None)],
            on_leg_accepted=lambda i, s: committed.append((i, s)),
        )
        out = runner.run()
        assert out.stop is ContinuationStop.PROVIDER_FAILURE
        assert committed == [], "an unaccountable leg must not be shown"
        assert out.visible_content == "1\n2\n3\n"

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

    def test_accepted_suffix_is_committed_once_per_leg(self):
        committed: list[tuple[int, str]] = []
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n"), leg("1\n2\n3\n4\n", "5\n", finish_reason="stop")],
            on_leg_accepted=lambda i, s: committed.append((i, s)),
        )
        runner.run()
        assert committed == [(1, "4\n"), (2, "5\n")]

    def test_context_is_recomputed_for_every_leg(self):
        """Never once per run: each leg carries more state than the last."""
        seen: list[int] = []

        def context(leg_index):
            seen.append(leg_index)
            return BIG

        budget = ContinuationBudget(r_effective=32_768)
        issuer = FakeIssuer(
            [leg("1\n2\n3\n", "4\n"), leg("1\n2\n3\n4\n", "5\n", finish_reason="stop")]
        )
        ContinuationRunner(
            budget=budget,
            issue_leg=issuer,
            context_remaining_for_leg=context,
            initial_visible="1\n2\n3\n",
        ).run()
        assert seen == [1, 2]


class TestMode2IsNotExecutable:
    """Mode 2 is deferred, so it must be unreachable through the executor."""

    def test_mode2_budget_cannot_construct_a_runner(self):
        budget = ContinuationBudget(
            r_effective=32_768, mode=ContinuationMode.MODE2_REASONING_CONTINUATION
        )
        with pytest.raises(ValueError, match="Mode 1 only"):
            ContinuationRunner(
                budget=budget,
                issue_leg=FakeIssuer([]),
                context_remaining_for_leg=lambda i: BIG,
                initial_visible="1\n2\n3\n",
            )

    def test_the_default_mode_is_mode1(self):
        assert ContinuationBudget(r_effective=32_768).mode is (
            ContinuationMode.MODE1_VISIBLE_ONLY
        )

    def test_the_executor_takes_no_reasoning_input(self):
        """No reasoning prefill parameter, so no caller can inject one."""
        import inspect

        params = inspect.signature(ContinuationRunner.__init__).parameters
        assert "initial_reasoning" not in params
        assert "issue_leg" in params

    def test_the_outcome_has_no_reasoning_surface(self):
        from turnstone.core.continuation import ContinuationOutcome

        assert "reasoning_items" not in ContinuationOutcome.__dataclass_fields__
        assert "reasoning_content" not in ContinuationLegResult.__dataclass_fields__


class TestRunnerAccounting:
    def test_prefill_is_not_double_counted(self):
        """Returned text = prefill + suffix, but only the suffix is charged."""
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n", completion_tokens=3, finish_reason="stop")]
        )
        out = runner.run()
        assert out.used_total == 3  # not len("1\n2\n3\n4\n")

    def test_reasoning_tokens_are_still_counted_by_the_counter(self):
        """The counter includes everything the leg generated, so a thinking
        backend stays correctly charged even though C2 is Mode 1 only."""
        runner, _ = make_runner(
            [leg("1\n2\n3\n", "4\n", completion_tokens=900, finish_reason="stop")]
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
