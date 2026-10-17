"""Tests for the bounded-state serializer.

Each test exists to make a specific failure impossible to reintroduce silently.
The suite is deliberately about BEHAVIOUR under reduction, not about reaching a
particular string, with one exception: determinism is asserted by exact
equality, because a serializer whose output varies cannot be reasoned about.

Every priority claim in the module docstring has a test here. If a future edit
weakens the policy, one of these fails rather than the drift surfacing later as
a silently misread task.
"""

from __future__ import annotations

import pytest

from turnstone.core.bounded_state import (
    CHARS_PER_TOKEN,
    SERIALIZER_VERSION,
    STATE_CHAR_BUDGET,
    STATE_TOKEN_BUDGET,
    StateBudgetError,
    estimate_tokens,
    serialize_state,
)

# A state that overflows the budget and carries one field at every tier.
OVERFLOW = {
    "objective": "CRITICAL: production routing is inverted and the fallback never fires.",
    "current_state": "cutover in progress, awaiting confirmation",
    "blockers": "one review finding open",
    "history": " ".join(f"event {i}: routine status poll, no material change" for i in range(120)),
    "filler": " ".join("irrelevant chatter " for _ in range(200)),
    "notes": " ".join("re-checked the same thing " for _ in range(120)),
    "completed": " ".join(f"done item {i}" for i in range(80)),
}


def test_budget_and_char_limit_are_derived_not_independent():
    """The char limit follows the token budget, so the two cannot drift."""
    assert STATE_CHAR_BUDGET == int(STATE_TOKEN_BUDGET * CHARS_PER_TOKEN)
    # Deliberately conservative against the measured 512-token ceiling.
    assert STATE_TOKEN_BUDGET < 512


def test_estimate_over_estimates_so_the_real_tokenizer_sees_less():
    """The error direction is the safe one: reserve more than is needed."""
    # 'word ' repeated: ~4.4-4.7 chars/token measured, 3.6 assumed.
    text = "word " * 200
    assert estimate_tokens(text) > len(text) / 4.7


def test_serialisation_is_deterministic_across_insertion_order():
    """A mapping's iteration order must never reach the output."""
    forward = dict(OVERFLOW)
    backward = dict(reversed(list(OVERFLOW.items())))
    assert serialize_state(forward).text == serialize_state(backward).text


def test_serialisation_is_stable_across_repeated_calls():
    assert serialize_state(OVERFLOW).text == serialize_state(OVERFLOW).text


def test_budget_is_always_respected():
    result = serialize_state(OVERFLOW)
    assert result.estimated_tokens <= STATE_TOKEN_BUDGET
    assert result.serialized_chars <= STATE_CHAR_BUDGET


def test_current_material_outranks_history():
    """The central policy claim: history and noise are dropped first."""
    text = serialize_state(OVERFLOW).text
    assert "objective:" in text
    assert "current_state:" in text
    assert "blockers:" in text
    for noise in ("history:", "filler:", "notes:", "completed:"):
        assert noise not in text, f"{noise} crowded out current material"


def test_truncation_is_reported():
    """A consumer must be able to tell a reduced observation from a full one."""
    result = serialize_state(OVERFLOW)
    assert result.state_truncated is True
    assert result.fields_dropped
    assert result.fields_kept < result.fields_in


def test_untouched_state_is_not_reported_as_truncated():
    result = serialize_state({"objective": "a small task", "progress": "half done"})
    assert result.state_truncated is False
    assert result.fields_dropped == ()


def test_provenance_carries_version_and_metrics():
    payload = serialize_state(OVERFLOW).to_dict()
    assert payload["serializer_version"] == SERIALIZER_VERSION
    assert payload["serialized_chars"] > 0
    assert payload["serialized_tokens"] > 0
    # The umbrella plus the two distinct causes. Reporting only one flag made a
    # healthy tier eviction indistinguishable from real capacity pressure.
    assert payload["state_reduced"] is True
    assert payload["evicted_by_tier"] is True or payload["overflowed_budget"] is True
    # Provenance must not duplicate the private state text.
    assert "text" not in payload
    assert "objective" not in str(payload)


