"""Nonce-delimited fences for trust boundaries at the LLM wire.

A *fence* wraps a span of content in ``[start {tag}_{nonce}] ... [end
{tag}_{nonce}]`` markers whose nonce an adversary cannot reproduce, and
neutralises any literal marker in adjacent untrusted text so a leaked or guessed
nonce alone cannot forge or break the boundary; the trusted fences also remove
their token itself from that text (:func:`remove_token`).  One mechanism, three
trust boundaries (two polarities):

* **Output-guard judge** (:mod:`turnstone.core.output_guard_judge`) wraps
  UNTRUSTED tool output before handing it to the judge LLM.  The nonce stops
  that content from breaking *out* of the fence; the judge's system prompt
  declares the fence *form* (``[start tool_output_NONCE]``) as untrusted data,
  so a fresh per-call nonce is enough.

* **Operator fold** (``lowering.fold_system_turns``) wraps TRUSTED operator
  instructions folded into a neighbouring turn for models without native
  mid-conversation system messages.  The nonce stops untrusted host text from
  forging a *fake* trusted block; the system prompt
  (:func:`turnstone.prompts.build_operator_instruction_declaration`) declares
  the fence *exact value* as the sole trusted marker, so the nonce must live in
  the (cached) system prefix — minted once per session, not per fold.

* **Sender label** (``ChatSession._inject_sender_labels``) wraps the TRUSTED
  per-turn ``message from <sender>`` attribution on a shared workstream.  The
  nonce stops one participant from typing a look-alike marker in their own
  message to forge another sender's attribution; the declaration
  (:func:`turnstone.prompts.build_shared_workstream_declaration`) names the
  exact value as the sole authentic label, so the nonce lives in the (cached)
  system prefix like the operator fold — minted once per session.

The marker shape is bracketed ``start``/``end`` keywords rather than the prior
``<{tag}_{nonce}>`` XML form: angle-bracket markup pushed some local models out
of distribution and toward emitting their own turn-structure tokens.  The chat
templates most at risk are the ones built around rigid ``<...>``-style
structural tokens, so a fold marker that resembles them derails the template
once a few accumulate.  ``start``/``end`` carry no slash — no ``</`` or ``[/``
closing-tag shape — and read as ordinary text the model has seen everywhere.

The lifecycle difference (per-call form vs. per-session value) belongs to the
callers; the mint / neutralise / wrap mechanism is shared here so the two
boundaries cannot drift in nonce width or escaping.  They did drift once — the
operator path had regressed to a 32-bit, no-escape nonce while the judge used a
64-bit per-call nonce with closing-tag escaping — which is the divergence this
module exists to prevent.
"""

from __future__ import annotations

import bisect
import functools
import re
import secrets
import unicodedata
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

# Tag bases for the three fence kinds.  Kept distinct so the trust declarations
# never cross-contaminate: ``tool_output`` content is declared UNTRUSTED (to the
# judge), while ``system-reminder`` (operator instructions) and ``sender-label``
# (per-turn shared-workstream attribution) are each declared TRUSTED to the
# assistant — but under separate declarations, so a forged label can never claim
# operator authority nor vice versa.  A shared tag would let one declaration's
# semantics bleed onto the other's markers.
TOOL_OUTPUT_TAG: Final = "tool_output"
SYSTEM_REMINDER_TAG: Final = "system-reminder"
SENDER_LABEL_TAG: Final = "sender-label"

# 8 bytes → 16 hex chars → 64 bits.  An adversary whose payload is fixed before
# the nonce is minted cannot guess it; and because the fold path also
# neutralises markers in the untrusted host text and removes the nonce itself
# from it (see :func:`neutralize`, :func:`remove_token`), even a mid-session
# leak of the reused per-session operator nonce cannot be turned into a forged
# block.  Matching the judge's width here is the point: one constant, no
# per-caller drift.
_NONCE_BYTES: Final = 8

