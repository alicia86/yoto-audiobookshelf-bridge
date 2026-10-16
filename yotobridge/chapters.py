"""Chapters as Yoto tracks.

The Yoto cannot seek inside a stream track (tested), but its next/previous buttons move between
tracks, and it remembers per card which track it was on and asks for that track on the next tap
(tested). So a streaming card's playlist holds one stream track per chapter with stable numbers:
track N is chapter N of the current book, from chapter 1, then on into the next books of a series
or collection (at most 99 tracks). What a track plays is decided when the player asks for it:

  a fresh tap (no listening session, or the card was taken out since):
      the track holding the ABS position resumes there (less the rewind); if the player asked
      for another track (e.g. the phone moved the book on), the bridge sends a few seconds of
      silence and tells the player over MQTT to start the right track instead.
  within a listen (next/previous, or the player moving on by itself):
      the whole chapter, from its start.

The layout only changes when the book (or the window of a very long book) changes. Track URLs carry
the layout id, so a player still holding an older playlist of the same books gets what it expects.
Books without chapters, and chapters longer than an hour, are cut into parts of part_seconds; those
cuts move to the nearest pause (quiet.py) when the book's pauses are known.
"""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass

from .quiet import Snap, cut

MAX_TRACKS = 99
KEEP_LAYOUTS = 20          # a player may hold an older list for a while; layouts are small


@dataclass
class Segment:
    item_id: str
    book: str              # book title
    start: float
    end: float
    number: int            # chapter (or part) number within the book, 1-based
    title: str
    book_duration: float
    chaptered: bool = True     # False: a fixed-length part of a book without chapters


