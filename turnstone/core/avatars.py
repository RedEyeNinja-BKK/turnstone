"""User avatars: one initial on one of six colours, or an uploaded picture.

The page draws a person's avatar from a small descriptor the server sends
wherever it sends that person's name: the palette index, the initial, and the
version of an uploaded picture (or ``None``).  The six colours live in
``shared_static/base.css`` as ``--avatar-<n>-color`` / ``--avatar-<n>-tint``
per theme; this module decides which index a person gets and which letter
stands for them.  The initial is derived here, not in the page, so the
creation-time colour pick and every viewer agree on the letter.

An account stores its colour (``user_avatars.color``) once one is picked: at
creation, by :func:`assign_avatar_color`, or by the person choosing a swatch.
With nothing stored, :func:`hashed_color` derives one from the user id, so an
account made by a path that skips the pick still renders.
"""

from __future__ import annotations

import hashlib
import threading
import unicodedata
from io import BytesIO
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from PIL import Image, ImageFile

log = get_logger(__name__)

PALETTE_SIZE = 6

# Palette entries a viewer with protanopia, deuteranopia or tritanopia can't
# tell apart.  Entries are orange, green, teal, blue, violet and pink (hues 45,
# 140, 195, 255, 305, 355).  A pair counts as confusable when, after simulating
# the vision type (the Machado, Oliveira and Fernandes 2009 matrices at full
# severity, applied to linear sRGB), the letter colours sit within 0.10 and the
# tints within 0.05 of each other in OKLab, in either theme.
# tests/test_avatars.py recomputes this from the palette in base.css.
_CONFUSABLE: frozenset[frozenset[int]] = frozenset(
    frozenset(pair)
    for pair in (
        (0, 1),
        (0, 5),
        (1, 2),
        (1, 3),
        (1, 5),
        (2, 3),
        (2, 4),
        (2, 5),
        (3, 4),
        (4, 5),
    )
)

# Uploaded pictures are stored and served as a square PNG of this side.
PICTURE_SIZE = 256

# The largest upload, in bytes.
MAX_PICTURE_BYTES = 4 * 1024 * 1024

# The most pixels an upload may declare, per side.  A small compressed file can
# declare a huge bitmap, and a 256px avatar needs little.  PNG and WebP decode
# at full size, so they stop at 2048.  JPEG decodes at reduced scale (draft()
# in normalize_picture), so it may go to 4096, which still takes a
# 12-megapixel phone photo.  The upload dialog sends at most 512px; these
# limits are for direct API uploads.
_PICTURE_SIDE_LIMIT = 2048
_JPEG_SIDE_LIMIT = 4096

# The upload types an avatar accepts, by sniffed magic bytes, and the Pillow
# decoders that may read them.
_PICTURE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_PICTURE_FORMATS = ("PNG", "JPEG", "WEBP")

# 16-bit greyscale modes, which converting to 8 bits clips instead of scaling.
_WIDE_GREY_MODES = ("I", "I;16", "I;16B", "I;16L", "I;16N")

# Vowels that Thai, Lao, New Tai Lue and Tai Viet write before the consonant
# they follow in speech (Unicode's Logical_Order_Exception), and the spacing
# modifier letters such as the Hawaiian okina (U+02BB).  Letters, but not the
# one a reader takes for a name's initial.
_NOT_INITIALS = frozenset(
    (
        *range(0x0E40, 0x0E45),
        *range(0x0EC0, 0x0EC5),
        *range(0x19B5, 0x19B8),
        0x19BA,
        0xAAB5,
        0xAAB6,
        0xAAB9,
        0xAABB,
        0xAABC,
        *range(0x02B0, 0x0300),
    )
)

# Characters of the image's SHA-256 used as the picture version in descriptors
# and cache-busting URLs.
_VERSION_CHARS = 16

# One colour pick at a time per process.  Two accounts made together would
# otherwise both read everyone before either writes, and could take the same
# colour.  Picks in different processes stay best effort.
_PICK_LOCK = threading.Lock()