# Open / close keywords for the bracketed marker — ``[start {tag}_{nonce}]`` /
# ``[end {tag}_{nonce}]``.  Slash-free by design (see the module docstring): the
# open/close discriminator is the keyword, not a ``/`` that would re-create the
# closing-tag shape that derails those models' chat templates.  Both fence
# kinds share these, so the detection regexes here and in
# ``output_guard._RE_FENCE_MARKER`` (built via :func:`detection_pattern`) cannot
# drift from what :func:`wrap` emits.
_OPEN_KW: Final = "start"
_CLOSE_KW: Final = "end"


def mint_nonce() -> str:
    """Mint a 64-bit unguessable hex nonce for a fence tag."""
    return secrets.token_hex(_NONCE_BYTES)


def _marker_pattern(tag: str, *, opening: bool) -> re.Pattern[str]:
    """Compile the marker pattern for *tag*.

    ``[end tag`` (closing) is always matched — that is how content breaks *out*
    of a fence wrapping it.  ``[start tag`` (opening) is matched too when
    *opening* is set — that is how surrounding text forges a fake fence to break
    *in*.

    The single capture spans the run between ``[`` and the tag — optional
    whitespace, the ``start``/``end`` keyword, the separating whitespace — so the
    defang (a backslash right after ``[``) lands in front of the keyword and the
    marker no longer matches.  Built from the same ``start``/``end`` keywords as
    :func:`wrap` and :func:`detection_pattern`, so a marker can never be
    emitted-but-not-detected.  Only the tag *prefix* is anchored, so a nonce
    suffix (``[start system-reminder_abcd]``) is matched and defanged regardless
    of whether the hex matches the real nonce.
    """
    kw = rf"(?:{_OPEN_KW}|{_CLOSE_KW})" if opening else _CLOSE_KW
    return re.compile(rf"\[(\s*{kw}\s+){re.escape(tag)}", re.IGNORECASE)


def neutralize(text: str, tag: str, *, opening: bool = False) -> str:
    """Defang literal fence markers for *tag* in untrusted *text*.

    Inserts a backslash after ``[`` (``[\\end tag`` / ``[\\start tag``) so the
    sequence stays human-readable in logs but no longer matches the fence's
    open/close marker — even if the adversary has learned the nonce.
    Idempotent: an already-defanged ``[\\end tag`` is not re-matched.

    By default only the *closing* marker is neutralised (break-out defence, for
    a fence wrapping untrusted content).  Pass ``opening=True`` to also
    neutralise the *opening* marker (forge-in defence, for untrusted text that
    surrounds a trusted fence).
    """
    if "[" not in text:
        return text
    pattern = _marker_pattern(tag, opening=opening)
    return pattern.sub(lambda m: f"[\\{m.group(1)}{tag}", text)