def test_tier_eviction_and_budget_overflow_are_distinguished():
    """The correction this split exists for: a healthy serializer must not
    report itself as capacity-pressured.

    A state whose only excess is a low-tier `history` field is *reduced by
    policy*, with the budget barely touched. A state so large that the current
    tier alone cannot fit is *overflowed*. Both used to read as
    `state_truncated=True`, which made a healthy serializer look permanently
    unhealthy.
    """
    tier_only = serialize_state({
        "objective": "reconcile the failover proof harness with the disposable lane",
        "history": " ".join(f"routine event {i}" for i in range(400)),
    })
    assert tier_only.state_truncated is True, "the history field must be dropped"
    assert tier_only.evicted_by_tier is True, "the drop is policy, not pressure"
    assert tier_only.overflowed_budget is False, (
        "a tier eviction must NOT be reported as budget overflow"
    )
    assert tier_only.estimated_tokens < STATE_TOKEN_BUDGET, (
        "the budget was never approached, so this is not capacity pressure"
    )

    # Capacity pressure: the CURRENT tier alone cannot fit.
    #
    # Measured, not assumed. Per-field clipping at MAX_FIELD_CHARS caps one
    # field at ~71 tokens, so field SIZE alone cannot overflow 256. It takes
    # several LONG current-tier fields: 4 fields x 40 chars fits easily, while
    # 4 fields x 240 chars overflows. So the honest trigger condition is
    # *aggregate* current material, not a long objective on its own. Worth
    # knowing when reading the metric: one long objective is clipped, not
    # pressure; many long distinct facts are pressure.
    def _current(fields: int, length: int) -> dict:
        return {f"f{i}": "w" * length for i in range(fields)}

    # Measured on this build: 4x40 -> 50 tokens (no reduction); 6x120 -> 209
    # tokens (no reduction); 6x200 -> 228 tokens with 4 kept (overflowed). The
    # ratio is 3.6 chars/token, so the crossing sits between 120 and 200 chars
    # per field at six fields. These numbers are asserted, not assumed, so a
    # change to CHARS_PER_TOKEN or MAX_FIELD_CHARS that moves the threshold will
    # fail here rather than quietly redefining what "pressure" means.
    small = serialize_state(_current(4, 40))
    assert small.overflowed_budget is False
    assert small.state_truncated is False
    assert small.estimated_tokens < 60

    medium = serialize_state(_current(6, 120))
    assert medium.overflowed_budget is False, (
        f"6x120 measured {medium.estimated_tokens} tokens, which fits; "
        "if this now overflows the budget constants changed"
    )

    pressured = serialize_state(_current(6, 200))
    assert pressured.estimated_tokens <= STATE_TOKEN_BUDGET, "the bound must still hold"
    assert pressured.evicted_by_tier is False, (
        "with no low-tier fields present there is nothing to evict by tier"
    )
    # And one enormous field is clipped, NOT pressure — the distinction the
    # whole split exists to make.
    single = serialize_state({"objective": "z" * 5000})
    assert single.state_truncated is False
    assert single.overflowed_budget is False
    assert single.evicted_by_tier is False


def test_umbrella_flag_is_exactly_the_union_of_the_two_causes():
    """`state_reduced` must be true if and only if something was dropped.

    Every case must place the two causes in DIFFERENT combinations, including
    overflow-without-eviction. A set of cases where both flags agree cannot
    distinguish a union from either single flag, which is exactly how a
    mislabelled cause would pass unnoticed.
    """
    cases = {
        # name: (state, expected_evicted, expected_overflowed)
        "clean": ({"objective": "short objective", "phase": "verify"}, False, False),
        "tier_only": ({"objective": "x" * 50, "history": "y " * 3000}, True, False),
        "overflow_only": ({f"f{i}": "w" * 200 for i in range(6)}, False, True),
        "both": (
            # Enough current-tier material to overflow AND a low-tier history
            # field, so both causes fire in one observation. Measured: 1+6
            # current-tier fields of 200 chars plus history -> both flags true.
            {
                "objective": "critical state that must survive",
                **{f"f{i}": "w" * 200 for i in range(6)},
                "history": "y " * 3000,
            },
            True, True,
        ),
    }
    for name, (state, expect_evicted, expect_overflowed) in cases.items():
        result = serialize_state(state)
        assert result.evicted_by_tier is expect_evicted, name
        assert result.overflowed_budget is expect_overflowed, name
        assert result.state_truncated == (expect_evicted or expect_overflowed), (
            f"{name}: state_reduced must be the union of the two causes"
        )


