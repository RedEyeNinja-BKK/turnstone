"""Build JPEGs with many scans, for the scan check in turnstone.core.images."""

from __future__ import annotations

from io import BytesIO


def _segments(data: bytes) -> list[tuple[int, int, int]]:
    """``(marker, start, end)`` for each segment; a scan includes its coded data."""
    out = [(0xD8, 0, 2)]
    i = 2
    while i < len(data):
        while data[i] == 0xFF:
            i += 1
        marker = data[i]
        start = i - 1
        i += 1
        if marker == 0xD9:
            out.append((marker, start, i))
            break
        end = i + int.from_bytes(data[i : i + 2], "big")
        if marker == 0xDA:
            # Coded data runs to the next marker other than stuffing or a restart.
            while not (
                data[end] == 0xFF and data[end + 1] != 0 and not 0xD0 <= data[end + 1] <= 0xD7
            ):
                end += 1
        out.append((marker, start, end))
        i = end
    return out


def jpeg_with_scans(extra: int, side: int = 64, orientation: int = 1) -> bytes:
    """A valid progressive JPEG whose last scan repeats ``extra`` more times.

    Decoders accept the repeats (with a warning) and pay a pass over the whole
    picture for each.  ``orientation`` other than 1 adds that EXIF tag.
    """
    from PIL import Image

    buf = BytesIO()
    exif = Image.Exif()
    if orientation != 1:
        exif[0x0112] = orientation
    Image.linear_gradient("L").resize((side, side)).convert("RGB").save(
        buf, "JPEG", quality=75, progressive=True, exif=exif
    )
    data = buf.getvalue()
    segments = _segments(data)
    last = max(i for i, seg in enumerate(segments) if seg[0] == 0xDA)
    first = last - 1 if segments[last - 1][0] == 0xC4 else last  # its table, if any
    repeat = data[segments[first][1] : segments[last][2]]
    end_of_image = segments[-1][1]
    return data[:end_of_image] + repeat * extra + data[end_of_image:]
