"""16x16 icons for the Yoto's pixel display, in one of several styles (STYLES): by default the
book's cover with the chapter number tiny in a corner.

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


# Icon styles, as offered on the page. "cover-tr" is the default; "theme", "custom" and "none" are
# handled by the bridge (they need the Yoto icon library, an uploaded picture, or no icon at all).
STYLES = {
    "cover-tr": "Cover, number top right",
    "cover-tl": "Cover, number top left",
    "cover-bl": "Cover, number bottom left",
    "cover-br": "Cover, number bottom right",
    "cover-big": "Big number on a dimmed cover",
    "cover": "Cover only",
    "number": "Number only",
    "progress": "Cover with a progress bar",
    "theme": "Chapter theme icons",
    "random": "Random pixel creatures",
    "custom": "Custom picture",
    "none": "No icon (Yoto's own)",
}
DEFAULT_STYLE = "cover-tr"
CORNERS = {"cover-tr": (1, 0), "cover-tl": (0, 0), "cover-bl": (0, 1), "cover-br": (1, 1)}   # (right?, bottom?)


def _base(cover16: bytes | None) -> bytearray:
    return bytearray(cover16) if cover16 else bytearray(bytes([*FALLBACK_BG, 255]) * SIZE * SIZE)


def _luma(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def render(cover16: bytes | None, number: int | None, corner: str = "cover-tr") -> bytes:
    """PNG of the icon: the cover as it is, with the chapter number tiny in a corner (top right
    unless corner says otherwise), tinted into the cover (light on dark covers, dark on light ones)
    instead of drawn on a patch."""
    px = _base(cover16)
    if number is None:
        return png(bytes(px))
    text = str(max(0, number))[-3:]
    width = len(text) * 4 - 1
    right, bottom = CORNERS.get(corner, (1, 0))
    x0, y0 = (SIZE - width if right else 0), (SIZE - 4 if bottom else 0)   # flush with the corner
    area = [px[(y * SIZE + x) * 4:(y * SIZE + x) * 4 + 3] for y in range(y0, y0 + 4) for x in range(x0, x0 + width)]
    light = sum(_luma(p) for p in area) / len(area) > 140
    ink = (40, 40, 44) if light else (235, 235, 235)
    for n, ch in enumerate(text):
        for cy, row in enumerate(TINY[ch]):
            for cx, c in enumerate(row):
                if c == "#":
                    i = ((y0 + cy) * SIZE + x0 + n * 4 + cx) * 4
                    px[i:i + 3] = bytes(p + (k - p) * INK // 100 for p, k in zip(px[i:i + 3], ink))
                    px[i + 3] = 255
    return png(bytes(px))


def _big_digits(px: bytearray, number: int, ink: tuple) -> None:
    """The number centred in 3x5 digits, doubled in size when it has one or two digits."""
    text = str(max(0, number))[-3:]
    scale = 2 if len(text) <= 2 else 1
    width = (len(text) * 4 - 1) * scale
    x0, y0 = (SIZE - width) // 2, (SIZE - 5 * scale) // 2
    for n, ch in enumerate(text):
        for cy, row in enumerate(DIGITS[ch]):
            for cx, c in enumerate(row):
                if c != "#":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        i = ((y0 + cy * scale + dy) * SIZE + x0 + (n * 4 + cx) * scale + dx) * 4
                        px[i:i + 4] = bytes([*ink, 255])


def average(cover16: bytes | None) -> tuple[int, int, int]:
    if not cover16:
        return FALLBACK_BG
    n = len(cover16) // 4
    return tuple(sum(cover16[i + k] for i in range(0, len(cover16), 4)) // n for k in range(3))


def render_big(cover16: bytes | None, number: int | None) -> bytes:
    """The cover dimmed to 40%, with the chapter number big and white in the middle."""
    px = _base(cover16)
    for i in range(0, len(px), 4):
        px[i:i + 3] = bytes(v * 40 // 100 for v in px[i:i + 3])
    if number is not None:
        _big_digits(px, number, (240, 240, 240))
    return png(bytes(px))


def render_number(cover16: bytes | None, number: int | None) -> bytes:
    """Just the number, big, on a plain colour taken from the cover (kept mid-dark so it glows on the
    Yoto without washing the digits out)."""
    r, g, b = average(cover16)
    top = max(r, g, b, 1)
    bg = tuple(min(255, v * 150 // top) for v in (r, g, b))     # same hue, brightest channel at 150
    px = bytearray(bytes([*bg, 255]) * SIZE * SIZE)
    if number is not None:
        _big_digits(px, number, (40, 40, 44) if _luma(bg) > 140 else (245, 245, 245))
    return png(bytes(px))


def render_progress(cover16: bytes | None, fraction: float | None) -> bytes:
    """The cover with a bar along the bottom row: how far through the book this chapter starts."""
    px = _base(cover16)
    if fraction is None:
        return png(bytes(px))
    filled = max(1, min(SIZE, round(fraction * SIZE)))
    for x in range(SIZE):
        i = ((SIZE - 1) * SIZE + x) * 4
        px[i:i + 4] = bytes([250, 250, 250, 255] if x < filled else [30, 30, 34, 255])
    return png(bytes(px))


def _vivid(cover16: bytes | None, rnd) -> tuple[int, int, int]:
    """A colourful pixel of the cover (or a random bright colour when the cover is grey)."""
    best, score = None, 60
    if cover16:
        for i in range(0, len(cover16), 4):
            r, g, b = cover16[i:i + 3]
            s = max(r, g, b) - min(r, g, b) + max(r, g, b) // 4
            if s > score:
                best, score = (r, g, b), s
    if best is None:
        best = rnd.choice([(250, 90, 90), (90, 200, 120), (90, 150, 250), (250, 200, 60), (200, 110, 240)])
    top = max(best) or 1
    return tuple(min(255, v * 240 // top) for v in best)       # as bright as it can go, same hue


def render_random(cover16: bytes | None, seed: str) -> bytes:
    """A little pixel creature: an 8x8 grid mirrored left to right, doubled to 16x16, in a colour
    from the cover, on the cover's darkened average. The same seed always draws the same creature."""
    import random
    rnd = random.Random(seed)
    body = _vivid(cover16, rnd)
    shade = tuple(v * 60 // 100 for v in body)
    bg = tuple(v * 25 // 100 for v in average(cover16))
    grid = [[0] * 8 for _ in range(8)]
    for y in range(1, 7):
        for x in range(1, 4):
            grid[y][x] = 1 if rnd.random() < (0.65 if x == 3 else 0.5) else 0
        grid[y][3] = grid[y][3] or (2 <= y <= 5)              # a solid middle so it reads as one body
    for y in range(1, 7):
        for x in range(1, 4):
            if grid[y][x] and rnd.random() < 0.2:
                grid[y][x] = 2                                 # a few shaded pixels
    ey = rnd.choice([2, 3])
    grid[ey][2] = 3                                            # eyes
    for y in range(8):
        for x in range(4):
            grid[y][7 - x] = grid[y][x]
    colours = {0: bg, 1: body, 2: shade, 3: (20, 20, 24)}
    px = bytearray(SIZE * SIZE * 4)
    for y in range(SIZE):
        for x in range(SIZE):
            i = (y * SIZE + x) * 4
            px[i:i + 4] = bytes([*colours[grid[y // 2][x // 2]], 255])
    return png(bytes(px))


def render_style(style: str, cover16: bytes | None, number: int | None, fraction: float | None = None,
                 seed: str = "") -> bytes:
    """The icon in one of the drawn styles. Styles the bridge draws itself fall back to the default."""
    if style == "cover":
        return render(cover16, None)
    if style == "cover-big":
        return render_big(cover16, number)
    if style == "number":
        return render_number(cover16, number)
    if style == "progress":
        return render_progress(cover16, fraction)
    if style == "random":
        return render_random(cover16, seed)
    return render(cover16, number, style if style in CORNERS else DEFAULT_STYLE)


def chapter_number(chapters: list[dict], pos: float) -> int | None:
    """1-based number of the ABS chapter containing pos; None for a book without chapters."""
    if len(chapters) < 2:
        return None
    n = 1
    for i, c in enumerate(chapters):
        if float(c.get("start", 0)) <= pos + 0.5:
            n = i + 1
    return n
