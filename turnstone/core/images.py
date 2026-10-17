"""Image-pixel utilities shared across the thumbnail, wire, and perception paths.

Currently: EXIF-orientation normalisation, the JPEG scan check that every
decoder of an uploaded image applies first, and the orientation reader they all
use instead of Pillow's EXIF parser.  Kept separate from
:mod:`turnstone.core.thumbnails` (which downscales for the UI) — this operates on
full-resolution bytes at the read/wire boundary so every consumer of an
attachment sees the same upright pixels.
"""

from __future__ import annotations

import hashlib
import re
import struct
import threading
from collections import OrderedDict
from io import BytesIO
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from PIL import Image

log = get_logger(__name__)

# EXIF tag 0x0112 (274) — image orientation (1 = upright; 2-8 = flips/rotations).
_EXIF_ORIENTATION_TAG = 0x0112
_TURNED_ORIENTATIONS = frozenset(range(2, 9))

# The ways a TIFF header (the start of an EXIF block) may begin, as Pillow
# accepts them, and the byte order each declares.
_TIFF_BYTE_ORDER = {b"II*\x00": "<", b"II\x00*": "<", b"MM\x00*": ">", b"MM*\x00": ">"}

# struct formats of the EXIF value types that fit inside a tag entry: BYTE,
# SHORT, LONG, SBYTE, SSHORT and SLONG.
_INLINE_VALUE_FORMATS = {1: "B", 3: "H", 4: "L", 6: "b", 8: "h", 9: "l"}

# Where XMP states the orientation, as Pillow reads it.
_XMP_ORIENTATION = r'tiff:Orientation(="|>)([0-9])'

# Mirror turnstone.core.thumbnails: bound decoded pixels so a small compressed
# file that expands to an enormous bitmap can't OOM the node during re-encode.
_MAX_IMAGE_PIXELS = 40_000_000

# A progressive JPEG may repeat scans, and decoding costs a pass over the whole
# picture per scan: a 1 MB file of repeated scans took 80 seconds to decode at
# 4096 by 4096, and decoding at a smaller scale doesn't help.  Encoders write
# about 10.  Counting FF DA (start of scan) marker pairs can only overcount,
# since byte stuffing keeps the pair out of compressed data, so the limit
# leaves room for metadata that happens to contain it.
MAX_JPEG_SCANS = 100

# How much normalize_image_orientation keeps of the answers that took a decode.
_NORMALIZED_BYTES = 64 * 1024 * 1024
_NORMALIZED_ENTRIES = 4096


def too_many_jpeg_scans(data: bytes) -> bool:
    """Whether ``data`` is a JPEG with more scans than an encoder writes.

    Callers refuse or skip such a file before anything decodes it.
    """
    return data.startswith(b"\xff\xd8\xff") and data.count(b"\xff\xda") > MAX_JPEG_SCANS


def exif_orientation(img: Image.Image) -> int:
    """The EXIF orientation an opened image declares: 1 is upright, 2-8 turn or flip.

    1 when the image declares none.  Reads only this tag, straight from the
    bytes, and callers turn the picture with :func:`turn_upright`, in place of
    ``getexif()`` and ``ImageOps.exif_transpose``: that transpose also rewrites
    the rest of the EXIF, and fails on a tag stored in a type other than the
    one Pillow expects.  A test keeps both calls out of turnstone/.  Looks where
    Pillow does: the EXIF block, else a PNG's "Raw profile type exif" text,
    else a TIFF's own tags, then XMP when none of those has the tag.  A PNG may
    store these after its pixels, so a caller that decodes the picture reads
    after decoding.
    """
    info = img.info
    exif = info.get("exif")
    raw_profile = info.get("Raw profile type exif")
    if exif is None and isinstance(raw_profile, str):
        try:
            exif = bytes.fromhex("".join(raw_profile.split("\n")[3:]))
        except ValueError:
            exif = None
    found: int | None = None
    if isinstance(exif, bytes):
        found = _tiff_orientation(exif)
    elif (tags := getattr(img, "tag_v2", None)) is not None:
        # A TIFF's tags were read when it was opened.
        value = tags.get(_EXIF_ORIENTATION_TAG)
        found = value if isinstance(value, int) else None
    if found is None:
        found = _xmp_orientation(info)
    return 1 if found is None else found


