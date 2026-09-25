"""Image-pixel utilities shared across the thumbnail, wire, and perception paths.

Currently: EXIF-orientation normalisation and the wire-payload budget.  Kept
separate from :mod:`turnstone.core.thumbnails` (which downscales for the UI) —
this operates on full-resolution bytes at the read/wire boundary so every
consumer of an attachment sees the same upright pixels, and so the bytes actually
sent to a provider stay bounded independently of what is stored.
"""

from __future__ import annotations

from io import BytesIO

from turnstone.core.log import get_logger

log = get_logger(__name__)

# EXIF tag 0x0112 (274) — image orientation (1 = upright; 2-8 = flips/rotations).
_EXIF_ORIENTATION_TAG = 0x0112

# Mirror turnstone.core.thumbnails: bound decoded pixels so a small compressed
# file that expands to an enormous bitmap can't OOM the node during re-encode.
_MAX_IMAGE_PIXELS = 40_000_000

# Wire-payload budget for one inlined image.  An attachment is stored at full
# resolution and re-encoded to base64 at SEND time, so the stored blob is never
# the payload a provider bills.  Measured 2026-09-25: a workstream whose prompts
# ran ~54k tokens sent 2,879,020 bytes of PNG as base64 and was billed 834,047
# prompt tokens for it — 4.60 base64 characters per token, i.e. the whole data
# URL tokenized as text, which is 3.45 bytes of image per token.  One 2 MB
# screenshot therefore costs more than a 266k-token context window, and no amount
# of history compaction can undo it, because the image is present in every
# attempt.  A 96 KiB budget keeps one image under ~28k tokens even under that
# worst-case billing (~13% of a 220k usable input budget) and costs ~1-2k tokens
# on a lane that charges per image patch.  Bound the SENT bytes only; the stored
# blob and every UI preview keep their full resolution.
_MAX_WIRE_IMAGE_BYTES = 96 * 1024
_MAX_WIRE_IMAGE_EDGE = 1568
_WIRE_IMAGE_EDGES = (_MAX_WIRE_IMAGE_EDGE, 1280, 1024, 768, 512, 384)


def normalize_image_orientation(data: bytes) -> bytes:
    """Bake an image's EXIF orientation into its pixels; return re-encoded bytes.

    Images with no orientation tag (or an identity orientation) are returned
    UNCHANGED — no decode/re-encode, so the pristine original is preserved and
    there is no per-send cost in the common case.  Never raises: any failure
    (Pillow missing, decode error, oversized) returns the original bytes.

    Why this exists: a phone photo stores landscape pixels plus an orientation
    tag.  Browsers honour the tag for ``<img>``, but Pillow (our thumbnails) and
    many vision-model image decoders do NOT — so the model literally perceives
    the photo rotated.  Normalising at the read/wire boundary makes every
    consumer (browser, thumbnail, model) see the same upright image.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - declared dependency; defensive
        return data
    try:
        img = Image.open(BytesIO(data))
        orientation = img.getexif().get(_EXIF_ORIENTATION_TAG)
        if not orientation or orientation == 1:
            return data  # upright already — keep the original bytes verbatim
        if img.size[0] * img.size[1] > _MAX_IMAGE_PIXELS:
            log.warning("orientation normalize skipped: image exceeds pixel cap")
            return data
        fmt = img.format or "PNG"
        upright = ImageOps.exif_transpose(img)  # applies the rotation + drops the tag
        if upright is None:  # pragma: no cover - in_place=False never returns None
            return data
        buf = BytesIO()
        save_kwargs: dict[str, object] = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = 90
        upright.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue()
    except Exception as exc:
        log.warning("orientation normalize failed: %s", exc)
        return data


def bound_image_for_wire(data: bytes) -> bytes:
    """Return *data* re-encoded to fit the wire budget, or *data* unchanged.

    Input already within ``_MAX_WIRE_IMAGE_BYTES`` is returned verbatim — no
    decode, no re-encode, no quality change, which keeps the common case free and
    the stored bytes and model view identical.  A larger input is downscaled to
    the largest edge in ``_WIRE_IMAGE_EDGES`` that fits the byte budget, JPEG for
    opaque images and PNG when transparency must survive.

    Never raises: any decode/encode failure returns the original bytes, so a
    bounded payload is a best effort and never a new way to lose an attachment.
    """
    if len(data) <= _MAX_WIRE_IMAGE_BYTES:
        return data
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - declared dependency; defensive
        return data
    try:
        img = Image.open(BytesIO(data))
        if img.size[0] * img.size[1] > _MAX_IMAGE_PIXELS:
            # Refuse to decode an expansion bomb; Pillow's own bomb guard would
            # raise anyway.  The stored bytes still reach the provider unchanged.
            log.warning("wire image bound skipped: image exceeds pixel cap")
            return data
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        )
        fmt = "PNG" if has_alpha else "JPEG"
        encoded = data
        for edge in _WIRE_IMAGE_EDGES:
            candidate = img.copy()
            candidate.thumbnail((edge, edge), Image.LANCZOS)
            if fmt == "JPEG" and candidate.mode != "RGB":
                candidate = candidate.convert("RGB")
            buf = BytesIO()
            if fmt == "JPEG":
                candidate.save(buf, format=fmt, quality=80, optimize=True)
            else:
                candidate.save(buf, format=fmt, optimize=True)
            encoded = buf.getvalue()
            if len(encoded) <= _MAX_WIRE_IMAGE_BYTES:
                log.info(
                    f"wire image bounded: {len(data)} -> {len(encoded)} bytes "
                    f"at {edge}px ({fmt})"
                )
                return encoded
        # Every rung of the ladder still exceeded the budget: the smallest
        # attempt still beats sending the original.
        log.warning(
            f"wire image bound settled above budget: {len(data)} -> {len(encoded)} bytes"
        )
        return encoded
    except Exception as exc:
        log.warning(f"wire image bound failed: {exc}")
        return data
