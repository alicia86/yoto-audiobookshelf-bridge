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

Yoto's limits for one MYO card (support.yotoplay.com, read 2026-10-11): 100 tracks, 60 min or
100 MB per track, 5 h or 500 MB in total. At 64 kbps mono, 5 h is about 145 MB.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from .abs_client import ABSError
from .store import Slot

log = logging.getLogger(__name__)
MAX_TRACKS = 100
MAX_TOTAL = 5 * 3600


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


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class OfflineManager:
    def __init__(self, bridge):
        self.br = bridge
        self.cfg = bridge.cfg
        self.tasks: dict[str, asyncio.Task] = {}

    # ---------------- building ----------------

    def start(self, slot: Slot, hours: float | None = None, speed: float | None = None) -> dict:
        if slot.kind != "card" or not slot.item_id or not slot.yoto_card_id:
            raise ValueError("Only a card with a book assigned can be made offline.")
        if slot.slot in self.tasks and not self.tasks[slot.slot].done():
            raise ValueError("An offline copy of this card is already being made.")
        hours = min(float(hours or self.cfg.offline_hours), MAX_TOTAL / 3600)
        speed = float(speed or slot.speed or 1.0)
        if not 0.5 <= speed <= 2.5:
            raise ValueError("speed must be between 0.5 and 2.5")
        slot.offline = {"status": "building", "step": "starting", "hours": hours, "speed": speed,
                        "started_at": time.time()}
        self.br.store.save()
        self.tasks[slot.slot] = asyncio.get_running_loop().create_task(self._build(slot, hours, speed))
        return slot.offline

    async def _build(self, slot: Slot, hours: float, speed: float = 1.0):
        try:
            await self._build_inner(slot, hours, speed)
        except Exception as e:
            log.exception("offline copy of %s failed", slot.title)
            # put the stream back first, so "failed" never shows while the card is still half-changed
            try:
                await self.br.save_playlist(slot)
            except Exception:
                log.exception("could not restore the stream playlist for %s", slot.title)
            slot.offline = {**slot.offline, "status": "failed", "error": str(e)[:300]}
            self.br.store.save()

    async def _build_inner(self, slot: Slot, hours: float, speed: float = 1.0):
        br, cfg = self.br, self.cfg
        await br.pm.finish_slot(slot.slot, "making offline copy")
        key = cfg.abs_key_for(slot.device_id)
        following = await br.pm.resolve_series(slot, key) if slot.series_id else []
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
        # the current book from its position, then (series cards) the following books from the top,
        # until the hours run out
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
        segments = segments[:MAX_TRACKS]
        if not segments:
            raise RuntimeError("Nothing left to copy: the book is finished.")

        uploaded = []
        for i, seg in enumerate(segments):
            slot.offline["step"] = f"uploading track {i + 1} of {len(segments)}"
            br.store.save()
            n = int((seg["end"] - seg["start"]) * cfg.byterate / speed)
            audio = bytearray()
            src = br.encoder.stream(seg["tracks"], seg["start"], seg["end"], br.abs.auth_header(key), speed=speed)
            async for chunk in br.encoder.exact(src, n):
                audio += chunk
            info = await br.yoto.upload_audio(bytes(audio), f"{seg['book']} {i + 1}")
            info["icon"] = await br.uploaded_icon(seg["item_id"], seg["start"])
            title = seg["title"] or f"{seg['book']} · {_hms(seg['start'])}"
            if following and seg["title"]:
                title = f"{seg['book']}: {seg['title']}"
            uploaded.append({**info, "key": f"{i + 1:02d}", "title": title})

        first, last = segments[0], segments[-1]
        span = (f"{first['book']} {_hms(first['start'])} to {last['book']} {_hms(last['end'])}"
                if first["item_id"] != last["item_id"] else f"{_hms(first['start'])} to {_hms(last['end'])}")
        if speed != 1:
            span += f" at {speed:g}x"
        slot.offline["step"] = "saving the card"
        br.store.save()
        cover_url = await br.cover_for_yoto(slot.item_id)
        await br.yoto.save_track_playlist(slot.series_name or slot.title, uploaded, card_id=slot.yoto_card_id,
                                          cover_url=cover_url,
                                          description=f"Offline copy, {span}. Made by the Yoto bridge.")
        now = time.time()
        order = list(dict.fromkeys(sg["item_id"] for sg in segments))
        slot.offline = {"status": "ready", "hours": hours, "speed": speed, "start": first["start"], "end": last["end"],
                        "from_title": first["book"], "to_title": last["book"], "books": order,
                        "tracks": [{"key": u["key"], "item_id": sg["item_id"], "book": sg["book"],
                                    "book_duration": sg["book_duration"], "start": sg["start"], "end": sg["end"]}
                                   for u, sg in zip(uploaded, segments)],
                        "made_at": now, "cur_item": first["item_id"], "last_pos": first["start"],
                        "last_write_wall": now, "last_event_at": None}
        br.store.save()
        log.info("offline copy of %s ready: %s in %d tracks", slot.series_name or slot.title, span, len(uploaded))

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
        if not off or off.get("status") != "ready" or merged.get("position") is None:
            return
        key = merged.get("chapterKey") or merged.get("trackKey")
        seg = next((t for t in off["tracks"] if t["key"] == key), None)
        if not seg:
            return
        speed = float(off.get("speed") or 1.0)        # the player's position is in seconds of (sped-up) audio
        pos = seg["start"] + min(max(0.0, float(merged["position"])) * speed, seg["end"] - seg["start"])
        off["last_pos"], off["last_event_at"], off["device_id"] = pos, time.time(), device_id
        off["last_book"] = seg.get("book", "")
        status = merged.get("playbackStatus")
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
