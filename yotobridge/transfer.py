"""Move an old MYO card (audio uploaded to Yoto) onto Audiobookshelf without losing the listener's place.

1. Read the old playlist's tracks from Yoto.
2. Work out which ABS book, and where in it, each track is. Tracks are matched by length against the
   book's file and chapter boundaries, then chained: the next track should start where this one ended,
   or at the start of the next book. That copes with one track per book, per file, per chapter, or
   files merged at boundaries; tracks with no exact match get an estimated start from their neighbours.
3. Read where the player is on the old card (the MQTT event it sends while the card is in it).
4. Write that place to ABS (books earlier on the card marked finished) and make a new card (Yoto
   playlist) for the book's series. The old playlist is never changed.
"""
from __future__ import annotations

import bisect
import logging
import re
import time
from dataclasses import dataclass, field

from aiohttp import web

from .abs_client import ABSError
from .series import _series_entries, group_books

log = logging.getLogger(__name__)

STOP = {"the", "and", "with", "from", "chapter", "track", "part", "disc", "book", "books", "audiobook",
        "unabridged", "abridged", "series", "volume", "intro", "outro", "credits", "opening", "closing",
        "mp3", "m4a", "m4b", "edition", "card", "yoto", "myo", "read", "audio"}
MAX_BOOKS = 80
KEEP = 80            # candidate places kept per track


def tol(d: float) -> float:
    """How far a Yoto track's length may be from the matching span in ABS (Yoto rounds and transcodes)."""
    return 2.5 + 0.002 * d


# ---------------------------------------------------------------- Yoto side

def card_tracks(card: dict) -> list[dict]:
    """The old playlist's tracks in play order: n (1-based), chapter/track keys, title, duration."""
    out = []
    for ch in (card.get("content") or {}).get("chapters") or []:
        tracks = ch.get("tracks") or [{}]
        for t in tracks:
            d = t.get("duration") if t.get("duration") is not None else (ch.get("duration") if len(tracks) == 1 else None)
            out.append({"n": len(out) + 1, "chapter_key": str(ch.get("key", "")), "track_key": str(t.get("key", "")),
                        "title": t.get("title") or ch.get("title") or f"Track {len(out) + 1}",
                        "duration": float(d) if d else 0.0, "type": t.get("type", "")})
    return out


def track_for_event(tracks: list[dict], ev: dict) -> dict | None:
    ck, tk = str(ev.get("chapterKey") or ""), str(ev.get("trackKey") or "")
    hit = next((t for t in tracks if t["chapter_key"] == ck and t["track_key"] == tk), None)
    hit = hit or next((t for t in tracks if ck and t["chapter_key"] == ck), None)
    if not hit and ck.isdigit() and 1 <= int(ck) <= len(tracks) and not any(t["chapter_key"] for t in tracks):
        hit = tracks[int(ck) - 1]
    return hit


