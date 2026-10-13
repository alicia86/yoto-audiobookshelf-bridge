"""Series and collection cards: one card follows a whole ABS series, or a collection, in order.

Which book a tap plays is worked out from ABS at tap time, so listening anywhere (the phone, another
card) moves the series card along too:
  - the series book with the most recent listening activity, if it is not finished;
  - if that book is finished, the next book in the series;
  - with no activity at all, the first book.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from .abs_client import ABSClient


@dataclass
class SeriesBook:
    item_id: str
    title: str
    duration: float
    sequence: str = ""


def _seq_key(seq: str):
    """Numeric-aware ordering like ABS's own (localeCompare numeric): "2" < "10", "1.5" between 1 and 2."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)", seq or "")
    return (0, float(m.group(1)), seq) if m else (1, 0.0, seq or "")


def _series_entries(value) -> list[dict]:
    """metadata.series comes as a list of {id, name, sequence} on an expanded item, but as a single
    such object on items listed with a series filter (ABS 2.37)."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


async def series_books(abs: ABSClient, library_id: str, series_id: str, key=None) -> tuple[str, list[SeriesBook]]:
    items = await abs.series_items(library_id, series_id, key)
    name, books = "", []
    for it in items:
        md = it.get("media", {}).get("metadata", {})
        entry = next((s for s in _series_entries(md.get("series")) if s.get("id") == series_id), None)
        if entry is None:
            continue
        name = name or entry.get("name", "")
        books.append(SeriesBook(it["id"], md.get("title") or it.get("id"), float(it.get("media", {}).get("duration") or 0),
                                str(entry.get("sequence") or "")))
    books.sort(key=lambda b: (_seq_key(b.sequence), b.title.lower()))
    return name, books


async def collection_books(abs: ABSClient, collection_id: str, key=None) -> tuple[str, list[SeriesBook]]:
    """A collection's books in the order set in ABS (server/models/Collection.js orders by collectionBook.order)."""
    c = await abs.collection(collection_id, key)
    books = []
    for i, it in enumerate(c.get("books") or []):
        md = it.get("media", {}).get("metadata", {})
        books.append(SeriesBook(it["id"], md.get("title") or it["id"], float(it.get("media", {}).get("duration") or 0), str(i + 1)))
    return c.get("name", ""), books


GROUP_TTL = 60          # seconds a series/collection book list is reused (ABS sends collections fully expanded)


async def group_books(abs: ABSClient, kind: str, library_id: str, group_id: str, key=None):
    """Books of a series or collection, in order. Cached briefly per ABS client: a collection can be
    megabytes of JSON, and every tap of a group card needs its list."""
    cache = abs.__dict__.setdefault("_group_cache", {})
    ck = (kind or "series", library_id, group_id, key)
    hit = cache.get(ck)
    if hit and time.monotonic() - hit[0] < GROUP_TTL:
        return hit[1]
    if kind == "collection":
        result = await collection_books(abs, group_id, key)
    else:
        result = await series_books(abs, library_id, group_id, key)
    cache[ck] = (time.monotonic(), result)
    return result


class CollectionIndex:
    """Which collections each book is in, for the book picker. Built from GET /api/collections
    (every collection fully expanded, megabytes on a big library), so it is refreshed in the
    background at most every few minutes and the picker answers instantly from memory."""

    MAX_AGE = 300

    def __init__(self, abs: ABSClient):
        self.abs = abs
        self.by_item: dict[str, list[dict]] = {}
        self.built = 0.0
        self._task: asyncio.Task | None = None

    async def _build(self):
        index: dict[str, list[dict]] = {}
        for c in await self.abs.collections():
            info = {"id": c["id"], "name": c.get("name", ""), "library_id": c.get("libraryId", ""),
                    "books": len(c.get("books") or [])}
            for b in c.get("books") or []:
                index.setdefault(b.get("id"), []).append(info)
        self.by_item, self.built = index, time.monotonic()

    def refresh(self) -> asyncio.Task:
        if not self._task or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._build())
        return self._task

    async def for_item(self, item_id: str) -> list[dict]:
        if not self.built:
            await self.refresh()              # first use: wait for it once
        elif time.monotonic() - self.built > self.MAX_AGE:
            self.refresh()                    # stale: answer now, refresh behind the scenes
        return self.by_item.get(item_id, [])


def current_index(books: list[SeriesBook], progress: list[dict]) -> int:
    by_item = {p.get("libraryItemId"): p for p in progress}
    touched = [(float(by_item[b.item_id].get("lastUpdate") or 0), i) for i, b in enumerate(books) if b.item_id in by_item]
    if not touched:
        return 0
    _, i = max(touched)
    if by_item[books[i].item_id].get("isFinished"):
        return i + 1 if i + 1 < len(books) else 0
    return i