# Code points outside ASCII whose compatibility decomposition (NFKD), lowercased,
# holds an ASCII letter or digit: accented letters (``é`` reads ``e``, as ``e``
# followed by a combining acute does), full-width and mathematical letters and
# digits, circled, parenthesized, superscript and subscript forms, ligatures,
# squared units and the like.  Unicode 16's; a test rebuilds the set from
# ``unicodedata``.
_LOOKALIKE_RANGES: Final = (
    (0x00AA, 0x00AA),
    (0x00B2, 0x00B3),
    (0x00B9, 0x00BA),
    (0x00BC, 0x00BE),
    (0x00C0, 0x00C5),
    (0x00C7, 0x00CF),
    (0x00D1, 0x00D6),
    (0x00D9, 0x00DD),
    (0x00E0, 0x00E5),
    (0x00E7, 0x00EF),
    (0x00F1, 0x00F6),
    (0x00F9, 0x00FD),
    (0x00FF, 0x010F),
    (0x0112, 0x0125),
    (0x0128, 0x0130),
    (0x0132, 0x0137),
    (0x0139, 0x0140),
    (0x0143, 0x0149),
    (0x014C, 0x0151),
    (0x0154, 0x0165),
    (0x0168, 0x017F),
    (0x01A0, 0x01A1),
    (0x01AF, 0x01B0),
    (0x01C4, 0x01DC),
    (0x01DE, 0x01E1),
    (0x01E6, 0x01ED),
    (0x01F0, 0x01F5),
    (0x01F8, 0x01FB),
    (0x0200, 0x021B),
    (0x021E, 0x021F),
    (0x0226, 0x0233),
    (0x02B0, 0x02B0),
    (0x02B2, 0x02B3),
    (0x02B7, 0x02B8),
    (0x02E1, 0x02E3),
    (0x1D2C, 0x1D2C),
    (0x1D2E, 0x1D2E),
    (0x1D30, 0x1D31),
    (0x1D33, 0x1D3A),
    (0x1D3C, 0x1D3C),
    (0x1D3E, 0x1D43),
    (0x1D47, 0x1D49),
    (0x1D4D, 0x1D4D),
    (0x1D4F, 0x1D50),
    (0x1D52, 0x1D52),
    (0x1D56, 0x1D58),
    (0x1D5B, 0x1D5B),
    (0x1D62, 0x1D65),
    (0x1D9C, 0x1D9C),
    (0x1DA0, 0x1DA0),
    (0x1DBB, 0x1DBB),
    (0x1E00, 0x1E9B),
    (0x1EA0, 0x1EF9),
    (0x2070, 0x2071),
    (0x2074, 0x2079),
    (0x207F, 0x2089),
    (0x2090, 0x2093),
    (0x2095, 0x209C),
    (0x20A8, 0x20A8),
    (0x2100, 0x2103),
    (0x2105, 0x2106),
    (0x2109, 0x210E),
    (0x2110, 0x2113),
    (0x2115, 0x2116),
    (0x2119, 0x211D),
    (0x2120, 0x2122),
    (0x2124, 0x2124),
    (0x2128, 0x2128),
    (0x212A, 0x212D),
    (0x212F, 0x2131),
    (0x2133, 0x2134),
    (0x2139, 0x2139),
    (0x213B, 0x213B),
    (0x2145, 0x2149),
    (0x2150, 0x217F),
    (0x2189, 0x2189),
    (0x2460, 0x24EA),
    (0x2C7C, 0x2C7D),
    (0x3250, 0x325F),
    (0x32B1, 0x32CF),
    (0x3358, 0x337A),
    (0x3380, 0x33FF),
    (0xA7F2, 0xA7F4),
    (0xFB00, 0xFB06),
    (0xFF10, 0xFF19),
    (0xFF21, 0xFF3A),
    (0xFF41, 0xFF5A),
    (0x107A5, 0x107A5),
    (0x1CCD6, 0x1CCF9),
    (0x1D400, 0x1D454),
    (0x1D456, 0x1D49C),
    (0x1D49E, 0x1D49F),
    (0x1D4A2, 0x1D4A2),
    (0x1D4A5, 0x1D4A6),
    (0x1D4A9, 0x1D4AC),
    (0x1D4AE, 0x1D4B9),
    (0x1D4BB, 0x1D4BB),
    (0x1D4BD, 0x1D4C3),
    (0x1D4C5, 0x1D505),
    (0x1D507, 0x1D50A),
    (0x1D50D, 0x1D514),
    (0x1D516, 0x1D51C),
    (0x1D51E, 0x1D539),
    (0x1D53B, 0x1D53E),
    (0x1D540, 0x1D544),
    (0x1D546, 0x1D546),
    (0x1D54A, 0x1D550),
    (0x1D552, 0x1D6A3),
    (0x1D7CE, 0x1D7FF),
    (0x1F100, 0x1F10A),
    (0x1F110, 0x1F12E),
    (0x1F130, 0x1F14F),
    (0x1F16A, 0x1F16C),
    (0x1F190, 0x1F190),
    (0x1FBF0, 0x1FBF9),
)


def _letters_and_digits(text: str) -> str:
    """The ASCII letters and digits *text*'s compatibility decomposition holds, lowercased."""
    folded = unicodedata.normalize("NFKD", text).lower()
    return "".join(char for char in folded if char.isascii() and char.isalnum())