def words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z][a-z']+", (text or "").lower().replace("_", " ")):
        w = w.strip("'")
        if len(w) < 4 or w in STOP:
            continue
        if w.endswith("s") and len(w) > 5:
            w = w[:-1]          # ABS search is a substring match: "hollowbrook" finds "Hollowbrooks"
        out.append(w)
    return out


def search_terms(card_title: str, tracks: list[dict], limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for w in words(card_title):
        counts[w] = counts.get(w, 0) + 1000          # the card's own title first
    for t in tracks:
        for w in set(words(t["title"])):
            counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


# ---------------------------------------------------------------- ABS side

@dataclass
class Book:
    item_id: str
    title: str
    duration: float
    library_id: str
    bounds: list[float]
    chapters: list[dict]
    series: list[dict] = field(default_factory=list)   # {id, name, sequence, library_id}
    words: set = field(default_factory=set)

    @classmethod
    def from_item(cls, item: dict) -> "Book":
        media = item.get("media") or {}
        md = media.get("metadata") or {}
        dur = float(media.get("duration") or 0)
        b = {0.0, dur}
        for t in media.get("tracks") or media.get("audioFiles") or []:
            if t.get("startOffset") is not None:
                b.add(float(t["startOffset"]))
                b.add(float(t["startOffset"]) + float(t.get("duration") or 0))
        chapters = media.get("chapters") or []
        for c in chapters:
            b.add(float(c.get("start") or 0))
            b.add(float(c.get("end") or 0))
        series = [{"id": s["id"], "name": s.get("name", ""), "sequence": str(s.get("sequence") or ""),
                   "library_id": item.get("libraryId", "")} for s in _series_entries(md.get("series")) if s.get("id")]
        title = md.get("title") or item.get("id")
        return cls(item["id"], title, dur, item.get("libraryId", ""), sorted(x for x in b if 0 <= x <= dur),
                   chapters, series, set(words(title + " " + " ".join(s["name"] for s in series))))

    def has_bound(self, x: float, t: float) -> bool:
        i = bisect.bisect_left(self.bounds, x - t)
        return i < len(self.bounds) and self.bounds[i] <= x + t

    def chapter_at(self, pos: float) -> str:
        for c in self.chapters:
            if float(c.get("start") or 0) <= pos < float(c.get("end") or 0):
                return c.get("title") or ""
        return ""


# ---------------------------------------------------------------- matching

@dataclass(frozen=True)
class Place:
    book: int          # index into books, -1 = unknown
    start: float
    exact: bool


UNKNOWN = Place(-1, 0.0, False)


def match(tracks: list[dict], books: list[Book]) -> list[Place]:
    """Best place (book, start second) for every track: a Viterbi pass over candidate places."""
    n = len(tracks)
    if not n:
        return []
    layers: list[dict[Place, tuple[float, Place | None]]] = []
    for i, t in enumerate(tracks):
        d = t["duration"]
        tw = set(words(t["title"]))
        cands: set[Place] = {UNKNOWN}
        if d > 0:
            for bi, b in enumerate(books):
                for s in b.bounds:
                    if s < b.duration - 1 and b.has_bound(s + d, tol(d)):
                        cands.add(Place(bi, s, True))
        if i:
            pd = tracks[i - 1]["duration"]
            for p in layers[-1]:
                if p.book < 0 or not pd:
                    continue
                b = books[p.book]
                exp = p.start + pd
                if exp < b.duration - tol(d) and not any(c.book == p.book and c.exact and abs(c.start - exp) <= tol(exp - p.start) + 2
                                                         for c in cands):
                    cands.add(Place(p.book, exp, False))
                if abs(exp - b.duration) <= tol(pd) + 2:
                    for bi, nb in enumerate(books):       # the previous book just ended: any book may start
                        if bi != p.book and not any(c.book == bi and c.exact and c.start < tol(d) for c in cands):
                            cands.add(Place(bi, 0.0, False))

        def emit(c: Place) -> float:
            if c.book < 0:
                return 0.0
            title_hit = 0.5 if tw & books[c.book].words else 0.0
            return (2.0 if c.exact else 0.1) + title_hit

        layer: dict[Place, tuple[float, Place | None]] = {}
        for c in cands:
            if not i:
                layer[c] = (emit(c) - (1.0 if c.book >= 0 and c.start > tol(d) else 0.0), None)
                continue
            best, arg = None, None
            pd = tracks[i - 1]["duration"]
            for p, (ps, _) in layers[-1].items():
                if c.book < 0:
                    tr = -0.5
                elif p.book < 0:
                    tr = -1.0 if c.start > tol(d) else 0.0
                else:
                    exp = p.start + pd
                    slack = tol(pd) + 2
                    # chaining earns a bonus only when this track matched exactly; an estimate merely carries on
                    if c.book == p.book and abs(c.start - exp) <= slack:
                        tr = 1.0 if c.exact else 0.0
                    elif c.start <= tol(d) and abs(exp - books[p.book].duration) <= slack:
                        tr = 1.0 if c.exact else 0.0      # next book from its beginning
                    else:
                        tr = -2.0
                v = ps + tr
                if best is None or v > best:
                    best, arg = v, p
            layer[c] = (best + emit(c), arg)
        if len(layer) > KEEP:
            layer = dict(sorted(layer.items(), key=lambda kv: -kv[1][0])[:KEEP])
            layer.setdefault(UNKNOWN, (0.0, None))
        layers.append(layer)
    # backtrack
    cur = max(layers[-1].items(), key=lambda kv: kv[1][0])[0]
    path = [cur]
    for i in range(n - 1, 0, -1):
        cur = layers[i][cur][1] or UNKNOWN
        path.append(cur)
    return path[::-1]


def summarize(tracks: list[dict], books: list[Book], path: list[Place]) -> list[dict]:
    """Runs of consecutive tracks in the same book, in card order."""
    runs: list[dict] = []
    for t, p in zip(tracks, path):
        if p.book < 0:
            continue
        b = books[p.book]
        if runs and runs[-1]["item_id"] == b.item_id and runs[-1]["last_track"] == t["n"] - 1:
            r = runs[-1]
        else:
            r = {"item_id": b.item_id, "title": b.title, "duration": round(b.duration), "series": b.series,
                 "first_track": t["n"], "from": p.start, "to": p.start, "exact": 0, "tracks": 0}
            runs.append(r)
        r["last_track"] = t["n"]
        r["to"] = p.start + t["duration"]
        r["tracks"] += 1
        r["exact"] += int(p.exact)
    for r in runs:
        r["whole"] = r["from"] <= tol(r["duration"]) + 2 and r["to"] >= r["duration"] - tol(r["duration"]) - 2
        r["from"], r["to"] = round(r["from"]), round(r["to"])
    return runs


# ---------------------------------------------------------------- the bridge's side

class Transfer:
    def __init__(self, br):
        self.br = br
        self.seen: dict[str, dict] = {}     # Yoto card id -> latest event from a player playing it (non-bridge cards)
        self.books: dict[str, Book] = {}
        self.on_player: dict[str, str] = {}  # player -> card it last reported

    def on_event(self, device_id: str, ev: dict, merged: dict):
        """ev: this event's own fields; merged: the player's state so far. Track and position are only
        trusted once this card's own event named the track (a just-inserted card can briefly carry the
        previous card's keys)."""
        card = merged.get("cardId")
        if not card or self.br.store.by_yoto_card(card):
            return
        if ev.get("cardId") and ev["cardId"] != self.on_player.get(device_id):
            self.on_player[device_id] = ev["cardId"]
            if "chapterKey" not in ev:
                return
        if "chapterKey" not in ev and (self.seen.get(card) or {}).get("device_id") != device_id:
            return
        if "chapterKey" not in ev and "position" not in ev and "playbackStatus" not in ev:
            return
        self.seen[card] = {"device_id": device_id, "chapterKey": merged.get("chapterKey"),
                           "trackKey": merged.get("trackKey"), "position": merged.get("position"),
                           "status": merged.get("playbackStatus"), "at": time.time()}

    async def book(self, item_id: str) -> Book:
        if item_id not in self.books:
            self.books[item_id] = Book.from_item(await self.br.abs.item(item_id))
        return self.books[item_id]

    async def candidates(self, card_title: str, tracks: list[dict], extra: list[str]) -> list[Book]:
        abs = self.br.abs
        libs = [l["id"] for l in await abs.libraries()]
        if self.br.cfg.library_ids:
            libs = [l for l in libs if l in self.br.cfg.library_ids]
        ids: list[str] = list(extra)
        for term in search_terms(card_title, tracks):
            for lib in libs:
                try:
                    data = await abs._req("GET", f"/api/libraries/{lib}/search", None, params={"q": term, "limit": 25})
                except ABSError as e:
                    log.warning("ABS search %r failed: %s", term, e)
                    continue
                for b in data.get("book") or []:
                    ids.append(b["libraryItem"]["id"])
                for s in data.get("series") or []:
                    ids += [x["id"] for x in s.get("books") or [] if x.get("id")]
        ids = list(dict.fromkeys(ids))[:MAX_BOOKS]
        books = [await self.book(i) for i in ids]
        # every book of every series found, so a card holding a whole series matches completely
        for b in list(books):
            for s in b.series:
                _, members = await group_books(abs, "series", s["library_id"] or b.library_id, s["id"])
                for m in members:
                    if m.item_id not in ids and len(ids) < MAX_BOOKS:
                        ids.append(m.item_id)
                        books.append(await self.book(m.item_id))
        return books

    async def analyse(self, card_id: str, extra: list[str]) -> dict:
        card = await self.br.yoto.playlist(card_id)
        tracks = card_tracks(card)
        books = await self.candidates(card.get("title", ""), tracks, extra)
        path = match(tracks, books)
        out_tracks = []
        for t, p in zip(tracks, path):
            row = {k: t[k] for k in ("n", "title", "duration")}
            if p.book >= 0:
                b = books[p.book]
                row.update(item_id=b.item_id, book=b.title, start=round(p.start, 1), exact=p.exact)
            out_tracks.append(row)
        result = {"card_id": card_id, "title": card.get("title", ""), "tracks": out_tracks,
                  "books": summarize(tracks, books, path), "player": None, "place": None}
        ev = self.seen.get(card_id)
        if ev:
            t = track_for_event(tracks, ev)
            result["player"] = {"device_id": ev["device_id"], "name": self.br.device_name(ev["device_id"]),
                                "track": t["n"] if t else None, "position": ev.get("position"),
                                "status": ev.get("status"), "at": ev["at"]}
            if t and ev.get("position") is not None:
                result["place"] = self.place(tracks, books, path, t["n"], float(ev["position"]))
        return result

    def place(self, tracks, books, path, n: int, pos: float) -> dict | None:
        p = path[n - 1]
        if p.book < 0:
            return {"track": n, "track_position": pos, "item_id": None}
        b = books[p.book]
        at = min(max(0.0, p.start + pos), max(0.0, b.duration - 5))
        before = []
        for t, q in zip(tracks[: n - 1], path[: n - 1]):
            if q.book >= 0 and q.book != p.book and books[q.book].item_id not in before:
                before.append(books[q.book].item_id)
        return {"track": n, "track_position": pos, "item_id": b.item_id, "title": b.title, "position": round(at, 1),
                "duration": b.duration, "chapter": b.chapter_at(at), "exact": p.exact, "series": b.series,
                "finish": [{"item_id": i, "title": self.books[i].title} for i in before]}

    async def where(self, card_id: str, n: int, pos: float, extra: list[str]) -> dict:
        """Place for a track chosen by hand (no player reading)."""
        card = await self.br.yoto.playlist(card_id)
        tracks = card_tracks(card)
        if not 1 <= n <= len(tracks):
            raise web.HTTPBadRequest(text="No such track on this card.")
        books = await self.candidates(card.get("title", ""), tracks, extra)
        return self.place(tracks, books, match(tracks, books), n, pos)


def register(app: web.Application, br, require_pin, point_card):
    br.transfer = Transfer(br)

    def extra(request) -> list[str]:
        return [x for x in request.query.get("add", "").split(",") if x]

    async def analyse(request):
        require_pin(request)
        return web.json_response(await br.transfer.analyse(request.match_info["card"], extra(request)))

    async def where(request):
        require_pin(request)
        q = request.query
        return web.json_response(await br.transfer.where(request.match_info["card"], int(q["track"]),
                                                         float(q.get("position") or 0), extra(request)))

    async def apply(request):
        """Save the place to ABS, then make a new card (Yoto playlist) for the book's series. The old
        playlist and its uploaded audio are left as they are."""
        require_pin(request)
        card_id = request.match_info["card"]
        body = await request.json()
        key = br.cfg.abs_key_for(body.get("device_id") or None)
        item_id, position = body["item_id"], float(body["position"])
        book = await br.transfer.book(item_id)
        # earlier books first, so the current book has the newest listening and series cards pick it
        for fid in body.get("finish") or []:
            if fid != item_id:
                fb = await br.transfer.book(fid)
                await br.abs.mark_finished(fid, fb.duration, key)
        await br.abs.patch_progress(item_id, position, book.duration, key)
        log.info("old card %s: place saved to ABS: %s at %.0fs (finished: %s)", card_id, book.title, position,
                 ", ".join(body.get("finish") or []) or "-")
        target = {"item_id": item_id}
        sid = body.get("series_id")
        if sid:
            s = next((s for s in book.series if s["id"] == sid), None) or {}
            target = {"series_id": sid, "library_id": s.get("library_id") or book.library_id,
                      "series_name": s.get("name") or body.get("series_name", "")}
        existing = next((c for c in br.store.cards() if c.yoto_card_id and (
            c.series_id == sid if sid else (not c.series_id and c.item_id == item_id))), None)
        if existing:                     # already a bridge card for it: that card resumes from ABS too
            return web.json_response({"slot": existing.slot, "title": existing.series_name or existing.title,
                                      "yoto_title": existing.yoto_title, "existing": True})
        slot = br.store.new_slot("card")
        try:
            await point_card(br, slot, target)
            await br.save_playlist(slot)
        except Exception:
            br.store.delete(slot.slot)
            raise
        return web.json_response({"slot": slot.slot, "title": slot.series_name or slot.title,
                                  "yoto_title": slot.yoto_title, "existing": False})

    app.router.add_get("/api/transfer/{card}", analyse)
    app.router.add_get("/api/transfer/{card}/where", where)
    app.router.add_post("/api/transfer/{card}/apply", apply)