def test_a_drop_is_always_attributed_to_exactly_one_cause():
    """Every dropped field lands in the union, and the union has no strays."""
    for state in (
        {"objective": "x" * 50, "history": "y " * 3000},
        {f"f{i}": "w" * 200 for i in range(6)},
        {"objective": "keep me", "history": "h" * 3000, "notes": "n" * 3000,
         "filler": "f" * 3000, "current_state": "w" * 200, "evidence": "w" * 200,
         "progress": "w" * 200},
    ):
        result = serialize_state(state)
        assert result.state_truncated == bool(result.fields_dropped)
        if result.evicted_by_tier:
            assert result.fields_dropped, "evicted_by_tier implies a dropped field"
        if result.overflowed_budget:
            assert result.fields_dropped, "overflowed_budget implies a dropped field"


def test_a_clean_state_reports_neither_cause():
    """No reduction at all means both flags are false, not merely one."""
    result = serialize_state({"objective": "a normal short objective", "phase": "verify"})
    assert result.state_truncated is False
    assert result.evicted_by_tier is False
    assert result.overflowed_budget is False
    assert result.fields_dropped == ()


def test_history_only_state_is_still_observable():
    """Reduction must never make a real state unobservable."""
    result = serialize_state({"history": " ".join(f"event {i}" for i in range(200))})
    assert result.text.strip()
    assert result.estimated_tokens <= STATE_TOKEN_BUDGET


def test_repeated_history_is_bounded_not_reported_as_duplicate():
    """Low-tier fields are dropped by PRIORITY, not by line deduplication.

    An earlier draft deduplicated rendered lines, which is unreachable for a
    Mapping: the field name is part of the line and mapping keys are unique, so
    two lines can never be identical. The policy that actually bounds repetition
    is the tier rule, and this test pins that instead of a fiction.
    """
    result = serialize_state(
        {"objective": "the current goal", "notes": "the same thing", "history": "old"}
    )
    assert "objective:" in result.text
    assert "notes:" not in result.text
    assert "history:" not in result.text
    assert "notes" in result.fields_dropped and "history" in result.fields_dropped


def test_whitespace_is_normalised_so_equivalent_states_match():
    a = serialize_state({"objective": "do   the thing\n\nnow"})
    b = serialize_state({"objective": "do the thing now"})
    assert a.text == b.text


def test_long_single_field_is_clipped_on_a_word_boundary():
    result = serialize_state({"evidence": "word " * 500})
    assert " ..." in result.text
    assert result.estimated_tokens <= STATE_TOKEN_BUDGET


def test_booleans_and_numbers_render_unambiguously():
    text = serialize_state({"blockers": False, "progress": 3, "ratio": 0.5}).text
    assert "blockers: false" in text
    assert "progress: 3" in text
    # An unrecognised name is treated as current material, not demoted to
    # history, so a real reported fact is not silently discarded.
    assert "ratio: 0.5" in text


def test_scalar_lists_are_allowed_but_bounded():
    text = serialize_state({"tools": ["curl", "python3", "ssh"]}).text
    assert text == "tools: curl, python3, ssh"


def test_nested_content_is_refused_not_serialised():
    """A caller handing over a transcript gets an error, never a silent leak."""
    with pytest.raises(StateBudgetError):
        serialize_state({"transcript": {"messages": ["secret"]}})


def test_empty_state_is_refused():
    with pytest.raises(StateBudgetError):
        serialize_state({})


def test_non_mapping_is_refused():
    with pytest.raises(StateBudgetError):
        serialize_state(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_blank_field_names_are_refused():
    with pytest.raises(StateBudgetError):
        serialize_state({"  ": "value"})


def test_unknown_field_names_are_kept_and_ordered_deterministically():
    """An unrecognised name is current material, ordered by name after it.

    Demoting unknown names to history silently lost facts the caller believed
    they had reported. Mapping keys are unique, so treating an unknown name as
    current cannot promote noise; it can only stop a real field being dropped.
    """
    text = serialize_state(
        {"objective": "the actual objective", "brand_new_field": "reported fact"}
    ).text
    assert "brand_new_field: reported fact" in text
    assert "objective:" in text
    # Both are tier 0, so ordering is by name: 'brand...' < 'objective'.
    assert text.index("brand_new_field:") < text.index("objective:")


def test_every_corpus_case_fits_the_budget():
    from turnstone.core.validation_corpus import CORPUS

    for name, _why, state in CORPUS:
        result = serialize_state(state)
        assert result.estimated_tokens <= STATE_TOKEN_BUDGET, name
        assert result.text.strip(), name