# How each of those code points reads, in ASCII letters and digits.  One that
# the running interpreter's Unicode does not assign yet reads as nothing and is
# left out (Python 3.13 has Unicode 15.1, without the outlined letters and
# digits at U+1CCD6-U+1CCF9), so a view never loses a character.
_LOOKALIKES: Final = {
    code: letters
    for code, letters in (
        (code, _letters_and_digits(chr(code)))
        for first, last in _LOOKALIKE_RANGES
        for code in range(first, last + 1)
    )
    if letters
}


def _character_class(codes: Iterable[tuple[int, int]]) -> str:
    """A pattern matching one code point in any of the inclusive *codes* ranges."""
    return (
        "["
        + "".join(
            re.escape(chr(first))
            if first == last
            else f"{re.escape(chr(first))}-{re.escape(chr(last))}"
            for first, last in codes
        )
        + "]"
    )


# Whether a text can hold a lookalike: the table's ranges in the Basic
# Multilingual Plane, which the regular-expression engine keeps as a bitmap,
# and every code point past it, since listing the table's own ranges there
# would cost a range check per character of every text.
_LOOKALIKE_GATE: Final = re.compile(
    _character_class(
        [(first, last) for first, last in _LOOKALIKE_RANGES if last <= 0xFFFF]
        + [(0x10000, 0x10FFFF)]
    )
)
# The lookalikes that read as several characters (U+2469, a circled ten, reads
# ``10``): the only places where a view is longer than its text.
_EXPANDING: Final = re.compile(
    _character_class((code, code) for code, letters in _LOOKALIKES.items() if len(letters) > 1)
)

# What a session token removed from untrusted text reads as.  Session tokens
# are hex (:func:`mint_nonce`), and this starts and ends with letters that are
# not, so a token's characters beside it cannot join it into a new copy.
TOKEN_PLACEHOLDER: Final = "[removed session token]"

# Between a token's characters a match allows anything that is not an ASCII
# letter or digit: a space, punctuation, a format or other invisible character
# (zero-width spaces, variation selectors, fillers), a combining mark.
_TOKEN_GAP: Final = "[^0-9a-z]*"


def _token_view(text: str) -> str:
    """*text* lowercased, with each lookalike of a letter or digit in ASCII.

    The same length as *text* unless a lookalike spells several characters
    (U+2469, a circled ten, reads ``10``): lowercasing lengthens one code point,
    U+0130, and the table translates it first.  Text the gate finds no
    lookalike in skips the table, the one step that costs a lookup per
    character.
    """
    if not text.isascii() and _LOOKALIKE_GATE.search(text) is not None:
        text = text.translate(_LOOKALIKES)
    return text.lower()


@functools.lru_cache(maxsize=1024)
def _token_pattern(token: str) -> re.Pattern[str]:
    """The pattern that finds *token* in a :func:`_token_view`."""
    if not (token.isascii() and token.isalnum()):
        raise ValueError("a session token is ASCII letters and digits")
    return re.compile(_TOKEN_GAP.join(token.lower()))


def contains_token(text: str, *tokens: str) -> bool:
    """Whether any of *tokens* appears in *text*, read as a person reads it.

    A token matches past case, past accents and compatibility forms of its
    letters and digits (``é``, full-width, mathematical, circled), and past
    anything between its characters that is not an ASCII letter or digit:
    spaces, line breaks, punctuation, invisible characters, combining marks.
    Lookalikes that Unicode does not decompose to a letter or digit are not
    folded: letters from other scripts (a Cyrillic ``а`` for ``a``), ASCII
    ``O`` for ``0`` and ``l`` or ``I`` for ``1``, small capitals, and the
    dingbat and double circled digits; on Python 3.13, whose Unicode predates
    them, neither are the outlined letters and digits Unicode 16 added.  An
    empty token never matches.  The search is not constant-time; nothing that
    supplies *text* observes how long it takes.
    """
    view: str | None = None
    for token in tokens:
        if not token:
            continue
        if view is None:
            view = _token_view(text)
        if _token_pattern(token).search(view) is not None:
            return True
    return False