class PictureRejectedError(ValueError):
    """An upload that can't become an avatar.  The message is shown to the user."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _first_letter(text: str) -> str:
    for ch in unicodedata.normalize("NFKC", text or ""):
        if unicodedata.category(ch)[0] in ("L", "N") and ord(ch) not in _NOT_INITIALS:
            title = ch.title()
            return title if len(title) == 1 else ch
    return ""


def initial_for(display_name: str, username: str) -> str:
    """The letter that stands for a person: display name first, then username.

    The first letter or digit after NFKC, so accented letters and Hangul
    syllables are one code point and full-width or decorative letters read as
    their plain forms.  Vowels written before their consonant and modifier
    letters are skipped (:data:`_NOT_INITIALS`).  A code point rather than a
    grapheme: an Indic conjunct syllable is one grapheme but blurs at 16px.
    Titlecased only when the titlecase is still one character (sharp s stays),
    and without the viewer's locale, so every viewer sees the same letter.
    Titlecase is Unicode's form for a word's first letter: it keeps a Georgian
    letter in its ordinary form, where uppercase gives the all-capitals one.
    ``""`` when neither name has a letter or digit; the page then shows the
    colour alone.
    """
    return _first_letter(display_name) or _first_letter(username)


def hashed_color(user_id: str) -> int:
    """The colour an account gets when none is stored: SHA-256 of the id, mod 6."""
    digest = hashlib.sha256(user_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % PALETTE_SIZE


def effective_color(user_id: str, stored: Any) -> int:
    """The stored colour when it is a valid index, else the hashed one."""
    if isinstance(stored, int) and not isinstance(stored, bool) and 0 <= stored < PALETTE_SIZE:
        return stored
    return hashed_color(user_id)


def pick_color(user_id: str, initial: str, others: Iterable[tuple[str, int]]) -> int:
    """The colour for a new account, against ``(initial, colour)`` of everyone else.

    Among the people who share the initial, take the colour the fewest of them
    have; among equals, the one the fewest have a colour-blind look-alike of;
    then the colour nearest the hashed one going round the palette, so ties
    don't pile onto one entry.
    """
    hashed = hashed_color(user_id)
    same = [color for other_initial, color in others if other_initial == initial]

    def cost(color: int) -> tuple[int, int, int]:
        return (
            sum(1 for c in same if c == color),
            sum(1 for c in same if frozenset((c, color)) in _CONFUSABLE),
            (color - hashed) % PALETTE_SIZE,
        )

    return min(range(PALETTE_SIZE), key=cost)


def picture_version(image_sha256: Any) -> str | None:
    """The short version string of an uploaded picture, or ``None`` without one."""
    if isinstance(image_sha256, str) and image_sha256:
        return image_sha256[:_VERSION_CHARS]
    return None


def descriptor(
    user_id: str, display_name: str, username: str, avatar: dict[str, Any] | None
) -> dict[str, Any]:
    """What the page needs to draw a person: ``{"color", "initial", "picture"}``.

    ``avatar`` is the person's ``user_avatars`` row (``color``,
    ``image_sha256``) or ``None`` when there is none.
    """
    row = avatar or {}
    return {
        "color": effective_color(user_id, row.get("color")),
        "initial": initial_for(display_name, username),
        "picture": picture_version(row.get("image_sha256")),
    }


def descriptor_for_user(storage: Any, user: dict[str, Any]) -> dict[str, Any]:
    """The descriptor for one ``users`` row, reading its avatar row from storage."""
    user_id = str(user.get("user_id", ""))
    return descriptor(
        user_id,
        str(user.get("display_name", "") or ""),
        str(user.get("username", "") or ""),
        storage.get_user_avatar(user_id),
    )


def assign_avatar_color(storage: Any, user_id: str, display_name: str, username: str) -> None:
    """Store the colour that best keeps a new account apart from people with its initial.

    Called after an account is created.  Best effort: on any failure the
    account keeps its hashed colour, which is a valid colour, just not a picked
    one.
    """
    try:
        initial = initial_for(display_name, username)
        with _PICK_LOCK:
            avatars = storage.list_user_avatars()
            others = [
                (
                    initial_for(
                        str(u.get("display_name", "") or ""), str(u.get("username", "") or "")
                    ),
                    effective_color(u["user_id"], (avatars.get(u["user_id"]) or {}).get("color")),
                )
                for u in storage.list_users()
                if u.get("user_id") != user_id
            ]
            storage.set_user_avatar_color(user_id, pick_color(user_id, initial, others))
    except Exception:
        log.warning(
            "Avatar colour not picked for user %s; the hashed colour applies",
            user_id,
            exc_info=True,
        )


def _to_8_bit_grey(img: Image.Image) -> Image.Image:
    """A 16-bit greyscale picture scaled to 8 bits, its colour key kept exact.

    Converting straight to 8 bits would clip the samples instead.  A colour key
    marks one 16-bit value, so it becomes an alpha mask read from the 16-bit
    samples: scaled down to 8 bits, it would also take every neighbouring value.
    """
    from PIL import ImageMath

    wide = img.convert("I")
    grey = wide.point(lambda v: v * (1 / 256)).convert("L")
    key = img.info.get("transparency")
    if isinstance(key, int):
        mask = ImageMath.lambda_eval(lambda a: (a["i"] != key) * 255, i=wide).convert("L")
        grey.info.pop("transparency", None)
        grey.putalpha(mask)
    return grey


def _match_colour_key(img: ImageFile.ImageFile) -> None:
    """Bring a PNG colour key to the scale Pillow decodes the samples at.

    Pillow widens 2- and 4-bit grey samples to 8 bits and keeps only the high
    byte of 16-bit RGB samples, but keeps the key as the file states it, where
    it would match the wrong pixels.  16-bit grey is handled by
    :func:`_to_8_bit_grey`.
    """
    key = img.info.get("transparency")
    if img.format != "PNG" or key is None or not img.tile:
        return
    rawmode = img.tile[0].args
    if rawmode == "L;2" and isinstance(key, int):
        img.info["transparency"] = key * 85
    elif rawmode == "L;4" and isinstance(key, int):
        img.info["transparency"] = key * 17
    elif rawmode == "RGB;16B" and isinstance(key, tuple):
        img.info["transparency"] = tuple(component >> 8 for component in key)


def normalize_picture(data: bytes) -> tuple[bytes, str]:
    """Turn an upload into the stored picture: ``(png_bytes, sha256_hex)``.

    Accepts PNG, JPEG and WebP, sniffed by magic bytes, up to
    :data:`MAX_PICTURE_BYTES` and the per-format pixel limits.  Crops the
    centre square, scales it to :data:`PICTURE_SIZE`, applies the EXIF
    orientation and writes a fresh PNG from the raw pixels, so nothing else
    from the upload (camera details, GPS position, colour profile) reaches
    storage.  The pixels are not converted to sRGB first, so a picture tagged
    with a wider colour space such as Display P3 comes out a little duller; the
    upload dialog converts to sRGB before it sends.  Raises
    :class:`PictureRejectedError` with a message for the user.  CPU-bound:
    callers run it off the event loop.
    """
    from turnstone.core.attachments import sniff_image_mime
    from turnstone.core.images import (
        MAX_JPEG_SCANS,
        exif_orientation,
        too_many_jpeg_scans,
        turn_upright,
    )

    if len(data) > MAX_PICTURE_BYTES:
        raise PictureRejectedError(
            f"The picture is over {MAX_PICTURE_BYTES // (1024 * 1024)} MB. Choose a smaller one.",
            status_code=413,
        )
    mime = sniff_image_mime(data)
    if mime not in _PICTURE_MIMES:
        raise PictureRejectedError("Use a PNG, JPEG or WebP image.")
    if too_many_jpeg_scans(data):
        raise PictureRejectedError(
            f"That JPEG is saved in more than {MAX_JPEG_SCANS} passes, which takes too long "
            "to read. Save it again as an ordinary JPEG or as a PNG."
        )
    try:
        from PIL import Image, JpegImagePlugin
    except ImportError as exc:  # pragma: no cover - declared dependency; defensive
        raise PictureRejectedError("Pictures can't be processed on this server.", 503) from exc
    too_many = PictureRejectedError(
        f"The picture has too many pixels (the limit is {_JPEG_SIDE_LIMIT} by "
        f"{_JPEG_SIDE_LIMIT} for JPEG, {_PICTURE_SIDE_LIMIT} by {_PICTURE_SIDE_LIMIT} "
        "for PNG and WebP). Choose a smaller one.",
        status_code=413,
    )
    try:
        opened = Image.open(BytesIO(data), formats=_PICTURE_FORMATS)
        # Multi-picture JPEGs open as a subclass of the JPEG plugin's class and
        # decode the same way.
        is_jpeg = isinstance(opened, JpegImagePlugin.JpegImageFile)
        side_limit = _JPEG_SIDE_LIMIT if is_jpeg else _PICTURE_SIDE_LIMIT
        width, height = opened.size
        # Check the header-declared size before anything decodes the pixels:
        # Pillow's own bomb check only raises above twice its limit.
        if max(width, height) > side_limit:
            raise too_many
        # Read where a browser does, before any decode.
        orientation = exif_orientation(opened)
        _match_colour_key(opened)
        if is_jpeg:
            # Decode at the smallest scale that keeps 512px on the short side.
            opened.draft("RGB", (PICTURE_SIZE * 2, PICTURE_SIZE * 2))
        img: Image.Image = opened
        if img.mode in _WIDE_GREY_MODES:
            img = _to_8_bit_grey(img)
        width, height = img.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        box = (left, top, left + side, top + side)
        size = (PICTURE_SIZE, PICTURE_SIZE)
        if img.mode in ("RGB", "L") and "transparency" not in img.info:
            # Opaque: scale straight from the centre square of the source, which
            # lets reducing_gap shrink it by whole factors first.
            square = img.resize(size, Image.Resampling.LANCZOS, box=box, reducing_gap=3.0)
            square = square.convert("RGBA")
        else:
            # RGBA at full size first, so a colour key becomes alpha before any
            # pixels blend, then scaled premultiplied, so transparent pixels lend
            # no colour to their neighbours.
            rgba = img if img.mode == "RGBA" else img.convert("RGBA")
            square = (
                rgba.convert("RGBa")
                .resize(size, Image.Resampling.LANCZOS, box=box, reducing_gap=3.0)
                .convert("RGBA")
            )
        # The centre square is the same region before and after any EXIF
        # rotation or flip, so the small result is turned, not the source.
        square = turn_upright(square, orientation)
        # A new image from the raw pixels carries no metadata from the upload.
        clean = Image.frombytes("RGBA", square.size, square.tobytes())
        buf = BytesIO()
        clean.save(buf, format="PNG")
    except PictureRejectedError:
        raise
    except Image.DecompressionBombError as exc:
        raise too_many from exc
    except Exception as exc:
        raise PictureRejectedError("That image could not be read.") from exc
    png = buf.getvalue()
    return png, hashlib.sha256(png).hexdigest()
