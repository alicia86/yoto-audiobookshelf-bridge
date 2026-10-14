"""Listening sessions: open an ABS playback session when a Yoto starts a stream, work out where the
listener really is, write it to ABS every few seconds, close the session when they stop.

ABS stays the source of truth: every new stream starts from ABS's saved position (which the phone
app may have moved), and every position the Yoto reaches goes back to ABS.

Where the listener is comes from two independent sources:
  1. MQTT events from the player (`position`), when available. Preferred.
  2. What the bridge itself sent. The listener cannot be past the audio delivered, nor further than
     the time the player has spent playing since the stream started (wall clock less the time the
     player reported itself paused or stopped). The Yoto does not report positions while streaming,
     so this is usually the estimate: that bound less a few seconds for the player to start, or,
     without MQTT (no pause events), less the player's whole buffer. Both err toward repeating a
     few seconds rather than skipping any.
The MQTT position is always clamped by (2), which also absorbs the unknown of whether the player's
counter restarts when it reconnects.

Playback speed: a playback is encoded at one speed, fixed when it starts (the card's own speed, else
the player's). Everything here is in book seconds; the player's own clock (its MQTT position, wall
time, its buffer) runs in audio seconds, so it is multiplied by the speed, and bytes map to book
seconds at byterate / speed (see rate()). ABS gets book positions and real listening time.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .abs_client import ABSClient, ABSError
from .config import Config
from .series import current_index, group_books
from .store import Slot, Store
from .streamer import Track

log = logging.getLogger(__name__)
now = time.monotonic

STARTUP_LAG_SECONDS = 3.0            # from the request for audio to the player playing it (see position())


@dataclass
class ChainBook:
    """One book of what a stream plays. A book card's stream is one book; a series card's stream runs
    from the current book through the rest of the series, books placed end to end on one timeline."""
    item_id: str
    title: str
    duration: float
    offset: float                    # where this book starts on the stream's timeline
    tracks: list[Track] | None = None
    session: str = ""                # ABS playback session, opened when listening reaches this book


@dataclass
class Warm:
    """The first seconds of a card's audio, encoded in advance from where ABS has the book."""
    item_id: str
    key: str
    start: float
    audio: bytes
    speed: float = 1.0


@dataclass
class Playback:
    slot: str
    item_id: str
    title: str
    abs_key: str
    abs_session: str
    duration: float                  # end of the timeline (= book length for a single book)
    tracks: list[Track]
    base: float                      # timeline second at which this stream's byte 0 starts
    resume_at: float                 # ABS position when it started (equals base in server mode)
    device_id: str = ""
    opened: float = field(default_factory=now)
    main_started: float = field(default_factory=now)
    main_bytes: int = 0
    open_streams: int = 0
    ended: float | None = None       # when the last stream closed
    event: dict = field(default_factory=dict)
    event_at: float | None = None
    event_offset: float | None = None  # player counter value that corresponds to `base`
    last_pos: float = 0.0
    last_sync: float = field(default_factory=now)
    seek_sent: bool = False
    event_card: str = ""             # the Yoto card id this playback's events carry, once known
    last_write_wall: float = field(default_factory=time.time)
    chain: list[ChainBook] = field(default_factory=list)
    book_idx: int = 0                # chain index of the book the ABS session belongs to
    device_sure: bool = True         # False if the player was not known when the stream started
    warm: bytes = b""                # pre-encoded audio for [base, base + warm_start_seconds), if it matched
    playing_logged: bool = False
    layout: str = ""                 # chapter tracks: the layout and track (1-based) this stream belongs to
    track: int = 0
    started_wall: float = field(default_factory=time.time)
    speed: float = 1.0               # playback speed this stream is encoded at
    paused_at: float | None = None   # since when the player reports this stream paused or stopped
    paused_total: float = 0.0        # seconds it spent paused since the stream started


