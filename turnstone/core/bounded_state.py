"""Bounded, deterministic state serialization for the advisory sensor.

This module turns a workstream state into the ``state`` string sent to
``switchyard-smartfree-aux-turnstone``. It exists because the fallback backend
silently truncates: measured live, Laya clamps at exactly 512 state tokens per
question and discards the excess with no error, keeping the head and dropping
the tail. A long history of routine text therefore buries the current objective
and is then read as a low-complexity task — a wrong observation rather than a
visible failure.

So the bound is enforced HERE, in tokens, by a deterministic reduction that
decides what to keep. It never asks a model to summarise, never invents a
conclusion, and never fails a real task because the state was long.

Three properties this is built to guarantee, each with a test:

* **Deterministic.** The same input always produces the same string. Priority is
  fixed, ordering is fixed, deduplication is fixed. No clocks, no randomness,
  no dict-iteration order leaking into the output.
* **Bounded.** The result always fits ``STATE_TOKEN_BUDGET``. Overflow is
  reduced, never raised.
* **Honest.** Reduction is reported, so a consumer can tell a bounded
  observation from a complete one.

The token budget is enforced through a deliberately conservative characters-per-
token ratio rather than a bundled tokenizer. The ratio is calibrated against the
live service (see LAYA-TOKEN-BUDGET.md); it over-estimates token count, so the
real backend always sees something smaller than this module claims. That
direction is deliberate: an over-estimate costs a little headroom, an
under-estimate costs a silently misread task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: Version of the bounded reduction policy. Recorded on every observation so a
#: stored snapshot can be interpreted against the policy that produced it.
SERIALIZER_VERSION = "turnstone-bounded-state:v1"

#: Conservative characters-per-token ratio, calibrated against the live English
#: Laya checkpoint. Measured samples ran ~4.4-4.7 chars/token; 3.6 leaves real
#: headroom for tokenizers that segment more finely, and the direction of the
#: error is the safe one: we reserve more than we need.
CHARS_PER_TOKEN = 3.6

#: State-token budget for one request. Half the measured 512-token
#: per-question ceiling, leaving room for question text, framing, tokenizer
#: variation and small future schema additions. Deliberately not the maximum.
STATE_TOKEN_BUDGET = 256

#: The character ceiling implied by the budget. Derived, never authored, so the
#: two cannot drift apart.
STATE_CHAR_BUDGET = int(STATE_TOKEN_BUDGET * CHARS_PER_TOKEN)  # 921

#: Per-field caps. A single field may never crowd out the whole budget.
MAX_FIELD_CHARS = 240
MAX_FIELDS = 40

#: Budget withheld from the history and noise tiers so that current material —
#: the objective, the active failure, the present state — is never displaced by
#: a long tail of low-value text. Half the budget, which is generous for the two
#: current/recent tiers while still refusing to let a clipped 240-character
#: history field dominate the observation.
LOW_TIER_RESERVE = STATE_TOKEN_BUDGET // 2

#: Retention tiers, highest first. A field in a lower tier is dropped before a
#: field in a higher tier. These are the categories the operator named: the
#: current objective and active blockers survive; repetition does not.
TIER_CURRENT = 0   # objective, current operational state, active failures
TIER_RECENT = 1    # latest material evidence, delegation/tool state
TIER_HISTORY = 2   # completed events, superseded facts
TIER_NOISE = 3     # duplicates, low-value verbosity

#: Tier for a field name we do not recognise.
#:
#: Not TIER_HISTORY. An unknown name is given the benefit of the doubt and
#: treated as current material, because demoting it silently loses a fact the
#: caller believed they were reporting. A mapping key is unique, so this cannot
#: promote noise; it can only prevent a real field from being discarded. The
#: budget still bounds how much survives.
TIER_UNKNOWN = TIER_CURRENT

_PRIORITY: dict[str, int] = {
    "objective": TIER_CURRENT,
    "current_state": TIER_CURRENT,
    "blockers": TIER_CURRENT,
    "failures": TIER_CURRENT,
    "delegation": TIER_RECENT,
    "tools": TIER_RECENT,
    "evidence": TIER_RECENT,
    "progress": TIER_RECENT,
    "history": TIER_HISTORY,
    "completed": TIER_HISTORY,
    "notes": TIER_HISTORY,
    "filler": TIER_NOISE,
    "chatter": TIER_NOISE,
}

_WHITESPACE = re.compile(r"\s+")


class StateBudgetError(ValueError):
    """The state could not be reduced into a usable observation."""


def _normalise(text: str) -> str:
    """Collapse whitespace so equivalent states serialise identically."""
    return _WHITESPACE.sub(" ", text).strip()


def _priority(field_name: str) -> int:
    """Retention tier for a field. Unknown names are treated as current."""
    return _PRIORITY.get(field_name.strip().lower(), TIER_UNKNOWN)


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return _normalise(value)
    if isinstance(value, (list, tuple)):
        # A short list of scalars is legitimate delegation/tool state.
        return ", ".join(_render_scalar(item) for item in value[:8])
    raise StateBudgetError(
        f"sensor state accepts only bounded scalars and short scalar lists, "
        f"not {type(value).__name__}"
    )


def _clip(value: str, limit: int) -> tuple[str, bool]:
    """Clip on a word boundary where possible. Returns (text, was_clipped)."""
    if len(value) <= limit:
        return value, False
    window = value[:limit]
    cut = window.rfind(" ")
    if cut >= limit // 2:
        window = window[:cut]
    return window.rstrip(" ,;:-") + " ...", True


@dataclass(frozen=True)
class SerializedState:
    """A bounded state string plus the provenance needed to interpret it."""

    text: str
    serializer_version: str = SERIALIZER_VERSION
    serialized_chars: int = 0
    estimated_tokens: int = 0
    #: True when ANY field was dropped, for any reason. This is the umbrella
    #: flag; read ``evicted_by_tier`` / ``overflowed_budget`` for meaning.
    state_truncated: bool = False
    #: A lower-tier field (history/noise) was dropped because higher-tier current
    #: material was present or the low-tier reserve had to be protected. This is
    #: the policy working as designed, NOT capacity pressure.
    evicted_by_tier: bool = False
    #: A field was dropped because the token budget was exhausted, i.e. the
    #: serializer had to make room. This is the capacity-pressure signal and the
    #: one worth alerting on.
    overflowed_budget: bool = False
    fields_in: int = 0
    fields_kept: int = 0
    fields_dropped: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Provenance for storage. Deliberately excludes the state text itself.

        A stored observation should be enough to evaluate the sensor later
        without duplicating private workstream content.
        """
        return {
            "serializer_version": self.serializer_version,
            "serialized_chars": self.serialized_chars,
            "serialized_tokens": self.estimated_tokens,
            "state_reduced": self.state_truncated,
            "evicted_by_tier": self.evicted_by_tier,
            "overflowed_budget": self.overflowed_budget,
            "fields_in": self.fields_in,
            "fields_kept": self.fields_kept,
            "fields_dropped": list(self.fields_dropped),
        }


