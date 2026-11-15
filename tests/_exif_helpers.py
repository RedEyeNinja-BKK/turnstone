"""Builders for EXIF blocks and the pictures that carry them, for the image tests."""

from __future__ import annotations

import struct
import zlib
from io import BytesIO

from PIL import Image

ORIENTATION = 0x0112


def exif_block(entries: list[tuple[int, int, int, bytes]], order: str = "<") -> bytes:
    """An EXIF block with one directory of ``(tag, type, count, value)`` entries.

    ``value`` is the entry's four value bytes: the value itself, or an offset.
    """
    head = (b"II*\x00" if order == "<" else b"MM\x00*") + struct.pack(order + "L", 8)
    directory = struct.pack(order + "H", len(entries)) + b"".join(
        struct.pack(order + "HHL", tag, kind, count) + value for tag, kind, count, value in entries
    )
    return head + directory + b"\x00\x00\x00\x00"


def orientation_entry(orientation: int, order: str = "<") -> tuple[int, int, int, bytes]:
    return (ORIENTATION, 3, 1, struct.pack(order + "HH", orientation, 0))


def halves(size: tuple[int, int] = (40, 20)) -> Image.Image:
    """Left half red, right half blue: orientation 6 shows red on top."""
    img = Image.new("RGB", size, (255, 0, 0))
    img.paste((0, 0, 255), (size[0] // 2, 0, size[0], size[1]))
    return img


def jpeg_with_exif(exif: bytes, img: Image.Image | None = None) -> bytes:
    buf = BytesIO()
    (img or halves()).save(buf, format="JPEG", quality=95, exif=b"Exif\x00\x00" + exif)
    return buf.getvalue()


def png_with_exif(
    exif: bytes, img: Image.Image | None = None, *, after_pixels: bool = False
) -> bytes:
    """A PNG carrying ``exif`` in an eXIf chunk, before its pixels or after them."""
    buf = BytesIO()
    if not after_pixels:
        (img or halves()).save(buf, format="PNG", exif=exif)
        return buf.getvalue()
    (img or halves()).save(buf, format="PNG")
    png = buf.getvalue()
    chunk = b"eXIf" + exif
    # IEND, the last 12 bytes, stays last.
    return (
        png[:-12]
        + struct.pack(">L", len(exif))
        + chunk
        + struct.pack(">L", zlib.crc32(chunk))
        + png[-12:]
    )