class PlaybackManager:
    def __init__(self, cfg: Config, abs_client: ABSClient, store: Store, control=None):
        self.cfg = cfg
        self.abs = abs_client
        self.store = store
        self.control = control           # YotoControl, or None if MQTT is not connected
        self.by_slot: dict[str, Playback] = {}
        self.devices: dict[str, dict] = {}   # deviceId -> merged latest event, for the web page
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self.expected: dict[str, tuple[str, float]] = {}   # Yoto card id -> (player told to start it, when)
        self.encoder = None              # set by the Bridge; needed for warm starts
        self.warm: dict[str, Warm] = {}  # slot -> pre-encoded start
        self._warming = False
        self.on_finished = None          # callback(slot id) after a listening session ends (the Bridge relayouts)
        self.exact_next: set[str] = set()  # slots whose next start is a seek target: no rewind
        self.known_devices = lambda: []  # the account's Yoto player ids (set by the Bridge)

    # ---------------- playback speed ----------------

    def speed_for(self, slot: Slot | None, device_id: str = "") -> float:
        """The card's own speed if it has one, else the player's; 1.0 if neither is set. With the
        player not known yet, the speed all players share (1.0 if they differ)."""
        if self.cfg.resume_mode != "server":
            return 1.0                       # seconds_in mode: the player seeks in its own seconds
        if slot and slot.speed:
            return float(slot.speed)
        speeds = self.store.state.speeds
        if device_id:
            return float(speeds.get(device_id, 1.0))
        found = {float(speeds.get(d, 1.0)) for d in self.known_devices()} or {float(v) for v in speeds.values()}
        return found.pop() if len(found) == 1 else 1.0

    def speeds_differ(self, slot: Slot) -> bool:
        """Whether which player taps this card changes its speed."""
        if slot.speed or self.cfg.resume_mode != "server":
            return False
        ids = set(self.known_devices()) | set(self.store.state.speeds)
        return len({float(self.store.state.speeds.get(d, 1.0)) for d in ids}) > 1

    def rate(self, pb: Playback) -> float:
        """Bytes of this playback's MP3 per second of book."""
        return self.cfg.byterate / pb.speed

    # ---------------- which player is this? ----------------
    # Needed only when players use different ABS users (devices.<id>.abs_api_key). Behind a proxy or
    # tunnel every stream request comes from the same address, so the client IP says nothing.
    # The player is known instead from: the card's own setting (remote slots), the bridge having just
    # told that player to start this card (phone page), the client IP (same LAN only), or the player's
    # MQTT event naming the card. If none of these is available when the audio starts, the bridge plays
    # from the default ABS user's position, and as soon as an event shows the card is on a player with
    # its own ABS user, it restarts the card there from that user's position.

    def expect(self, device_id: str, card_id: str):
        """The bridge is about to tell device_id to start card_id: the next stream of it is that player's."""
        if card_id:
            self.expected[card_id] = (device_id, now())

    def _device_from_events(self, card_id: str, within: float = 30) -> str:
        for dev_id, ev in self.devices.items():
            if card_id and ev.get("cardId") == card_id and time.time() - ev.get("at", 0) < within:
                return dev_id
        return ""

    async def _identify(self, slot: Slot, client_ip: str | None) -> tuple[str, bool]:
        """(player id or "", whether that is certain) for a stream of this slot starting now."""
        if slot.device_id:
            return slot.device_id, True
        dev, at = self.expected.pop(slot.yoto_card_id, ("", 0.0))
        if dev and now() - at < 60:
            return dev, True
        dev = self.cfg.device_for_ip(client_ip) or self._device_from_events(slot.yoto_card_id)
        if dev or not (self.cfg.per_player_users or self.speeds_differ(slot)):
            return dev, bool(dev)
        # The player's "card inserted" event may arrive just after its request for the audio.
        deadline = now() + self.cfg.identify_wait_seconds
        while now() < deadline:
            await asyncio.sleep(0.1)
            dev = self._device_from_events(slot.yoto_card_id)
            if dev:
                return dev, True
        return "", False

    # ---------------- stream lifecycle ----------------

    async def open_stream(self, slot: Slot, range_start: int, client_ip: str | None) -> tuple[Playback, float]:
        """Called for every GET of /s/<slot>.mp3. Returns the playback and the book second the
        response body starts at. A request from byte 0 is a (re)start: re-read ABS so a position
        set on the phone wins. A request from byte N>0 continues the current playback."""
        lock = self._locks.setdefault(slot.slot, asyncio.Lock())
        async with lock:
            pb = self.by_slot.get(slot.slot)
            if pb and pb.item_id == slot.item_id and range_start > 0:
                start = pb.base + range_start / self.rate(pb)
            else:
                if pb:
                    await self._finish(pb, "restarted")
                pb = await self._begin(slot, client_ip)
                start = pb.base + range_start / self.rate(pb)
            pb.open_streams += 1
            pb.ended = None
            if range_start == 0:
                pb.main_started = now()
                pb.main_bytes = 0
                pb.event_offset = None
                pb.paused_at, pb.paused_total = None, 0.0
            return pb, start

    async def open_track(self, slot: Slot, range_start: int, client_ip: str | None, req: dict) -> tuple[Playback, float]:
        """Like open_stream, for one chapter track: req has layout, track, item_id, title, start, end
        and book_duration, already resolved by the caller. Moving to another track of the same book
        keeps the ABS session and just re-anchors the playback on the new track."""
        lock = self._locks.setdefault(slot.slot, asyncio.Lock())
        async with lock:
            pb = self.by_slot.get(slot.slot)
            same_track = pb and pb.layout == req["layout"] and pb.track == req["track"] and pb.item_id == req["item_id"]
            if same_track and range_start > 0:
                start = pb.base + range_start / self.rate(pb)
            else:
                if pb and pb.item_id == req["item_id"] and len(pb.chain) <= 1:
                    if pb.track != req["track"]:
                        log.info("%s: track %d -> %d (%.0fs)", pb.title, pb.track, req["track"], req["start"])
                    pb.base, pb.duration, pb.resume_at = req["start"], req["end"], req["start"]
                    pb.layout, pb.track, pb.warm = req["layout"], req["track"], b""
                    pb.started_wall = time.time()
                else:
                    if pb:
                        await self._finish(pb, "next book" if pb.item_id != req["item_id"] else "restarted")
                    pb = await self._begin_track(slot, client_ip, req)
                start = pb.base + range_start / self.rate(pb)
            pb.open_streams += 1
            pb.ended = None
            if range_start == 0:
                pb.main_started = now()
                pb.main_bytes = 0
                pb.event_offset = None
                pb.paused_at, pb.paused_total = None, 0.0
            return pb, start

    async def _begin_track(self, slot: Slot, client_ip: str | None, req: dict) -> Playback:
        device_id, sure = await self._identify(slot, client_ip)
        key = self.cfg.abs_key_for(device_id)
        name = (self.cfg.devices.get(device_id).name if device_id in self.cfg.devices else "") or "Yoto"
        sess = await self.abs.play(req["item_id"], f"yoto-{device_id or slot.slot}", name, key)
        tracks = self._tracks(sess.get("audioTracks", []))
        book_duration = float(sess.get("duration") or req.get("book_duration") or 0)
        speed = self.speed_for(slot, device_id)
        warm = self.warm.get(slot.slot)
        warm_ok = (warm and warm.item_id == req["item_id"] and warm.key == key and warm.speed == speed
                   and abs(warm.start - req["start"]) < 0.6)
        start = warm.start if warm_ok else req["start"]
        chain = [ChainBook(req["item_id"], req["title"], book_duration, 0.0, tracks, sess["id"])]
        pb = Playback(slot=slot.slot, item_id=req["item_id"], title=req["title"], abs_key=key,
                      abs_session=sess["id"], duration=req["end"], tracks=tracks, base=start, resume_at=start,
                      device_id=device_id, last_pos=start, chain=chain, device_sure=sure,
                      warm=warm.audio if warm_ok else b"", layout=req["layout"], track=req["track"], speed=speed)
        if slot.series_id and slot.item_id != req["item_id"]:
            slot.item_id, slot.title = req["item_id"], req["title"]
            self.store.save()
        self.by_slot[slot.slot] = pb
        log.info("start %s track %d on %s at %.0fs (track ends %.0fs, book %.0fs; session %s)%s", pb.title,
                 req["track"], device_id or "unknown player", start, req["end"], book_duration, pb.abs_session,
                 f" at {speed:g}x" if speed != 1 else "")
        return pb

    def on_bytes(self, pb: Playback, n: int, main: bool):
        if main:
            pb.main_bytes += n

    def close_stream(self, pb: Playback):
        pb.open_streams = max(0, pb.open_streams - 1)
        if pb.open_streams == 0:
            pb.ended = now()

    async def _begin(self, slot: Slot, client_ip: str | None) -> Playback:
        device_id, sure = await self._identify(slot, client_ip)
        key = self.cfg.abs_key_for(device_id)
        name = (self.cfg.devices.get(device_id).name if device_id in self.cfg.devices else "") or "Yoto"
        following = []
        if slot.series_id:
            following = await self.resolve_series(slot, key)
        sess = await self.abs.play(slot.item_id, f"yoto-{device_id or slot.slot}", name, key)
        duration = float(sess.get("duration") or 0)
        raw = float(sess.get("startTime") or sess.get("currentTime") or 0)
        resume = raw if slot.slot in self.exact_next else self._resume_point(raw, duration)
        self.exact_next.discard(slot.slot)
        speed = self.speed_for(slot, device_id)
        warm = self.warm.get(slot.slot)
        if not (warm and self.cfg.resume_mode == "server" and warm.item_id == slot.item_id and warm.key == key
                and warm.speed == speed and abs(warm.start - resume) < 0.6):
            warm = None
        elif warm.start != resume:
            resume = warm.start                      # a fraction of a second apart: start where the audio was made
        tracks = self._tracks(sess.get("audioTracks", []))
        if not duration and tracks:
            duration = tracks[-1].start + tracks[-1].duration
        chain = [ChainBook(slot.item_id, slot.title or sess.get("displayTitle", ""), duration, 0.0, tracks, sess["id"])]
        for b in following:
            chain.append(ChainBook(b.item_id, b.title, b.duration, chain[-1].offset + chain[-1].duration))
        base = resume if self.cfg.resume_mode == "server" else 0.0
        pb = Playback(slot=slot.slot, item_id=slot.item_id, title=chain[0].title,
                      abs_key=key, abs_session=sess["id"], duration=chain[-1].offset + chain[-1].duration,
                      tracks=tracks, base=base, resume_at=resume, device_id=device_id, last_pos=resume, chain=chain,
                      device_sure=sure, warm=warm.audio if warm else b"", speed=speed)
        self.by_slot[slot.slot] = pb
        log.info("start %s on %s at %.0fs of %.0fs (session %s)%s%s", pb.title or pb.item_id,
                 device_id or "unknown player", resume, duration, pb.abs_session,
                 f", then {len(following)} more book(s) of the series" if following else "",
                 f" at {speed:g}x" if speed != 1 else "")
        return pb

    def _resume_point(self, pos: float, duration: float) -> float:
        """Where a tap starts: the ABS position less rewind_seconds; a finished book starts again."""
        if duration and pos >= duration - 10:
            pos = 0.0
        return max(0.0, pos - self.cfg.rewind_seconds) if pos > self.cfg.rewind_seconds else 0.0

    # ---------------- warm starts ----------------
    # Between a tap and the first audio, the bridge would otherwise wait for ffmpeg to start, open the
    # book over HTTP, seek and encode; on a low-power CPU that is a large part of the bridge's share of the
    # delay, and the player then waits for its buffer to fill. With the first seconds already encoded,
    # the response starts at once and the player's buffer fills at network speed while ffmpeg starts
    # on the rest. A warm start is used only if ABS still has the book where it was made from.

    async def prepare(self, slot: Slot):
        secs = self.cfg.warm_start_seconds
        if (not secs or not self.encoder or slot.offline or not slot.item_id or slot.slot in self.by_slot
                or self.cfg.resume_mode != "server"):
            return
        key = self.cfg.abs_key_for(slot.device_id)
        prog = await self.abs.progress(slot.item_id, key) or {}
        tracks = self._tracks(await self.abs.item_tracks(slot.item_id, key))
        duration = (tracks[-1].start + tracks[-1].duration) if tracks else 0.0
        pos = float(prog.get("currentTime") or 0)
        start = pos if slot.slot in self.exact_next else self._resume_point(pos, duration)
        speed = self.speed_for(slot, slot.device_id)
        w = self.warm.get(slot.slot)
        if w and w.item_id == slot.item_id and w.key == key and w.speed == speed and abs(w.start - start) < 0.5:
            return
        if not tracks or start + secs > duration:
            self.warm.pop(slot.slot, None)
            return
        n = int(secs * self.cfg.byterate / speed)     # secs of book
        audio = bytearray()
        src = self.encoder.stream(tracks, start, start + secs, self.abs.auth_header(key), speed=speed)
        async for chunk in self.encoder.exact(src, n):
            audio += chunk
        if slot.slot in self.by_slot:            # tapped while encoding: that playback started without it
            return
        self.warm[slot.slot] = Warm(slot.item_id, key, start, bytes(audio), speed)
        log.debug("warm start ready for %s at %.0fs", slot.title or slot.slot, start)

    async def prepare_all(self):
        if self._warming:
            return
        self._warming = True
        try:
            for slot in list(self.store.state.slots.values()):
                try:
                    await self.prepare(slot)
                except Exception as e:
                    log.debug("warm start for %s skipped: %s", slot.title or slot.slot, e)
        finally:
            self._warming = False

    async def _prepare_later(self, slot_id: str, delay: float = 3):
        await asyncio.sleep(delay)
        slot = self.store.get(slot_id)
        if slot:
            await self.prepare(slot)

    async def resolve_series(self, slot: Slot, key: str) -> list:
        """Point a series/collection card's slot at the book to play now; return the books after it."""
        name, books = await group_books(self.abs, slot.group_kind, slot.series_library, slot.series_id, key)
        if not books:
            raise ABSError(404, f"series {slot.series_name or slot.series_id} has no books")
        i = current_index(books, await self.abs.all_progress(key))
        if slot.item_id != books[i].item_id:
            log.info("series %s: now on book %d, %s", name or slot.series_name, i + 1, books[i].title)
        slot.item_id, slot.title = books[i].item_id, books[i].title
        slot.series_name = name or slot.series_name
        self.store.save()
        return books[i + 1:]

    def _tracks(self, raw: list[dict]) -> list[Track]:
        return [Track(self.abs.full_url(t["contentUrl"]), float(t.get("startOffset", 0)), float(t["duration"]))
                for t in raw]

    # ---------------- the timeline ----------------

    def book_at(self, pb: Playback, t: float) -> int:
        for i in range(len(pb.chain) - 1, -1, -1):
            if t >= pb.chain[i].offset:
                return i
        return 0

    async def audio(self, encoder, pb: Playback, start: float, end: float):
        """MP3 for timeline [start, end), book after book. Each book's piece is padded or cut to its
        exact byte length, so byte offsets keep mapping to the right second across books."""
        rate = self.rate(pb)
        if pb.warm and start == pb.base:
            take = min(len(pb.warm), int((end - start) * rate))
            yield pb.warm[:take]
            start += take / rate
        for b in pb.chain:
            s, e = max(start, b.offset), min(end, b.offset + b.duration)
            if e <= s:
                continue
            if b.tracks is None:
                b.tracks = self._tracks(await self.abs.item_tracks(b.item_id, pb.abs_key))
            n = int((e - start) * rate) - int((s - start) * rate)
            src = encoder.stream(b.tracks, s - b.offset, e - b.offset, self.abs.auth_header(pb.abs_key), speed=pb.speed)
            async for chunk in encoder.exact(src, n):
                yield chunk

    async def _advance(self, pb: Playback, idx: int, listened: float = 0.0):
        """Listening has crossed into a later book of the series: finish the earlier book(s) in ABS
        and open a session on the new one."""
        while pb.book_idx < idx:
            done = pb.chain[pb.book_idx]
            try:
                await self.abs.close(pb.abs_session, done.duration, listened, done.duration, pb.abs_key)
            except ABSError as e:
                log.warning("ABS close at end of %s failed: %s", done.title, e)
            await self.abs.mark_finished(done.item_id, done.duration, pb.abs_key)
            listened = 0.0
            pb.book_idx += 1
            nxt = pb.chain[pb.book_idx]
            sess = await self.abs.play(nxt.item_id, f"yoto-{pb.device_id or pb.slot}", "Yoto", pb.abs_key)
            nxt.session = sess["id"]
            if nxt.tracks is None:
                nxt.tracks = self._tracks(sess.get("audioTracks", []))
            pb.item_id, pb.title, pb.abs_session, pb.tracks = nxt.item_id, nxt.title, nxt.session, nxt.tracks
            slot = self.store.get(pb.slot)
            if slot and slot.series_id:
                slot.item_id, slot.title = nxt.item_id, nxt.title
                self.store.save()
            log.info("finished %s; series continues with %s", done.title, nxt.title)
        pb.last_write_wall = time.time()

    def book_position(self, pb: Playback) -> tuple[ChainBook, float]:
        """(book, seconds into it) for display."""
        pos = self.position(pb)
        b = pb.chain[self.book_at(pb, pos)] if pb.chain else ChainBook(pb.item_id, pb.title, pb.duration, 0.0)
        return b, pos - b.offset

    def length_bytes(self, pb: Playback) -> int:
        return int(max(0.0, pb.duration - pb.base) * self.rate(pb))

    # ---------------- MQTT events ----------------

    def on_event(self, device_id: str, ev: dict):
        merged = self.devices.setdefault(device_id, {})
        if ev.get("cardId") in ("none", ""):
            for k in ("cardId", "position", "trackLength", "trackKey", "chapterKey"):
                merged.pop(k, None)
            ev = {k: v for k, v in ev.items() if k != "cardId"}
            merged["playbackStatus"] = ev.get("playbackStatus", "stopped")
        merged.update(ev)
        merged["at"] = time.time()
        pb = self._playback_for_event(device_id, merged)
        if not pb:
            if merged.get("cardId"):
                log.debug("event for card %s on %s: not a bridge card", merged.get("cardId"), device_id)
            return
        slot = self.store.get(pb.slot)
        if not pb.device_sure and slot and merged.get("cardId") == slot.yoto_card_id:
            # Only an event naming this very card identifies the player; the 30 s fallback above
            # could be another child's shop card on the other player.
            pb.device_sure = True
            pb.device_id = device_id
            if self.cfg.abs_key_for(device_id) != pb.abs_key or self.speed_for(slot, device_id) != pb.speed:
                self._restart_as(pb, device_id, merged["cardId"])
                return
        pb.device_id = pb.device_id or device_id
        if merged.get("playbackStatus") == "playing":
            self._close_others_on(device_id, pb)
        if merged.get("playbackStatus") == "playing" and not pb.playing_logged:
            pb.playing_logged = True
            log.info("%s playing on %s, %.1fs after the player asked for the audio", pb.title or pb.item_id,
                     device_id, now() - pb.main_started)
        status = ev.get("playbackStatus")
        if status in ("paused", "stopped") and pb.paused_at is None:
            pb.paused_at = now()
        elif status == "playing" and pb.paused_at is not None:
            pb.paused_total += now() - max(pb.paused_at, pb.main_started)
            pb.paused_at = None
        pb.event = dict(merged)
        pb.event_at = now()
        if "position" in ev and pb.event_offset is None and self.cfg.position_mode == "relative":
            elapsed = now() - pb.main_started
            off = max(0.0, float(ev["position"]) - elapsed)
            pb.event_offset = off if off > 10 else 0.0   # counter carried over from before a reconnect
        self._maybe_seek(pb, device_id, merged)
        if merged.get("playbackStatus") in ("paused", "stopped"):
            self._bg(self.sync(pb, force=True))

    def _playback_for_event(self, device_id: str, merged: dict) -> Playback | None:
        card = merged.get("cardId")
        if card:
            slot = self.store.by_yoto_card(card)
            if slot and slot.slot in self.by_slot:
                pb = self.by_slot[slot.slot]
                pb.event_card = card
                return pb
        # The card id in events may not equal the playlist id (unverified). Fall back to the playback
        # this player is bound to, then to the only unbound playback started in the last 30 s, and
        # remember the card id it reports so another card's events are never mixed in.
        for pb in self.by_slot.values():
            if pb.device_id == device_id:
                if card and pb.event_card and card != pb.event_card:
                    return None
                if card and not pb.event_card:
                    pb.event_card = card
                return pb
        fresh = [pb for pb in self.by_slot.values() if not pb.device_id and now() - pb.opened < 300]
        if len(fresh) == 1 and card:
            fresh[0].event_card = card
            return fresh[0]
        return None

    def _close_others_on(self, device_id: str, keep: Playback):
        """A player plays one card at a time: once it is playing `keep`, any other session still open
        on it (e.g. waiting out the buffer of a card taken out) has ended."""
        for other in list(self.by_slot.values()):
            if other is not keep and other.device_id == device_id:
                log.info("%s: %s started on the same player, closing this session", other.title, keep.title)
                self._bg(self._finish(other, "another card started on the player"))

    def card_out_since_start(self, pb: Playback) -> bool:
        """The player reported its card taken out (or playback stopped) after this playback last started."""
        ev = self.devices.get(pb.device_id) or {}
        return (bool(pb.device_id) and ev.get("at", 0) > pb.started_wall and not ev.get("cardId")
                and ev.get("playbackStatus") == "stopped")

    def current_on(self, device_id: str) -> Playback | None:
        """The session a player is playing now: the one for the card its latest event names, else the
        most recently started one bound to it."""
        card = (self.devices.get(device_id) or {}).get("cardId")
        slot = self.store.by_yoto_card(card) if card else None
        if slot and slot.slot in self.by_slot:
            return self.by_slot[slot.slot]
        mine = [p for p in self.by_slot.values() if p.device_id == device_id]
        return max(mine, key=lambda p: p.main_started) if mine else None

    def _restart_as(self, pb: Playback, device_id: str, card_id: str):
        """The card turned out to be on a player with its own ABS user: drop this playback without
        writing anything (it started from the default user's position) and start the card again on
        that player, which makes it request the audio again, now known to be that player's."""
        name = self.cfg.devices[device_id].name if device_id in self.cfg.devices else device_id
        log.info("%s is on %s, which has its own ABS user or speed: restarting it from that user's position",
                 pb.title or pb.item_id, name or device_id)
        self.by_slot.pop(pb.slot, None)
        slot = self.store.get(pb.slot)
        if not self.control or not slot:
            return
        self.expect(device_id, slot.yoto_card_id)
        self._bg(self.control.play(device_id, slot.yoto_card_id, None))

    def _maybe_seek(self, pb: Playback, device_id: str, ev: dict):
        """seconds_in mode only: a card tap starts at 0, so jump to the ABS position once."""
        if (self.cfg.resume_mode != "seconds_in" or pb.seek_sent or not self.control
                or ev.get("playbackStatus") != "playing" or pb.resume_at < 30):
            return
        if float(ev.get("position", 0)) < 15:
            pb.seek_sent = True
            slot = self.store.get(pb.slot)
            self.expect(device_id, slot.yoto_card_id)
            self._bg(self.control.play(device_id, slot.yoto_card_id, int(pb.resume_at)))

    def _bg(self, coro):
        async def run():
            try:
                await coro
            except Exception:
                log.exception("background task failed")
        asyncio.get_running_loop().create_task(run())

    # ---------------- position ----------------

    def position(self, pb: Playback) -> float:
        # The Yoto downloads well ahead (seen: ~12 min of audio in 9 s) and closes the connection, then
        # plays from its buffer; so wall-clock time keeps counting after the stream closes, capped by
        # what was delivered.
        played = now() - pb.main_started - pb.paused_total
        if pb.paused_at is not None:
            played -= now() - max(pb.paused_at, pb.main_started)
        cap = pb.base + min(pb.main_bytes / self.rate(pb), max(0.0, played) * pb.speed)
        cap = min(cap, pb.duration) if pb.duration else cap
        ev = pb.event
        status = ev.get("playbackStatus")
        fresh = pb.event_at is not None and (now() - pb.event_at < 3 * self.cfg.sync_interval
                                             or status in ("paused", "stopped"))
        if fresh and ev.get("position") is not None:
            p = float(ev["position"])
            if status == "playing" and pb.event_at:
                p += now() - pb.event_at         # events are sparse; extrapolate while playing
            if self.cfg.position_mode == "relative":
                # never before where this stream started: the player cannot be behind its own track's start
                return max(min(pb.base + (p - (pb.event_offset or 0.0)) * pb.speed, cap), pb.base)
            # absolute positions (seconds_in mode): the player seeks itself, so the delivery bound
            # does not apply; only the book length does.
            return max(min(p, pb.duration or p), 0.0)
        # When the player sends status events, the clock above stops while it is paused or stopped, so
        # only the start-up delay is unknown (seen: under 3 s from the request for audio to playing).
        # Subtracting the whole buffer there would lose up to that much listening on every seek or
        # speed change (at 2x, the first 40 s of book).
        lag = self.cfg.player_buffer_seconds
        reports = (self.devices.get(pb.device_id) or {}).get("playbackStatus") if pb.device_id else None
        if reports and self.control is not None and getattr(self.control, "connected", False):
            lag = min(lag, STARTUP_LAG_SECONDS)
        return max(pb.base, cap - lag * pb.speed, min(pb.last_pos, cap))

    # ---------------- ABS writes ----------------

    async def _moved_elsewhere(self, pb: Playback) -> bool:
        """True if another client (the phone) wrote a different position after our last write.
        Checked only once the Yoto has stopped streaming, so a late write from the bridge never
        overwrites where the phone has got to."""
        if pb.open_streams:
            return False
        try:
            prog = await self.abs.progress(pb.item_id, pb.abs_key) or {}
        except ABSError:
            return False
        newer = float(prog.get("lastUpdate") or 0) / 1000 > pb.last_write_wall + 2
        local = pb.last_pos - (pb.chain[pb.book_idx].offset if pb.chain else 0.0)
        return newer and abs(float(prog.get("currentTime") or 0) - local) > 2

    async def sync(self, pb: Playback, force: bool = False):
        pos = self.position(pb)
        if not force and abs(pos - pb.last_pos) < 1:
            return
        if await self._moved_elsewhere(pb):
            log.info("%s was moved on another device; dropping the Yoto session without writing", pb.title)
            self.by_slot.pop(pb.slot, None)
            return
        listened = max(0.0, min((pos - pb.last_pos) / pb.speed, now() - pb.last_sync))
        idx = self.book_at(pb, pos)
        if idx > pb.book_idx:
            await self._advance(pb, idx)
            listened = 0.0
        book = pb.chain[pb.book_idx] if pb.chain else ChainBook(pb.item_id, pb.title, pb.duration, 0.0)
        local = max(0.0, pos - book.offset)
        try:
            await self.abs.sync(pb.abs_session, local, listened, book.duration, pb.abs_key)
        except ABSError as e:
            if e.status != 404:
                log.warning("ABS sync failed: %s", e)
                return
            # session gone (ABS restarted): write the position directly
            await self.abs.patch_progress(pb.item_id, local, book.duration, pb.abs_key)
        pb.last_pos, pb.last_sync, pb.last_write_wall = pos, now(), time.time()

    async def _finish(self, pb: Playback, why: str):
        pos = self.position(pb)
        listened = max(0.0, min((pos - pb.last_pos) / pb.speed, now() - pb.last_sync))
        self.by_slot.pop(pb.slot, None)
        if await self._moved_elsewhere(pb):
            log.info("stop %s: position was moved on another device, leaving it alone (%s)", pb.title, why)
            return
        idx = self.book_at(pb, pos)
        if idx > pb.book_idx:
            await self._advance(pb, idx, listened)
            listened = 0.0
        book = pb.chain[pb.book_idx] if pb.chain else ChainBook(pb.item_id, pb.title, pb.duration, 0.0)
        local = max(0.0, pos - book.offset)
        try:
            await self.abs.close(pb.abs_session, local, listened, book.duration, pb.abs_key)
        except ABSError as e:
            log.warning("ABS close failed (%s); writing progress directly", e)
            await self.abs.patch_progress(pb.item_id, local, book.duration, pb.abs_key)
        if book.duration and local >= book.duration - min(30.0, 0.02 * book.duration):   # credits/silence at the end
            await self.abs.mark_finished(pb.item_id, book.duration, pb.abs_key)
            log.info("stop %s: finished (%s)", pb.title or pb.item_id, why)
        else:
            log.info("stop %s at %.0fs (%s)", pb.title or pb.item_id, local, why)
        if self.on_finished:
            self.on_finished(pb.slot)
        self._bg(self._prepare_later(pb.slot))

    async def tick(self):
        for pb in list(self.by_slot.values()):
            try:
                if pb.open_streams == 0 and pb.ended is not None:
                    grace = self.cfg.end_grace_seconds
                    idle = now() - pb.ended
                    recent = pb.event_at is not None and now() - pb.event_at < 60
                    status = pb.event.get("playbackStatus") if recent else None
                    # audio the player still has buffered, if it kept playing since the stream started
                    buffered = pb.main_bytes / self.cfg.byterate - (now() - pb.main_started)
                    if idle > grace and (status == "stopped" or (buffered < -grace and status != "playing")):
                        await self._finish(pb, "player stopped" if status == "stopped" else "stream closed")
                        continue
                await self.sync(pb)
            except Exception:
                log.exception("sync tick failed for %s", pb.slot)

    async def run(self):
        last_warm = -1e9
        while True:
            if self.cfg.warm_start_seconds and now() - last_warm >= self.cfg.warm_refresh_seconds:
                last_warm = now()
                self._bg(self.prepare_all())
            await asyncio.sleep(self.cfg.sync_interval)
            await self.tick()

    async def finish_slot(self, slot: str, why: str):
        pb = self.by_slot.get(slot)
        if pb:
            await self._finish(pb, why)

    async def shutdown(self):
        for pb in list(self.by_slot.values()):
            try:
                await self._finish(pb, "bridge shutting down")
            except Exception:
                log.exception("close on shutdown failed")