def chapter_spans(chapters: list[dict], duration: float, part_seconds: int,
                  snap: Snap | None = None) -> list[tuple[float, float, str]]:
    """(start, end, title) per track: each chapter, with chapters longer than two parts split evenly
    into parts of at most part_seconds (some books have 2 "chapters" for 6 hours); books with fewer
    than 2 chapters get fixed-length parts. snap moves the cuts that are not chapter boundaries."""
    out = []
    for start, end, title in _raw_spans(chapters, duration, part_seconds, snap):
        n = 1 if end - start <= 2 * part_seconds else -(-(end - start) // part_seconds)
        n = int(n)
        step = (end - start) / n
        s = start
        for i in range(n):
            e = end if i == n - 1 else cut(snap, start + (i + 1) * step, s, end)
            out.append((s, e, f"{title} ({i + 1}/{n})" if n > 1 else title))
            s = e
    return out


def _raw_spans(chapters: list[dict], duration: float, part_seconds: int,
               snap: Snap | None = None) -> list[tuple[float, float, str]]:
    if len(chapters) >= 2:
        spans = []
        for i, c in enumerate(chapters):
            start = float(c.get("start", 0))
            nxt = float(chapters[i + 1].get("start", duration)) if i + 1 < len(chapters) else duration
            end = float(c["end"]) if c.get("end") else nxt
            if end - start > 0.5:
                spans.append((start, end, c.get("title") or f"Chapter {i + 1}"))
        if spans:
            return _fill_gaps(spans, duration, part_seconds, snap)
    out, t, n = [], 0.0, 1
    while t < duration - 0.5:
        e = min(duration, n * part_seconds)
        e = e if e >= duration else cut(snap, e, t, duration)
        out.append((t, e, f"Part {n}"))
        t = e
        n += 1
    return out or [(0.0, duration, "Part 1")]


def _fill_gaps(spans: list[tuple[float, float, str]], duration: float,
               part_seconds: int, snap: Snap | None = None) -> list[tuple[float, float, str]]:
    """Cover audio no chapter covers (broken chapter data: a 20 h book listed as one 2 h chapter and an
    empty one) with parts of part_seconds, so every second of the book is on some track."""
    out, t, n = [], 0.0, 1
    for start, end, title in spans + [(duration, duration, "")]:
        gap, k = t, 1
        while start - t > 0.5:
            e = min(start, gap + k * part_seconds)
            e = e if e >= start else cut(snap, e, t, start)
            out.append((t, e, f"Part {n}"))
            t, n, k = e, n + 1, k + 1
        if title:
            out.append((start, end, title))
        t = max(t, end)
    return out


def index_at(spans: list[tuple[float, float, str]], pos: float) -> int:
    for i, (s, e, _) in enumerate(spans):
        if s <= pos < e:
            return i
    return len(spans) - 1 if spans and pos >= spans[-1][0] else 0


WINDOW_STEP = MAX_TRACKS - 9   # a book with more tracks than fit shows a 99-track window that moves in steps of 90


def window_start(index: int, total: int) -> int:
    """First track of the window holding chapter `index` (0-based) of a book with `total` tracks.
    The window only moves when the listener passes its 90th track, so the numbers the player
    remembers stay valid for 90 chapters at a time; each window overlaps the previous by 9."""
    return min((index // WINDOW_STEP) * WINDOW_STEP, total - MAX_TRACKS)


def make_layout(books: list[tuple[str, str, float, list, float]], part_seconds: int,
                snaps: dict[str, Snap | None] | None = None) -> list[Segment]:
    """books: (item_id, title, duration, chapters, position) from the current book on, each from its
    first chapter (a book with more than 99 tracks starts a few before the one holding the position).
    snaps: per book, where its cuts inside chapters move to (quiet.py)."""
    segs: list[Segment] = []
    for n, (item_id, title, duration, chapters, pos) in enumerate(books):
        spans = chapter_spans(chapters, duration, part_seconds, (snaps or {}).get(item_id))
        first = 0 if n or len(spans) <= MAX_TRACKS else window_start(index_at(spans, pos), len(spans))
        for i in range(first, len(spans)):
            s, e, t = spans[i]
            chaptered = len(chapters) >= 2
            number = sum(1 for c in chapters if float(c.get("start", 0)) <= s + 0.5) if chaptered else i + 1
            segs.append(Segment(item_id, title, s, e, max(1, number), t, duration, chaptered))
            if len(segs) >= MAX_TRACKS:
                return segs
    return segs


def new_layout_id() -> str:
    return secrets.token_urlsafe(4).replace("-", "x").replace("_", "y")


def store_layout(slot, segs: list[Segment]) -> str:
    lid = new_layout_id()
    layouts = dict(slot.layouts)
    layouts[lid] = [asdict(s) for s in segs]
    for old in list(layouts)[:-KEEP_LAYOUTS]:
        layouts.pop(old, None)
    slot.layouts, slot.layout_id = layouts, lid
    return lid


def track_for(slot, item_id: str, pos: float) -> int | None:
    """Track number (1-based) of the current layout holding (item_id, pos)."""
    for k, s in enumerate(slot.layouts.get(slot.layout_id) or [], 1):
        if s["item_id"] == item_id and s["start"] <= pos < s["end"]:
            return k
    return None


def same_layout(slot, segs: list[Segment]) -> bool:
    return [asdict(s) for s in segs] == (slot.layouts.get(slot.layout_id) or None)


def current_layout_for(slot, layout_id: str) -> str:
    """The layout to serve when a player asks for layout_id. A player may start a playlist from its
    cache before it fetches the update (seen with "Play on Yoto": it asked for the previous book's
    list). An older list of the same book(s) still means what it says; one whose books are no longer
    on the card, or one the bridge no longer keeps, is replaced by the current list."""
    current = slot.layout_id
    if not current or layout_id == current:
        return layout_id
    old = {s["item_id"] for s in slot.layouts.get(layout_id) or []}
    now = {s["item_id"] for s in slot.layouts.get(current) or []}
    return layout_id if old & now else current


def segment(slot, layout_id: str, k: int) -> Segment | None:
    segs = slot.layouts.get(layout_id) or []
    return Segment(**segs[k - 1]) if 1 <= k <= len(segs) else None
