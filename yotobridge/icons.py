"""16x16 icons for the Yoto's pixel display: the book's cover, shrunk and dimmed, with the chapter
number drawn on top in pixel digits.

Streaming cards point their track at an icon URL on the bridge (display.iconUrl16x16, yoto.dev
"dynamic icons"), which draws the chapter being listened to. Offline copies upload one icon per
chapter (display.icon16x16, yoto.dev "uploading icons"; needs the user:icons:manage scope) so the
icons are on the player with the audio.

No imaging library: ffmpeg (already needed for audio) shrinks the cover to raw RGBA, and the PNG
is written by hand with zlib.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import zlib

log = logging.getLogger(__name__)
SIZE = 16

# 3x5 pixel digits, one string per row
DIGITS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
}
FALLBACK_BG = (60, 70, 90)       # when a book has no cover; never black (black pixels stay dark on the Yoto)


def png(rgba: bytes, w: int = SIZE, h: int = SIZE) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


async def shrink_cover(image: bytes, ffmpeg: str = "ffmpeg") -> bytes | None:
    """Cover image (any format ffmpeg reads) -> 16x16 RGBA, cropped to a square from the centre."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=16:16:flags=area", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(image)
    if proc.returncode or len(out) != SIZE * SIZE * 4:
        log.debug("cover shrink failed: %s", err.decode(errors="replace")[-200:])
        return None
    return out


async def to_jpeg(image: bytes, ffmpeg: str = "ffmpeg") -> bytes | None:
    """Any cover ffmpeg reads (ABS often serves WebP) -> JPEG, which Yoto's cover upload accepts."""
    if image[:2] == b"\xff\xd8" or image[:8] == b"\x89PNG\r\n\x1a\n" or image[:4] == b"GIF8":
        return image
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1", "-q:v", "3",
        "-f", "mjpeg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate(image)
    return out if proc.returncode == 0 and out[:3] == b"\xff\xd8\xff" else None


STYLE = "corner-tiny"             # bump when the look changes, so offline copies upload new icons

# 3x4 pixel digits for the corner number: the smallest that still tell 0-9 apart
TINY = {
    "0": ["###", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#."],
    "2": ["##.", "..#", ".#.", "###"],
    "3": ["###", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#"],
    "5": ["###", "##.", "..#", "##."],
    "6": ["#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#."],
    "8": ["###", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#"],
}
INK = 70                          # % of the way from the cover pixel to the digit colour: a tint, not a stamp


def render(cover16: bytes | None, number: int | None) -> bytes:
    """PNG of the icon: the cover as it is, with the chapter number tiny in the top-right corner,
    tinted into the cover (light on dark covers, dark on light ones) instead of drawn on a patch."""
    px = bytearray(cover16) if cover16 else bytearray(bytes([*FALLBACK_BG, 255]) * SIZE * SIZE)
    if number is None:
        return png(bytes(px))
    text = str(max(0, number))[-3:]
    width = len(text) * 4 - 1
    x0, y0 = SIZE - width, 0                             # flush with the top-right corner
    corner = [px[(y * SIZE + x) * 4:(y * SIZE + x) * 4 + 3] for y in range(4) for x in range(x0, SIZE)]
    light = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in corner) / len(corner) > 140
    ink = (40, 40, 44) if light else (235, 235, 235)
    for n, ch in enumerate(text):
        for cy, row in enumerate(TINY[ch]):
            for cx, c in enumerate(row):
                if c == "#":
                    i = ((y0 + cy) * SIZE + x0 + n * 4 + cx) * 4
                    px[i:i + 3] = bytes(p + (k - p) * INK // 100 for p, k in zip(px[i:i + 3], ink))
                    px[i + 3] = 255
    return png(bytes(px))


def chapter_number(chapters: list[dict], pos: float) -> int | None:
    """1-based number of the ABS chapter containing pos; None for a book without chapters."""
    if len(chapters) < 2:
        return None
    n = 1
    for i, c in enumerate(chapters):
        if float(c.get("start", 0)) <= pos + 0.5:
            n = i + 1
    return n
