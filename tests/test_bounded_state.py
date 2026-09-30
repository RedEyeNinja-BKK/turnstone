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
    assert payload["state_truncated"] is True
    # Provenance must not duplicate the private state text.
    assert "text" not in payload
    assert "objective" not in str(payload)


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