def estimate_tokens(text: str) -> int:
    """Token estimate used for bounding. Over-estimates by design."""
    if not text:
        return 0
    # Integer ceiling on a float ratio, then cast: the return type is a count.
    return int(-(-len(text) // CHARS_PER_TOKEN))


def serialize_state(state: Mapping[str, Any]) -> SerializedState:
    """Serialise ``state`` into a bounded, deterministic decision-state string.

    The policy is fixed and testable:

    1. render each field, clipping to ``MAX_FIELD_CHARS`` on a word boundary;
    2. drop exact duplicates, keeping the first occurrence;
    3. order by retention tier, then by field name — both total orders, so the
       output cannot depend on mapping iteration order;
    4. admit fields in that order while the running estimate fits the budget;
    5. report what was dropped.

    It selects, orders, clips and deduplicates existing state. It does not
    infer, generate or summarise: no model is involved at any point.
    """
    if not isinstance(state, Mapping):
        raise StateBudgetError("state must be a mapping")

    lines: list[tuple[int, str, str]] = []

    for name in state:
        if not isinstance(name, str) or not name.strip():
            raise StateBudgetError("state field names must be non-empty strings")
        body = _clip(_render_scalar(state[name]), MAX_FIELD_CHARS)[0]
        lines.append((_priority(name), name, f"{name.strip()}: {body}"))

    # Fixed ordering: tier, then name. Never insertion order.
    lines.sort(key=lambda item: (item[0], item[1]))

    # Tier-aware admission with a RESERVE for high-tier material.
    #
    # A naive "admit while it fits" is wrong here in a way that only shows up on
    # realistic state: each field is clipped to MAX_FIELD_CHARS first, so a
    # huge history collapses to ~70 tokens and happily fits alongside the
    # objective, displacing the very facts a later turn needs. Measured on the
    # overflow case, `history` and `completed` survived while the objective was
    # crowded toward the end of the budget.
    #
    # So: high tiers are always admitted, and the remaining budget is shared
    # with lower tiers only while a reserve is preserved for the top tier. If
    # the objective or blockers are large, low-value history is dropped rather
    # than allowed to consume what the current state needs.
    by_tier: dict[int, list[tuple[int, str, str]]] = {}
    for item in lines:
        by_tier.setdefault(item[0], []).append(item)
    for group in by_tier.values():
        group.sort(key=lambda item: item[1])

    kept: list[tuple[int, str, str]] = []
    # Drops are attributed to a cause. Tier eviction is the policy selecting
    # current material; budget overflow is capacity pressure. Reporting both as
    # one "truncated" flag made a healthy serializer look permanently unhealthy,
    # so the two are counted separately and the umbrella is derived from them.
    evicted_by_tier: list[str] = []
    overflowed_budget: list[str] = []

    def used_tokens() -> int:
        return sum(estimate_tokens(line) + 1 for _t, _n, line in kept)

    # Pass 1: everything at the current/recent tiers is admitted.
    for tier in (TIER_CURRENT, TIER_RECENT):
        kept.extend(by_tier.get(tier, []))

    # Pass 2: lower tiers are admitted ONLY when there is no current material to
    # protect, and then only while a reserve remains. A reserve alone is not
    # enough: measured on the overflow case, the high tiers used ~52 tokens
    # while a clipped 240-character `history` field is ~71, so it still fit
    # under the reserve and crowded the observation. The operator's requirement
    # is that history and noise are the FIRST thing dropped, not merely the last
    # thing added.
    has_high_tier = any(item[0] <= TIER_RECENT for item in lines)
    for tier in (TIER_HISTORY, TIER_NOISE):
        for item in by_tier.get(tier, []):
            if has_high_tier or used_tokens() > STATE_TOKEN_BUDGET - LOW_TIER_RESERVE:
                evicted_by_tier.append(item[1])
                continue
            kept.append(item)

    # Pass 3: a final eviction, lowest tier first, in case the high tiers alone
    # still exceed the budget. These drops are capacity pressure, not policy.
    kept.sort(key=lambda item: (item[0], item[1]))
    while used_tokens() > STATE_TOKEN_BUDGET and kept:
        worst = max(range(len(kept)), key=lambda i: (kept[i][0], kept[i][1]))
        _tier, name, _line = kept.pop(worst)
        overflowed_budget.append(name)
        kept.sort(key=lambda item: (item[0], item[1]))

    if not kept:
        raise StateBudgetError(
            "state reduced to nothing; no field fits the bounded budget"
        )

    dropped = evicted_by_tier + overflowed_budget
    text = "\n".join(line for _tier, _name, line in kept)
    return SerializedState(
        text=text,
        serialized_chars=len(text),
        estimated_tokens=estimate_tokens(text),
        state_truncated=bool(dropped),
        evicted_by_tier=bool(evicted_by_tier),
        overflowed_budget=bool(overflowed_budget),
        fields_in=len(state),
        fields_kept=len(kept),
        fields_dropped=tuple(sorted(set(dropped))),
    )
