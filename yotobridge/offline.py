"""Offline copies: put the next few hours of a card's book onto the card as ordinary uploaded Yoto
tracks, so the player downloads them and plays without the bridge (or the internet).

A bridge card normally holds one stream track and resumes from ABS on every tap. "Make offline"
replaces that playlist with uploaded tracks covering [ABS position, + up to 5 h), cut at chapter
boundaries. While the copy is on the card, positions the player reports over MQTT (chapter key +
position within it) are mapped back to book time and written to ABS. "Back to streaming" restores
the stream track; the next tap resumes from ABS as usual.

Speed: a copy can be made at a playback speed (the Yoto plays uploaded tracks at 1x, so the speed is
baked into the audio). Yoto's limits are in audio time, so at speed s the 5 h hold 5 * s hours of book,
and a position the player reports is s times as far into the book.

Three kinds of copy (`mode`):
- "next": the next few hours from where the book was left (on a series card running on into the next
  books), as above.
- "book": the whole current book, one chapter per track, so track N is chapter N as on a streaming card
  (a chapter longer than a track can be is split into equal parts; a book with more than 99 chapters has
  neighbouring chapters share a track).
- "series": the whole series from book 1 to the end, wherever the listener is (from the current book
  only if the earlier ones would crowd it out of 99 tracks), each track as much of a book as it takes to fit everything in 99 tracks. Tracks never
  span two books, so a position always belongs to one book.
Whole copies start at the beginning, so until the player has got back to where the book was left, its
positions are not written to ABS (a fresh copy starting at track 1 never drags ABS back), and the first
time it starts earlier than that while online, the bridge moves it to the place it was left.

Yoto's limits for one MYO card (support.yotoplay.com, read 2026-10-11 and 2026-10-14): 100 tracks,
60 min or 100 MB per track, 5 h or 500 MB in total. The per-card total is not a hard limit in practice
(a whole seven-book series has been uploaded to one card by hand), so "next" keeps to its hours but
whole copies have no total limit unless `offline_max_mb` sets one. The 60 min per track is not enforced
by hand uploads either (whether Yoto's API enforces it is unknown), so whole copies have no track length
limit unless `offline_whole_track_seconds` sets one; "next" keeps its 55 min tracks. Chapters are cut as
on a streaming card, though: a book without chapters, or a "chapter" longer than an hour (a book listed
as one huge chapter), goes in 30 min parts (`part_seconds`), so no single track is a whole long book
(a 19 h track of 548 MB never finished processing at Yoto). With a track limit
set, a series too long for 99 such tracks takes as many whole books as fit, and a single book too long
for them fails with a message.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

import aiohttp

from . import chapters as chapter_tracks
from .abs_client import ABSError
from .series import group_books
from .store import Slot
from .streamer import SourceError

log = logging.getLogger(__name__)
MAX_TRACKS = 100
MAX_TOTAL = 5 * 3600
WHOLE_TRACKS = 99                 # whole-book and whole-series copies: at most 99 tracks
# Yoto's API never finished processing tracks over 2 h (seen: 96-119 min tracks done in 1-2 min, a
# 122 min one stuck three times, 2 h and 19 h ones never done), so whole copies keep under it
YOTO_MAX_TRACK = 7000             # seconds of audio
MODES = ("next", "book", "series")
LIVE_BATCH = 5           # while a copy uploads, put finished tracks on the card every 5 tracks
LIVE_SECONDS = 120       # or every 2 minutes
# Audiobookshelf (or the network) away: wait and carry on, keeping the uploaded tracks; about an hour
# in all before the copy is called failed. A track getting through starts the count again.
RETRY_WAITS = (60, 120, 300, 600, 900, 900, 900)


def _passing(e: BaseException) -> bool:
    """An error that clears up by itself: a refused or dropped connection, a time-out, a 5xx."""
    if isinstance(e, ABSError):
        return e.status >= 500
    return isinstance(e, (aiohttp.ClientConnectionError, asyncio.TimeoutError, ConnectionError, SourceError))


def plan_segments(start: float, end: float, chapters: list[dict], max_len: float) -> list[tuple[float, float, str]]:
    """Cut book time [start, end) into tracks: at chapter boundaries, long chapters split evenly into
    pieces of at most max_len, runs of very short chapters merged."""
    pieces: list[tuple[float, float, str]] = []
    chs = [c for c in chapters if float(c["end"]) > start and float(c["start"]) < end] or \
          [{"start": start, "end": end, "title": ""}]
    for c in chs:
        s, e = max(start, float(c["start"])), min(end, float(c["end"]))
        if e - s < 0.5:
            continue
        n = max(1, math.ceil((e - s) / max_len))
        step = (e - s) / n
        title = c.get("title") or ""
        for i in range(n):
            part = f"{title} ({i + 1}/{n})" if n > 1 and title else title
            pieces.append((s + i * step, e if i == n - 1 else s + (i + 1) * step, part))
    # first chapter seen may start before `start` only through the max() above; fill a gap at the
    # front (no chapter covers it) so the copy always begins exactly at `start`
    if pieces and pieces[0][0] > start + 0.5:
        pieces.insert(0, (start, pieces[0][0], ""))
    # merge runs of very short chapters (under a quarter of max_len) so a book with many tiny
    # chapters still fits Yoto's 100-track limit; normal chapters keep their own track
    short = max_len / 4
    out: list[tuple[float, float, str]] = []
    for s, e, title in pieces:
        if out and e - s < short and out[-1][1] - out[-1][0] < short and e - out[-1][0] <= max_len:
            ps, _, pt = out[-1]
            out[-1] = (ps, e, pt or title)
        else:
            out.append((s, e, title))
    return out[:MAX_TRACKS]


def chapter_units(chapters: list[dict], duration: float, max_len: float,
                  part_seconds: float = 1800) -> list[tuple[float, float, str, int]]:
    """A whole book as (start, end, title, chapter number) pieces, contiguous from 0 to the end, cut
    like a streaming card's chapter tracks (chapters.chapter_spans): one per chapter, a book without
    chapters in parts of part_seconds, and a chapter longer than two parts (a book listed as one or two
    huge "chapters") in parts of part_seconds; then anything longer than max_len split to fit."""
    spans = chapter_tracks.chapter_spans(
        sorted((c for c in chapters if 0 <= float(c.get("start", 0)) < duration), key=lambda c: float(c["start"])),
        duration, int(part_seconds))
    out = []
    for i, (s, e, title) in enumerate(spans):
        s = 0.0 if i == 0 else s
        e = duration if i == len(spans) - 1 else spans[i + 1][0]
        if e - s < 0.5:
            continue
        n = max(1, math.ceil((e - s) / max_len - 1e-9))
        step = (e - s) / n
        for k in range(n):
            part = f"{title} ({k + 1}/{n})" if n > 1 and title else title
            out.append((s + k * step, e if k == n - 1 else s + (k + 1) * step, part, i + 1))
    return out


def _pack(units, target: float, max_len: float) -> list[tuple[float, float, str]]:
    """Join neighbouring pieces into tracks of at most max(target, piece) and never over max_len."""
    out: list[list] = []
    for s, e, title, _n in units:
        if out and e - out[-1][0] <= min(target, max_len):
            out[-1][1], out[-1][3] = e, title
        else:
            out.append([s, e, title, title])
    return [(s, e, first if first == last or not (first and last) else f"{first} – {last}")
            for s, e, first, last in out]


def min_tracks(chapters: list[dict], duration: float, max_len: float, part_seconds: float = 1800) -> int:
    """The fewest tracks a whole book can be cut into with tracks of at most max_len."""
    return len(_pack(chapter_units(chapters, duration, max_len, part_seconds), max_len, max_len))


def plan_whole(books: list[tuple[list[dict], float]], max_len: float, limit: int = WHOLE_TRACKS,
               part_seconds: float = 1800) -> list[list[tuple[float, float, str]]]:
    """Tracks for whole books (chapters, duration each), per book: one chapter per track when that fits
    in `limit` tracks, otherwise the shortest track length that fits, chapters kept whole where they can
    be. Tracks never span two books."""
    units = [chapter_units(ch, dur, max_len, part_seconds) for ch, dur in books]

    def plan(target):
        return [_pack(u, target, max_len) for u in units]

    def count(target):
        return sum(len(p) for p in plan(target))

    if count(0) <= limit:
        return plan(0)
    lo, hi = 0.0, min(max_len, max((dur for _, dur in books), default=0.0))
    if count(hi) > limit:
        return plan(hi)                  # cannot fit: the caller cuts at `limit`
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if count(mid) <= limit else (mid, hi)
    return plan(hi)


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class OfflineManager:
    RETRY_WAITS = RETRY_WAITS

    def __init__(self, bridge):
        self.br = bridge
        self.cfg = bridge.cfg
        self.tasks: dict[str, asyncio.Task] = {}

    # ---------------- building ----------------

    def start(self, slot: Slot, hours: float | None = None, speed: float | None = None, mode: str | None = None,
              redo_from: int | None = None) -> dict:
        if slot.kind != "card" or not slot.item_id or not slot.yoto_card_id:
            raise ValueError("Only a card with a book assigned can be made offline.")
        if slot.slot in self.tasks and not self.tasks[slot.slot].done():
            raise ValueError("An offline copy of this card is already being made.")
        mode = mode or "next"
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if mode == "series" and not slot.series_id:
            raise ValueError("Only a series or collection card can copy the whole series.")
        hours = min(float(hours or self.cfg.offline_hours), MAX_TOTAL / 3600)
        speed = float(speed or slot.speed or 1.0)
        if not 0.5 <= speed <= 2.5:
            raise ValueError("speed must be between 0.5 and 2.5")
        # tracks already uploaded for a copy that was cut short (restart mid-upload) or failed are reused
        prev = slot.offline or {}
        done = prev.get("uploads") if prev.get("status") in ("building", "failed") else None
        if done and redo_from:
            # uploads from track redo_from on are not trusted (e.g. made while ABS was going down, before
            # short reads failed instead of being padded with silence): upload those tracks again
            for t in (prev.get("tracks") or [])[max(0, int(redo_from) - 1):]:
                ukey = f"{t['item_id']}:{t['start']:.1f}:{t['end']:.1f}:{float(prev.get('speed') or speed):g}:" \
                       f"{self.cfg.bitrate_kbps}"
                if done.pop(ukey, None):
                    log.info("offline copy of %s: track %s will be uploaded again", slot.title, t["key"])
        slot.offline = {"status": "building", "step": "starting", "mode": mode, "hours": hours, "speed": speed,
                        "started_at": time.time(), "uploads": done or {}}
        self.br.store.save()
        self.tasks[slot.slot] = asyncio.get_running_loop().create_task(self._build(slot, hours, speed, mode))
        return slot.offline

    def resume_interrupted(self):
        """At start-up: carry on with copies that were being made when the bridge stopped (a deploy or
        restart), reusing the tracks already uploaded. Until then the card still streams."""
        for slot in list(self.br.store.state.slots.values()):
            off = slot.offline or {}
            if off.get("status") != "building" or (slot.slot in self.tasks and not self.tasks[slot.slot].done()):
                continue
            log.info("carrying on with the offline copy of %s (%d tracks already uploaded)",
                     slot.series_name or slot.title, len(off.get("uploads") or {}))
            try:
                self.start(slot, off.get("hours"), off.get("speed"), off.get("mode"))
            except ValueError as e:
                slot.offline = {**off, "status": "failed", "error": str(e)[:300]}
                self.br.store.save()

    async def _build(self, slot: Slot, hours: float, speed: float = 1.0, mode: str = "next"):
        waits, had = iter(self.RETRY_WAITS), -1
        while True:
            try:
                await self._build_inner(slot, hours, speed, mode)
                return
            except Exception as e:
                have = len((slot.offline or {}).get("uploads") or {})
                if have > had:
                    waits, had = iter(self.RETRY_WAITS), have
                wait = next(waits, None) if _passing(e) else None
                if wait is None:
                    await self._failed(slot, e)
                    return
                why = str(e) or type(e).__name__
                log.warning("offline copy of %s: %s; trying again in %.0f min (%d tracks uploaded are kept)",
                            slot.series_name or slot.title, why, wait / 60, have)
                slot.offline["step"] = (f"waiting, Audiobookshelf or the network went away ({why[:120]}); "
                                        f"trying again in {wait / 60:.0f} min")
                self.br.store.save()
                await asyncio.sleep(wait)

    async def _failed(self, slot: Slot, e: BaseException):
        log.error("offline copy of %s failed", slot.title, exc_info=e)
        # put the stream back first, so "failed" never shows while the card is still half-changed
        try:
            await self.br.save_playlist(slot)
        except Exception:
            log.exception("could not restore the stream playlist for %s", slot.title)
        slot.offline = {**slot.offline, "status": "failed", "error": str(e)[:300]}
        self.br.store.save()

    async def _build_inner(self, slot: Slot, hours: float, speed: float = 1.0, mode: str = "next"):
        br, cfg = self.br, self.cfg
        await br.pm.finish_slot(slot.slot, "making offline copy")
        key = cfg.abs_key_for(slot.device_id)
        following = await br.pm.resolve_series(slot, key) if slot.series_id else []
        earlier = []
        if mode == "book":
            following = []
        elif mode == "series":
            # the whole series from book 1, wherever the listener is (they may go back to an earlier book)
            _name, books = await group_books(br.abs, slot.group_kind, slot.series_library, slot.series_id, key)
            ids = [b.item_id for b in books]
            earlier = books[:ids.index(slot.item_id)] if slot.item_id in ids else []
        sess = await br.abs.play(slot.item_id, f"yoto-offline-{slot.slot}", "Yoto (offline copy)", key)
        duration = float(sess.get("duration") or 0)
        pos = float(sess.get("startTime") or sess.get("currentTime") or 0)
        try:
            await br.abs.close(sess["id"], pos, 0, duration, key)
        except ABSError as e:
            log.warning("closing the ABS session for the offline copy failed: %s", e)
        if duration and pos >= duration - 10:
            pos = 0.0
        start = max(0.0, pos - cfg.rewind_seconds) if pos > cfg.rewind_seconds else 0.0
        tracks = br.pm._tracks(sess.get("audioTracks", []))
        if not duration and tracks:
            duration = tracks[-1].start + tracks[-1].duration
        if mode == "next":
            segments = await self._plan_next(slot, key, hours, speed, duration, tracks, start, following)
            books_left_off = 0
        else:
            segments, books_left_off = await self._plan_whole(slot, key, speed, duration, tracks, following, earlier)
        if not segments:
            raise RuntimeError("Nothing left to copy: the book is finished.")
        first, last = segments[0], segments[-1]
        titles = []
        for seg in segments:
            title = seg["title"] or f"{seg['book']} · {_hms(seg['start'])}"
            if following:
                title = f"{seg['book']}: {seg['title']}" if seg["title"] else f"{seg['book']} · {_hms(seg['start'])}"
            titles.append(title)
        # The plan goes into the state now, so positions the player reports from tracks already on the
        # card sync while the rest uploads. The card gets the copy's track list at once: tracks not
        # uploaded yet stream from the bridge (a chapter-track layout of the same stretches), and each
        # one switches to its uploaded audio as it is done (saved in batches, never while it plays).
        order = list(dict.fromkeys(sg["item_id"] for sg in segments))
        slot.offline.update({"books": order, "cur_item": first["item_id"], "last_pos": first["start"],
                             "last_write_wall": time.time(), "last_event_at": None,
                             "from_title": first["book"], "to_title": last["book"],
                             "start": first["start"], "end": last["end"], "books_left_off": books_left_off,
                             "tracks": [{"key": f"{i + 1:02d}", "item_id": sg["item_id"], "book": sg["book"],
                                         "book_duration": sg["book_duration"], "start": sg["start"], "end": sg["end"],
                                         "on_card": False} for i, sg in enumerate(segments)]})
        if first["item_id"] != slot.item_id or start > first["start"] + 1:
            slot.offline["resume"] = {"item_id": slot.item_id, "pos": start}
        numbers: dict[str, int] = {}
        live = []
        for seg, title in zip(segments, titles):
            numbers[seg["item_id"]] = numbers.get(seg["item_id"], 0) + 1
            live.append(chapter_tracks.Segment(seg["item_id"], seg["book"], seg["start"], seg["end"],
                                               numbers[seg["item_id"]], title, seg["book_duration"]))
        lid = chapter_tracks.store_layout(slot, live)
        br.store.save()
        cover_url = await br.cover_for_yoto(slot.item_id)
        name = slot.series_name or slot.title
        infos: list[dict | None] = [None] * len(segments)
        state = {"saved": -1, "at": 0.0, "ok": True}

        async def save_live(force: bool = False):
            count = sum(1 for x in infos if x)
            if not state["ok"] or count == state["saved"] or self._playing(slot):
                return
            if not force and state["saved"] >= 0 and count - state["saved"] < LIVE_BATCH \
                    and time.time() - state["at"] < LIVE_SECONDS:
                return
            base = f"{cfg.public_url}/s"
            entries = [{**info, "key": f"{i + 1:02d}", "title": titles[i]} if info else
                       {"key": f"{i + 1:02d}", "title": titles[i], "url": f"{base}/{slot.slot}/{lid}/{i + 1}.mp3",
                        "icon_url": br.track_icon_url(slot, lid, i + 1)} for i, info in enumerate(infos)]
            try:
                await br.yoto.save_mixed_playlist(name, entries, card_id=slot.yoto_card_id, cover_url=cover_url,
                                                  description=f"Offline copy being made: {count} of "
                                                              f"{len(infos)} tracks on the card, the rest stream.")
            except Exception as e:
                state["ok"] = False      # the card keeps what it has; the full copy still goes on at the end
                log.warning("could not put the offline copy's first tracks on the card (%s); "
                            "it goes on when every track is uploaded", e)
                return
            for t, info in zip(slot.offline["tracks"], infos):
                t["on_card"] = bool(info)
            slot.offline["on_card"] = count
            state["saved"], state["at"] = count, time.time()
            br.store.save()
            log.info("offline copy of %s: %d of %d tracks on the card", name, count, len(infos))

        await save_live(force=True)
        done = slot.offline.setdefault("uploads", {})
        for i, seg in enumerate(segments):
            slot.offline["step"] = f"uploading track {i + 1} of {len(segments)}"
            br.store.save()
            ukey = f"{seg['item_id']}:{seg['start']:.1f}:{seg['end']:.1f}:{speed:g}:{cfg.bitrate_kbps}"
            if ukey in done:
                info = dict(done[ukey])               # uploaded before a restart
            else:
                n = int((seg["end"] - seg["start"]) * cfg.byterate / speed)
                audio = bytearray()
                src = br.encoder.stream(seg["tracks"], seg["start"], seg["end"], br.abs.auth_header(key), speed=speed,
                                         strict=True)
                async for chunk in br.encoder.exact(src, n):
                    audio += chunk
                log.info("offline copy of %s: track %d of %d encoded (%.0f min, %.0f MB), uploading",
                         slot.series_name or slot.title, i + 1, len(segments),
                         (seg["end"] - seg["start"]) / speed / 60, len(audio) / 1e6)
                info = await br.yoto.upload_audio(bytes(audio), f"{seg['book']} {i + 1}")
                del audio
                done[ukey] = dict(info)
                br.store.save()
            info["icon"] = await br.uploaded_icon(slot, seg["item_id"], seg["start"])
            infos[i] = info
            if i + 1 < len(segments):
                await save_live()
        uploaded = [{**info, "key": f"{i + 1:02d}", "title": titles[i]} for i, info in enumerate(infos)]

        span = (f"{first['book']} {_hms(first['start'])} to {last['book']} {_hms(last['end'])}"
                if first["item_id"] != last["item_id"] else f"{_hms(first['start'])} to {_hms(last['end'])}")
        if mode == "book":
            span = f"the whole book in {len(uploaded)} tracks"
        elif mode == "series":
            span = (f"the whole series from {first['book']} in {len(uploaded)} tracks" if not books_left_off else
                    f"{first['book']} to {last['book']} in {len(uploaded)} tracks "
                    f"({books_left_off} later book{'s' if books_left_off > 1 else ''} did not fit)")
        if speed != 1:
            span += f" at {speed:g}x"
        slot.offline["step"] = "saving the card"
        br.store.save()
        await br.yoto.save_track_playlist(name, uploaded, card_id=slot.yoto_card_id, cover_url=cover_url,
                                          description=f"Offline copy, {span}. Made by the Yoto bridge.")
        now = time.time()
        prev = slot.offline
        slot.offline = {"status": "ready", "mode": mode, "hours": hours, "speed": speed,
                        "start": first["start"], "end": last["end"], "books_left_off": books_left_off,
                        "from_title": first["book"], "to_title": last["book"], "books": order,
                        "tracks": [{k: v for k, v in t.items() if k != "on_card"} for t in prev["tracks"]],
                        "made_at": now, "cur_item": prev.get("cur_item") or first["item_id"],
                        "last_pos": prev.get("last_pos", first["start"]),
                        "last_write_wall": prev.get("last_write_wall", now), "last_event_at": prev.get("last_event_at")}
        for k in ("resume", "passed_resume", "jump_sent", "last_written", "device_id", "last_book", "last_write_attempt"):
            if k in prev:
                slot.offline[k] = prev[k]
        if slot.offline.get("passed_resume"):
            br.store.save()
            log.info("offline copy of %s ready: %s in %d tracks", name, span, len(uploaded))
            return
        # where the listener is now, not when the copy was planned: the card kept streaming while the copy
        # was made, or the progress was changed (reset) in ABS meanwhile; on a series, maybe another book
        try:
            if mode == "series":
                await br.pm.resolve_series(slot, key)
            prog = await br.abs.progress(slot.item_id, key) or {}
        except ABSError:
            prog = None
        if prog is not None and slot.item_id in slot.offline.get("books", []):
            pos = 0.0 if prog.get("isFinished") else float(prog.get("currentTime") or 0)
            now_at = max(0.0, pos - cfg.rewind_seconds) if pos > cfg.rewind_seconds else 0.0
            if abs(now_at - start) > 1 or slot.item_id != (slot.offline.get("resume") or {}).get("item_id", slot.item_id):
                log.info("%s is at %.0fs now (the offline copy was planned from %.0fs)", slot.title, now_at, start)
            if slot.item_id != first["item_id"] or now_at > first["start"] + 1:
                # the copy starts earlier than the place the book was left (a whole copy starts at the top):
                # hold writes back until the player gets there, and move it there on its first play
                slot.offline["resume"] = {"item_id": slot.item_id, "pos": now_at}
            else:
                slot.offline.pop("resume", None)
        br.store.save()
        log.info("offline copy of %s ready: %s in %d tracks", slot.series_name or slot.title, span, len(uploaded))

    async def _plan_next(self, slot, key, hours, speed, duration, tracks, start, following) -> list[dict]:
        """The current book from its position, then (series cards) the following books from the top,
        until the hours run out."""
        br, cfg = self.br, self.cfg
        books = [(slot.item_id, slot.title, duration, tracks, start)] + \
                [(b.item_id, b.title, b.duration, None, 0.0) for b in following]
        left = hours * 3600 * speed                 # book seconds that fit in `hours` of audio
        max_len = cfg.offline_track_seconds * speed  # longest track, in book seconds
        segments = []
        for item_id, title, dur, trks, bstart in books:
            if left < 30 or len(segments) >= MAX_TRACKS:
                break
            bend = min(dur, bstart + left)
            if trks is None:
                trks = br.pm._tracks(await br.abs.item_tracks(item_id, key))
            chapters = (await br.abs.item(item_id, key)).get("media", {}).get("chapters") or []
            for s, e, ch in plan_segments(bstart, bend, chapters, max_len):
                segments.append({"item_id": item_id, "book": title, "book_duration": dur, "tracks": trks,
                                 "start": s, "end": e, "title": ch})
            left -= bend - bstart
        return segments[:MAX_TRACKS]

    async def _plan_whole(self, slot, key, speed, duration, tracks, following,
                          earlier=()) -> tuple[list[dict], int]:
        """Whole books: the current one, after the series' earlier books (if given) and before the
        following ones; as many whole books as fit in 99 tracks (and in offline_max_mb, when set), from
        book 1, or from the current book when the earlier ones would crowd it out. Returns the segments
        and how many books were left off."""
        br, cfg = self.br, self.cfg
        # longest track, in book seconds: under the 2 h Yoto processes, or offline_whole_track_seconds if set
        max_len = min(cfg.offline_whole_track_seconds or YOTO_MAX_TRACK, YOTO_MAX_TRACK) * speed
        cap = cfg.offline_max_mb * 1_000_000 if cfg.offline_max_mb else None
        books = [(b.item_id, b.title, b.duration, None) for b in earlier] + \
                [(slot.item_id, slot.title, duration, tracks)] + \
                [(b.item_id, b.title, b.duration, None) for b in following]
        chapter_lists: dict[str, list] = {}

        async def take(group):
            fit, chapters, size, ntracks = [], [], 0.0, 0
            for b in group:
                if b[0] not in chapter_lists:
                    chapter_lists[b[0]] = (await br.abs.item(b[0], key)).get("media", {}).get("chapters") or []
                ch = chapter_lists[b[0]]
                audio = b[2] / speed * cfg.byterate
                need = min_tracks(ch, b[2], max_len, cfg.part_seconds)
                if ntracks + need > WHOLE_TRACKS or (cap and size + audio > cap):
                    break
                fit.append(b)
                chapters.append(ch)
                size, ntracks = size + audio, ntracks + need
            return fit, chapters

        fit, chapters = await take(books)
        if earlier and slot.item_id not in [b[0] for b in fit]:
            fit, chapters = await take(books[len(earlier):])    # too long from book 1: from the current book
        if not fit:
            why = (f"over offline_max_mb ({cfg.offline_max_mb} MB)" if cap and duration / speed * cfg.byterate > cap
                   else f"more than {WHOLE_TRACKS} tracks of {cfg.offline_whole_track_seconds / 3600:g} h")
            raise RuntimeError(f"the book is {duration / 3600:.1f} h at {speed:g}x, {why}. "
                               "Use a faster speed or the next 5 hours")
        plans = plan_whole([(ch, b[2]) for ch, b in zip(chapters, fit)], max_len, part_seconds=cfg.part_seconds)
        segments = []
        for (item_id, title, dur, trks), plan in zip(fit, plans):
            if trks is None:
                trks = br.pm._tracks(await br.abs.item_tracks(item_id, key))
            for s, e, ch in plan:
                segments.append({"item_id": item_id, "book": title, "book_duration": dur, "tracks": trks,
                                 "start": s, "end": e, "title": ch})
        return segments[:WHOLE_TRACKS], len(books) - len(fit)

    def _playing(self, slot: Slot) -> bool:
        """Is the card playing right now (streaming from the bridge, or a recent "playing" event)? Its
        track list is not rewritten then, so a save never cuts the player off."""
        if slot.slot in self.br.pm.by_slot:
            return True
        return any(ev.get("cardId") == slot.yoto_card_id and ev.get("playbackStatus") == "playing"
                   and time.time() - ev.get("at", 0) < 600 for ev in self.br.pm.devices.values())

    def _before_resume(self, off: dict, item_id: str, pos: float) -> bool:
        """A whole copy: is (item_id, pos) earlier than where the book was left when the copy was made?"""
        r = off.get("resume")
        if not r or off.get("passed_resume"):
            return False
        order = off.get("books") or [r["item_id"]]
        a = order.index(item_id) if item_id in order else 0
        b = order.index(r["item_id"]) if r["item_id"] in order else 0
        return (a, pos) < (b, r["pos"] - 1)

    def _resume_target(self, off: dict) -> tuple[int, int] | None:
        """(track number, seconds into it) of the place a whole copy should start from."""
        r = off.get("resume")
        speed = float(off.get("speed") or 1.0)
        for i, t in enumerate(off["tracks"]):
            if t.get("item_id") == r["item_id"] and t["start"] <= r["pos"] < t["end"]:
                return i + 1, int((r["pos"] - t["start"]) / speed)
        return None

    # ---------------- back to streaming ----------------

    async def go_online(self, slot: Slot):
        task = self.tasks.get(slot.slot)
        if task and not task.done():
            task.cancel()
        slot.offline = {}
        self.br.store.save()
        await self.br.save_playlist(slot)

    # ---------------- positions from the player ----------------

    def on_event(self, device_id: str, merged: dict):
        card = merged.get("cardId")
        slot = self.br.store.by_yoto_card(card) if card else None
        off = slot.offline if slot else None
        if not off or off.get("status") not in ("ready", "building") or merged.get("position") is None:
            return
        key = merged.get("chapterKey") or merged.get("trackKey")
        seg = next((t for t in off.get("tracks") or [] if t["key"] == key), None)
        if not seg or seg.get("on_card") is False:
            return            # still uploading and this track streams: the stream's own sync covers it
        speed = float(off.get("speed") or 1.0)        # the player's position is in seconds of (sped-up) audio
        pos = seg["start"] + min(max(0.0, float(merged["position"])) * speed, seg["end"] - seg["start"])
        off["last_pos"], off["last_event_at"], off["device_id"] = pos, time.time(), device_id
        off["last_book"] = seg.get("book", "")
        status = merged.get("playbackStatus")
        if self._before_resume(off, seg.get("item_id") or slot.item_id, pos):
            # a whole copy started earlier than where the book was left: once, move the player there
            control = self.br.control
            target = self._resume_target(off)
            if status == "playing" and not off.get("jump_sent") and target and control and control.connected:
                off["jump_sent"] = True
                self.br.store.save()
                log.info("offline copy of %s: moving the player to track %d, %d s in", slot.title, *target)
                self.br.pm.expect(device_id, slot.yoto_card_id)
                self.br.pm._bg(control.play(device_id, slot.yoto_card_id, target[1], track=target[0]))
            return
        if off.get("resume") and not off.get("passed_resume"):
            off["passed_resume"] = True
            self.br.store.save()
        due = time.time() - (off.get("last_write_attempt") or 0) >= self.cfg.sync_interval
        if status in ("paused", "stopped") or due:
            off["last_write_attempt"] = time.time()
            self.br.pm._bg(self.write(slot, pos, device_id, seg))

    async def write(self, slot: Slot, pos: float, device_id: str = "", seg: dict | None = None):
        """Write the offline copy's position to ABS, unless another device (the phone) has moved the
        book further since the bridge last wrote: a stale offline copy never drags it backwards.
        On a series card, reaching a later book marks the books before it finished."""
        off = slot.offline
        seg = seg or off["tracks"][0]
        item_id = seg.get("item_id") or slot.item_id
        key = self.cfg.abs_key_for(device_id or off.get("device_id", ""))
        order = off.get("books") or [item_id]
        cur = off.get("cur_item") or order[0]
        if item_id != cur and item_id in order and cur in order and order.index(item_id) > order.index(cur):
            for done in order[order.index(cur):order.index(item_id)]:
                dur = next((t["book_duration"] for t in off["tracks"] if t.get("item_id") == done), 0)
                await self.br.abs.mark_finished(done, dur, key)
            off["cur_item"] = item_id
            if slot.series_id:
                slot.item_id, slot.title = item_id, seg.get("book") or slot.title
        prog = await self.br.abs.progress(item_id, key) or {}
        abs_pos = float(prog.get("currentTime") or 0)
        newer = float(prog.get("lastUpdate") or 0) / 1000 > off.get("last_write_wall", 0) + 2
        if newer and abs_pos > pos + 2:
            log.info("%s: phone is further (%.0fs) than the offline copy (%.0fs); not writing", seg.get("book"), abs_pos, pos)
            return
        if abs(abs_pos - pos) < 1:
            return
        await self.br.abs.patch_progress(item_id, pos, seg.get("book_duration") or off.get("duration") or 0, key)
        off["last_written"], off["last_write_wall"] = pos, time.time()
        self.br.store.save()
        log.info("offline copy: wrote %s at %.0fs to ABS", seg.get("book") or slot.title, pos)
