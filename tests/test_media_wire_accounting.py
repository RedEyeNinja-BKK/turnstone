"""Wire-time media accounting: the image payload budget and the calibration guard.

Regression cover for the 2026-09-25 context collapse (workstream
``bf25fe6afef94b4eb3c40401b96530c2``).  Measured facts, used verbatim below:

* the workstream's prompts ran ~54,015 tokens;
* the next call after two PNG attachments (1,981,209 + 897,811 bytes) was billed
  **888,062** prompt tokens — a single-call rise of 834,047 tokens for 2,879,020
  bytes, i.e. 4.60 base64 characters per token, so the data URL was tokenized as
  text rather than charged per image patch;
* the fixed 1,000-token-per-image charge subtracted only 2,000 of that, so the
  residual was attributed to the text and ``chars_per_token`` collapsed from
  ~4.6 to ~0.213;
* that collapsed ratio then divided a fixed-size system prefix of 141,315
  characters and produced a 660,352-token estimate, above the 219,932-token
  usable input budget — ``ModelAdmissionError``, workstream ``state=error``.

The two properties under test are therefore: the wire payload is bounded, and an
arithmetically self-inconsistent calibration sample is refused instead of
poisoning the ratio.
"""

from __future__ import annotations

import base64
import random
from io import BytesIO

import pytest

from turnstone.core.compaction import calibrated_chars_per_token
from turnstone.core.images import bound_image_for_wire

Image = pytest.importorskip("PIL.Image")

# -- Measured incident constants (see module docstring) -----------------------

INCIDENT_PROMPT_TOKENS = 888_062
INCIDENT_IMAGE_COUNT = 2
INCIDENT_TEXT_CHARS = 189_000
INCIDENT_SYSTEM_PREFIX_CHARS = 141_315
INCIDENT_USABLE_INPUT_TOKENS = 219_932
HEALTHY_RATIO = 4.0

_WIRE_BUDGET_BYTES = 96 * 1024
_INCIDENT_IMAGE_BYTES = 3 * 1024 * 1024  # PNG of noise; >1.5 MB like the real one


def _counts(text_chars: int, images: int) -> tuple[int, int, int]:
    return text_chars, images, 0


def _incident_measure(message: dict[str, int]) -> tuple[int, int, int]:
    return _counts(message["text_chars"], message["images"])


def _incident_messages() -> list[dict[str, int]]:
    """The measured shape: the two images plus the text the ratio was computed over."""
    return [{"text_chars": INCIDENT_TEXT_CHARS, "images": INCIDENT_IMAGE_COUNT}]


def _incident_naive_ratio() -> float:
    """What the old arithmetic returned: the whole image bill folded into the text."""
    return INCIDENT_TEXT_CHARS / (INCIDENT_PROMPT_TOKENS - INCIDENT_IMAGE_COUNT * 1000)