def remove_token(text: str, *tokens: str) -> str:
    """Replace each occurrence of any of *tokens* in *text* with :data:`TOKEN_PLACEHOLDER`.

    Occurrences are found as :func:`contains_token` finds them, and each
    replacement spans an occurrence from its first character to its last;
    overlapping occurrences of different tokens become one.  The placeholder
    keeps the line breaks of the span it replaces, so the lines after it keep
    their numbers.  The operator fold, the sender-label pass and attachment
    preparation run this over untrusted text, so a block that carries the
    session's token is one the framework wrote, whatever marker spelling
    surrounds a leaked copy.  Text without an occurrence is returned as is.
    """
    patterns = [_token_pattern(token) for token in tokens if token]
    if not patterns:
        return text
    view = _token_view(text)
    spans = sorted(match.span() for pattern in patterns for match in pattern.finditer(view))
    if not spans:
        return text
    if len(view) != len(text):
        spans = _text_spans(text, spans)
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    pieces: list[str] = []
    last = 0
    for start, end in merged:
        pieces.append(text[last:start])
        pieces.append(TOKEN_PLACEHOLDER + "\n" * text.count("\n", start, end))
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def _text_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Map *spans* of :func:`_token_view`'s view of *text* back onto *text*.

    The view is longer than *text* only at lookalikes that read as several
    characters, and no lookalike reads as nothing, so a view position maps back
    through the ones before it.
    """
    starts: list[int] = []  # where each such lookalike's letters start in the view
    ends: list[int] = []  # where they end
    indexes: list[int] = []  # where the lookalike is in the text
    shifts: list[int] = []  # how far the view runs ahead of the text after it
    shift = 0
    # A position in the text is never past its position in the view, so the
    # lookalikes from the last span's end in the view onward map nothing.
    for match in _EXPANDING.finditer(text, 0, max(end for _, end in spans)):
        index = match.start()
        width = len(_LOOKALIKES[ord(match.group())])
        starts.append(index + shift)
        ends.append(index + shift + width)
        indexes.append(index)
        shift += width - 1
        shifts.append(shift)

    def origin(position: int) -> int:
        before = bisect.bisect_right(starts, position) - 1
        if before < 0:
            return position
        if position < ends[before]:
            return indexes[before]
        return position - shifts[before]

    return [(origin(start), origin(end - 1) + 1) for start, end in spans]


def wrap(content: str, nonce: str, tag: str) -> str:
    """Wrap *content* in a ``[start {tag}_{nonce}] ... [end {tag}_{nonce}]`` fence.

    The body's *closing* marker is neutralised first so content cannot break
    out of the fence even if it knows the nonce.  Forge-in defence
    (neutralising the *opening* marker in the untrusted text that *surrounds*
    the fence) is the caller's job via :func:`neutralize` with ``opening=True``
    — needed by both the operator fold (untrusted host text around a trusted
    fold) and the sender-label fence (a participant's own message content
    surrounding their authentic label); the judge fence is the exception,
    wrapping a standalone message with no untrusted host to defend.
    """
    body = neutralize(content, tag)
    return f"[{_OPEN_KW} {tag}_{nonce}]\n{body}\n[{_CLOSE_KW} {tag}_{nonce}]"


def detection_pattern(tags: Iterable[str]) -> re.Pattern[str]:
    """Compile an open-or-close marker detector for any of *tags*.

    Single source for the marker *shape* used by forgery scanning
    (``output_guard._RE_FENCE_MARKER``), so a detector cannot drift from what
    :func:`wrap` emits.  Matches either the ``start`` or the ``end`` keyword
    form.  Only the tag prefix is anchored, so a marker is caught whether or not
    a nonce follows it; whether text holds a real token is
    :func:`contains_token`'s question.

    Raises ``ValueError`` on an empty tag set: an empty alternation would compile
    to ``(?:)`` and match *any* ``[start …]`` / ``[end …]`` run, turning the
    forgery scanner into a false-positive generator.
    """
    alt = "|".join(re.escape(t) for t in tags if t)
    if not alt:
        raise ValueError("detection_pattern requires at least one non-empty tag")
    return re.compile(
        rf"\[\s*(?:{_OPEN_KW}|{_CLOSE_KW})\s+(?:{alt})",
        re.IGNORECASE,
    )