def _tiff_orientation(data: bytes) -> int | None:
    """The orientation tag in an EXIF block's first directory, or ``None``."""
    base = 0
    while data.startswith(b"Exif\x00\x00", base):
        base += 6
    order = _TIFF_BYTE_ORDER.get(data[base : base + 4])
    if order is None or len(data) < base + 8:
        return None
    (offset,) = struct.unpack_from(order + "L", data, base + 4)
    start = base + offset
    if len(data) < start + 2:
        return None
    (count,) = struct.unpack_from(order + "H", data, start)
    end = start + 2 + 12 * min(count, (len(data) - start - 2) // 12)
    found: int | None = None
    for tag, kind, n, value in struct.iter_unpack(
        order + "HHL4s", memoryview(data)[start + 2 : end]
    ):
        value_format = _INLINE_VALUE_FORMATS.get(kind)
        if tag == _EXIF_ORIENTATION_TAG and n == 1 and value_format is not None:
            # A repeated tag's last entry wins, as in Pillow.
            (found,) = struct.unpack_from(order + value_format, value)
    return found


def _xmp_orientation(info: Mapping[Any, Any]) -> int | None:
    xmp = info.get("XML:com.adobe.xmp")
    if isinstance(xmp, str) and xmp:
        found = re.search(_XMP_ORIENTATION, xmp)
        return int(found[2]) if found else None
    raw = info.get("xmp")
    if isinstance(raw, bytes) and raw:
        found_raw = re.search(_XMP_ORIENTATION.encode(), raw)
        return int(found_raw[2]) if found_raw else None
    return None


def turn_upright(img: Image.Image, orientation: int) -> Image.Image:
    """``img`` turned or flipped as EXIF ``orientation`` says to show it.

    ``img`` itself for 1 and for values outside 2-8, as
    ``ImageOps.exif_transpose`` treats them.
    """
    from PIL import Image

    method = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }.get(orientation)
    return img if method is None else img.transpose(method)


class _NormalizedCache:
    """Answers of :func:`normalize_image_orientation` that took a decode.

    Keyed by the image's SHA-256.  A value is the upright bytes, or ``None``
    for an image that turned out upright.  Bounded by the bytes it holds and by
    entries; the least recently used go first.
    """

    def __init__(self, max_bytes: int, max_entries: int) -> None:
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._entries: OrderedDict[bytes, bytes | None] = OrderedDict()
        self._held = 0
        self._lock = threading.Lock()

    def get(self, key: bytes) -> tuple[bool, bytes | None]:
        """``(True, value)`` for a stored key, else ``(False, None)``."""
        with self._lock:
            if key not in self._entries:
                return False, None
            self._entries.move_to_end(key)
            return True, self._entries[key]

    def put(self, key: bytes, value: bytes | None) -> None:
        size = len(value or b"")
        if size > self._max_bytes:
            return
        with self._lock:
            if key in self._entries:
                self._held -= len(self._entries.pop(key) or b"")
            self._entries[key] = value
            self._held += size
            while self._held > self._max_bytes or len(self._entries) > self._max_entries:
                _key, dropped = self._entries.popitem(last=False)
                self._held -= len(dropped or b"")

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._held = 0


_normalized = _NormalizedCache(_NORMALIZED_BYTES, _NORMALIZED_ENTRIES)


def normalize_image_orientation(data: bytes) -> bytes:
    """Bake an image's EXIF orientation into its pixels; return re-encoded bytes.

    Images with no orientation tag (or an identity orientation) are returned
    UNCHANGED — no re-encode, so the pristine original is preserved.  Every
    send rebuilds the request from the stored originals, so an answer that took
    a decode (a rotation, or a PNG decoded to look for EXIF stored after its
    pixels) is kept by the image's SHA-256 rather than worked out again.  Never
    raises: any failure (Pillow missing, decode error, oversized) returns the
    original bytes.

    Why this exists: a phone photo stores landscape pixels plus an orientation
    tag.  Browsers honour the tag for ``<img>``, but Pillow (our thumbnails) and
    many vision-model image decoders do NOT — so the model literally perceives
    the photo rotated.  Normalising at the read/wire boundary makes every
    consumer (browser, thumbnail, model) see the same upright image.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - declared dependency; defensive
        return data
    try:
        img = Image.open(BytesIO(data))
        turned = exif_orientation(img) in _TURNED_ORIENTATIONS
        # A PNG may store its EXIF after the pixels, where only a decode finds it.
        exif_may_follow = img.format == "PNG" and "exif" not in img.info
        if not turned and not exif_may_follow:
            return data  # upright already — keep the original bytes verbatim
        if img.size[0] * img.size[1] > _MAX_IMAGE_PIXELS:
            if turned:
                log.warning("orientation normalize skipped: image exceeds pixel cap")
            return data
        if too_many_jpeg_scans(data):
            log.warning(
                "orientation normalize skipped: JPEG has more than %d scans", MAX_JPEG_SCANS
            )
            return data
        key = hashlib.sha256(data).digest()
        known, kept = _normalized.get(key)
        if known:
            return data if kept is None else kept
        fmt = img.format or "PNG"
        img.load()
        # Read again: a PNG's EXIF may follow its pixels, and Pillow turns a
        # TIFF itself as it loads and drops the tag, which leaves it upright.
        upright = turn_upright(img, exif_orientation(img))
        if upright is img and not turned:
            _normalized.put(key, None)
            return data
        buf = BytesIO()
        save_kwargs: dict[str, object] = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = 90
        upright.save(buf, format=fmt, **save_kwargs)
        out = buf.getvalue()
        _normalized.put(key, out)
        return out
    except Exception as exc:
        log.warning("orientation normalize failed: %s", exc)
        return data