@pytest.fixture(scope="module")
def oversized_png() -> bytes:
    """A PNG of the incident's order of magnitude (irreducibly noisy, ~3 MB)."""
    rng = random.Random(20260925)
    data = rng.randbytes(_INCIDENT_IMAGE_BYTES)
    img = Image.frombytes("RGB", (1024, 1024), data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    out = buf.getvalue()
    assert len(out) > 1_500_000, "fixture must be large enough to exercise the bound"
    return out


# -- Calibration guard --------------------------------------------------------


def test_fixture_reproduces_the_measured_collapse() -> None:
    """Control: the incident numbers really do collapse the ratio to ~0.21.

    Without this the guard test below could pass vacuously.
    """
    naive = _incident_naive_ratio()
    assert 0.20 < naive < 0.22, f"expected the measured ~0.213 collapse, got {naive}"


def test_collapse_is_refused_and_fallback_retained() -> None:
    """The incident sample must not become the lane's ratio."""
    ratio = calibrated_chars_per_token(
        prompt_tokens=INCIDENT_PROMPT_TOKENS,
        messages=_incident_messages(),
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    assert ratio == HEALTHY_RATIO, "a self-inconsistent sample must retain the fallback"


def test_refused_collapse_keeps_the_system_prefix_admissible() -> None:
    """The outcome the incident lost: the fixed prefix must stay within capacity."""
    ratio = calibrated_chars_per_token(
        prompt_tokens=INCIDENT_PROMPT_TOKENS,
        messages=_incident_messages(),
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    prefix_tokens = INCIDENT_SYSTEM_PREFIX_CHARS / ratio
    assert prefix_tokens <= INCIDENT_USABLE_INPUT_TOKENS, (
        "the leading system prefix must remain admissible"
    )
    # And the defect is visible: the refused ratio would have exceeded capacity.
    assert INCIDENT_SYSTEM_PREFIX_CHARS / _incident_naive_ratio() > INCIDENT_USABLE_INPUT_TOKENS


def test_healthy_text_sample_still_calibrates() -> None:
    """Guard must not disable calibration for ordinary text turns."""
    ratio = calibrated_chars_per_token(
        prompt_tokens=50_000,
        messages=[{"text_chars": 230_000, "images": 0}],
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    assert ratio == pytest.approx(230_000 / 50_000)


def test_dense_script_sample_is_accepted() -> None:
    """Thai/CJK tokenize near one character per token; the band must allow that."""
    ratio = calibrated_chars_per_token(
        prompt_tokens=100_000,
        messages=[{"text_chars": 120_000, "images": 0}],
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    assert ratio == pytest.approx(1.2)


def test_implausibly_high_sample_is_refused() -> None:
    """An under-counting ratio is the dangerous direction; refuse it where the
    image charge is in play."""
    ratio = calibrated_chars_per_token(
        prompt_tokens=100_000,
        messages=[{"text_chars": 900_000, "images": 1}],
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    assert ratio == HEALTHY_RATIO


def test_text_only_sample_keeps_the_unchanged_arithmetic() -> None:
    """With no images the provider's count IS the text measurement.

    The band must not reach samples where the fixed per-image charge — the only
    term that can be wrong — was never subtracted.
    """
    ratio = calibrated_chars_per_token(
        prompt_tokens=30,
        messages=[{"text_chars": 1_315, "images": 0}],
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    assert ratio == pytest.approx(1_315 / 30)


def test_nonpositive_denominator_retains_fallback() -> None:
    ratio = calibrated_chars_per_token(
        prompt_tokens=1_500,
        messages=[{"text_chars": 12_000, "images": 2}],
        tool_def_chars=0,
        measure=_incident_measure,
        fallback=HEALTHY_RATIO,
    )
    assert ratio == HEALTHY_RATIO


# -- Wire payload budget ------------------------------------------------------


def test_oversized_image_is_bounded_within_the_wire_budget(oversized_png: bytes) -> None:
    out = bound_image_for_wire(oversized_png)
    assert len(out) <= _WIRE_BUDGET_BYTES, f"wire payload stayed {len(out)} bytes"


def test_bounded_image_still_decodes_at_a_usable_size(oversized_png: bytes) -> None:
    out = bound_image_for_wire(oversized_png)
    img = Image.open(BytesIO(out))
    img.load()
    assert max(img.size) <= 1568, "the bound must not upscale or exceed the edge cap"
    assert min(img.size) >= 256, "the bound must leave a usable image"


def test_bounding_reduces_the_payload_actually_billed(oversized_png: bytes) -> None:
    """The property the incident lost: base64 sent must be small enough to bill sanely."""
    out = bound_image_for_wire(oversized_png)
    unbounded_b64 = len(base64.b64encode(oversized_png))
    bounded_b64 = len(base64.b64encode(out))
    assert unbounded_b64 > 2_500_000, "control: the source payload really was the incident size"
    assert bounded_b64 < unbounded_b64
    # 4.60 base64 characters per token, the measured rate.
    assert bounded_b64 / 4.60 < 30_000, "one image must not be able to bill a whole window"


def test_small_image_passes_through_verbatim() -> None:
    buf = BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, format="JPEG")
    small = buf.getvalue()
    assert bound_image_for_wire(small) is small, "no re-encode when already affordable"


def test_bound_never_raises_on_undecodable_bytes() -> None:
    junk = b"\x00" * (200 * 1024)
    assert bound_image_for_wire(junk) == junk
