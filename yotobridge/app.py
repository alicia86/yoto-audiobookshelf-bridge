"""HTTP server: the stream endpoint the Yoto players fetch, the JSON API behind the phone page,
and the wiring between ABS, Yoto and the playback manager."""
from __future__ import annotations

import asyncio
import hashlib
import html
import shutil
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web

from .abs_client import ABSClient, ABSError
from .alerts import Alerts
from . import chapter_api
from . import transfer
from . import chapters as chapter_tracks
from . import icons
from .icon_themes import IconLibrary
from .ai_pictures import AIPictures, AISettings
from .offline import OfflineManager, holds_copy, progress as offline_progress
from .series import CollectionIndex, _series_entries, group_books
from .config import Config
from .playback import PlaybackManager
from .quiet import WINDOW as QUIET_WINDOW, QuietSpots
from .localprogress import LocalProgress
from .sources import Sources
from .store import Store
from .streamer import Encoder
from .yoto import YotoAPI, YotoAuth, YotoControl, YotoLoginError, YotoNotLoggedIn

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
BRIDGE = web.AppKey("bridge", object)
# Under /s/ so whatever publishes the streams (a proxy or tunnel forwarding /s/) also carries the Yoto login redirect.
CALLBACK_PATH = "/s/yoto-callback"


class Bridge:
    def __init__(self, cfg: Config, http: aiohttp.ClientSession, control_factory=None):
        self.cfg = cfg
        self.http = http
        self.store = Store(cfg.data_dir)
        self.abs = Sources(ABSClient(http, cfg.abs_url, cfg.abs_api_key), LocalProgress(cfg.data_dir))
        self.encoder = Encoder(cfg.bitrate_kbps, cfg.channels, cfg.sample_rate)
        self.quiet = QuietSpots(cfg.data_dir)
        self.auth = YotoAuth(http, cfg.yoto_client_id, cfg.yoto_login, cfg.yoto_api,
                             Path(cfg.data_dir) / "yoto_tokens.json",
                             redirect_uri=cfg.yoto_redirect_uri or f"{cfg.public_url}{CALLBACK_PATH}")
        self.yoto = YotoAPI(http, self.auth, cfg.yoto_api)
        self.icon_library = IconLibrary(self.yoto, cfg.data_dir)
        self.ai = AIPictures(self, AISettings.parse(cfg.ai_pictures))
        self.pm = PlaybackManager(cfg, self.abs, self.store)
        factory = control_factory or (lambda: YotoControl(self.auth, cfg.card_uri_format, self.on_yoto_event))
        self.control = factory()
        self.pm.control = self.control
        self.pm.encoder = self.encoder
        self.pm.known_devices = lambda: [d["deviceId"] for d in self.yoto_devices]
        self.pm.on_finished = lambda slot_id: self.pm._bg(self.relayout(slot_id))
        self.offline = OfflineManager(self)
        self.collection_index = CollectionIndex(self.abs)
        self.alerts = Alerts(self)
        self.yoto_devices: list[dict] = []
        self._cover16: dict[str, bytes | None] = {}
        self._chapters: dict[str, list] = {}
        self._durations: dict[str, float] = {}
        self.seeking: set[str] = set()
        self.jumping: dict[str, tuple[int, float]] = {}   # slot -> (track the player was told to start, when)
        self.icon_upload_error = ""
        self.last_error = ""
        self.transfer = None                # old MYO card -> ABS (transfer.py), set by make_app

    def stream_url(self, slot: str) -> str:
        return f"{self.cfg.public_url}/s/{slot}.mp3"

    def chapter_mode(self, slot) -> bool:
        return bool(self.cfg.chapter_tracks and self.cfg.resume_mode == "server" and slot.item_id)

    def playlist_url(self, slot) -> str:
        """What slot.stream_url should be: the single stream, or (chapter tracks) the slot's track prefix."""
        return f"{self.cfg.public_url}/s/{slot.slot}/" if self.chapter_mode(slot) else self.stream_url(slot.slot)

    # ---------------- icon styles ----------------

    def icon_style(self, slot) -> str:
        """The card's own icon style, else the default. A card set to its own picture without one
        uploaded uses the default."""
        own = slot.icon_style if slot and slot.kind == "card" else ""
        if own == "custom":
            if slot.icon_custom or slot.icon_chapters:
                return own
            own = ""
        style = own or self.store.state.icon_style
        return style if style in icons.STYLES and style != "custom" else icons.DEFAULT_STYLE

    def icon_tag(self, slot) -> str:
        """Part of the icon URLs that changes with the look, so the Yoto fetches new icons instead of
        showing cached ones. Empty for the default style, which keeps the URLs cards were made with."""
        style = self.icon_style(slot)
        if style == icons.DEFAULT_STYLE:
            return ""
        extra = (str(self.store.state.icon_allow_all) if style == "theme"
                 else f"{slot.icon_custom}|{sorted(slot.icon_chapters.items())}|{self.fallback_style()}"
                 if style == "custom" else "")
        return hashlib.sha1(f"{style}|{extra}".encode()).hexdigest()[:8]

    def _icon_base(self, slot) -> str | None:
        if not self.cfg.chapter_icons or self.icon_style(slot) == "none":
            return None
        tag = self.icon_tag(slot)
        return f"{self.cfg.public_url}/s/icon/{tag}" if tag else f"{self.cfg.public_url}/s/icon"

    def icon_url(self, slot_id: str) -> str:
        base = self._icon_base(self.store.get(slot_id))
        return f"{base}/{slot_id}.png" if base else ""

    def track_icon_url(self, slot, layout_id: str, k: int) -> str | None:
        base = self._icon_base(slot)
        return f"{base}/{slot.slot}/{layout_id}/{k}.png" if base else None

    def custom_icon_path(self, slot, chapter_key: str = "") -> Path:
        """The card's own picture, or (chapter_key "<item>:<n>") the picture of one chapter."""
        d = Path(self.cfg.data_dir) / "icons"
        return d / slot.slot / f"{chapter_key.replace(':', '-')}.rgba" if chapter_key else d / f"{slot.slot}.rgba"

    def fallback_style(self) -> str:
        """What a custom card shows for a chapter with no picture of its own and no card picture."""
        style = self.store.state.icon_style
        return style if style in icons.STYLES and style not in ("custom", "none") else icons.DEFAULT_STYLE

    def custom_picture(self, slot, item_id: str, number: int | None) -> tuple[str, Path] | None:
        """(hash, file) of the picture a custom card shows for a chapter: the chapter's own, else the card's."""
        key = f"{item_id}:{number}"
        if number and key in slot.icon_chapters:
            return slot.icon_chapters[key], self.custom_icon_path(slot, key)
        if slot.icon_custom:
            return slot.icon_custom, self.custom_icon_path(slot)
        return None

    async def apply_icon_style(self, slots=None):
        """Rewrite the playlists whose icon URLs no longer match their style. A card that is playing
        (or holds an offline copy) keeps its icons until it stops (relayout) or goes back to streaming."""
        for slot in list(slots if slots is not None else self.store.state.slots.values()):
            if (not slot.yoto_card_id or not slot.stream_url or slot.offline or slot.slot in self.pm.by_slot
                    or slot.icon_url == self.icon_url(slot.slot)):
                continue
            try:
                await self.save_playlist(slot, cover=False)
            except Exception as e:
                log.warning("could not change the icons of %s: %s", slot.yoto_title or slot.slot, e)

    # ---------------- chapter icons ----------------

    async def chapters(self, item_id: str) -> list:
        if item_id not in self._chapters:
            item = await self.abs.item(item_id)
            self._chapters[item_id] = item.get("media", {}).get("chapters") or []
            self._durations[item_id] = float(item.get("media", {}).get("duration") or 0)
        return self._chapters[item_id]

    async def book_info(self, item_id: str) -> tuple[float, list]:
        chapters = await self.chapters(item_id)
        return self._durations.get(item_id, 0.0), chapters

    # ---------------- chapters as tracks ----------------

    def snapper(self, item_id: str):
        """Where a book's cuts inside chapters move to, from the pauses already found (quiet.py)."""
        return self.quiet.snapper(item_id) if self.cfg.snap_cuts else None

    async def snap_for(self, item_id: str, key, plan, budget: float | None = None):
        """Find the pauses near the cuts plan(snap) makes inside chapters, if not found before, and
        return the snapper for the book. plan is run once with a snap that only notes the cuts. With a
        budget (seconds), the cuts not looked at in time keep their planned place until the next call."""
        if not self.cfg.snap_cuts:
            return None
        planned: list[float] = []
        plan(lambda t: (planned.append(t), t)[1])
        todo = self.quiet.missing(item_id, planned)
        if todo:
            tracks = self.pm._tracks(await self.abs.item_tracks(item_id, key))
            auth = self.abs.auth_header(key)
            end = (tracks[-1].start + tracks[-1].duration) if tracks else 0.0
            t0, gate, found = time.monotonic(), asyncio.Semaphore(4), []

            async def look(t: float):
                async with gate:
                    if budget is not None and time.monotonic() - t0 > budget:
                        return
                    try:
                        spots = await asyncio.wait_for(self.encoder.quiet_spots(
                            tracks, max(0.0, t - QUIET_WINDOW), min(end, t + QUIET_WINDOW), auth), 60)
                    except Exception as e:       # not this time: the planned cut stands, tried again next time
                        log.warning("could not look for a pause near %.0fs of %s: %s", t, item_id,
                                    e or type(e).__name__)
                        return
                    self.quiet.add(item_id, (t - QUIET_WINDOW, t + QUIET_WINDOW), spots)
                    found.append(t)

            await asyncio.gather(*(look(t) for t in todo))
            self.quiet.save()
            log.info("looked for pauses near %d of %d cuts inside chapters of %s in %.0fs", len(found), len(todo),
                     item_id, time.monotonic() - t0)
        return self.snapper(item_id)

    async def build_layout(self, slot) -> list:
        key = self.pm.key_for(slot, slot.device_id)
        following = await self.pm.resolve_series(slot, key) if slot.series_id else []
        prog = await self.abs.progress(slot.item_id, key) or {}
        duration, chapters = await self.book_info(slot.item_id)
        pos = float(prog.get("currentTime") or 0)
        if prog.get("isFinished") or (duration and pos >= duration - 10):
            pos = 0.0
        books = [(slot.item_id, slot.title, duration, chapters, pos)]
        for b in following[:20]:
            d, ch = await self.book_info(b.item_id)
            books.append((b.item_id, b.title, d or b.duration, ch, 0.0))
        part, snaps = self.cfg.part_seconds, {}
        seen = 0
        for item_id, _t, d, ch, _p in books:
            if seen >= chapter_tracks.MAX_TRACKS:
                break
            snaps[item_id] = await self.snap_for(          # a card being set up waits at most ~20 s
                item_id, key, lambda sn, ch=ch, d=d: chapter_tracks.chapter_spans(ch, d, part, sn), budget=20)
            seen += len(chapter_tracks.chapter_spans(ch, d, part))
        return chapter_tracks.make_layout(books, part, snaps)

    def track_title(self, seg, multi_book: bool) -> str:
        label = f"{seg.number}. {seg.title}" if seg.chaptered and not seg.title.lower().startswith(("chapter", "part")) else seg.title
        return f"{seg.book}: {label}" if multi_book else label

    async def relayout(self, slot_id: str):
        """After a listen: rewrite the card's track list so track 1 is the chapter the listener is in now."""
        slot = self.store.get(slot_id)
        if (not slot or not slot.yoto_card_id or slot.offline or not self.chapter_mode(slot)
                or slot.slot in self.pm.by_slot or slot.slot in self.seeking):
            return
        try:
            if (chapter_tracks.same_layout(slot, await self.build_layout(slot))
                    and slot.icon_url == self.icon_url(slot.slot)):
                return                               # same book(s): keep the numbering the player remembers
            await self.save_playlist(slot, cover=False)
        except Exception as e:
            log.warning("could not update the chapter tracks of %s: %s", slot.title or slot.slot, e)

    async def cover16(self, item_id: str) -> bytes | None:
        if item_id not in self._cover16:
            try:
                self._cover16[item_id] = await icons.shrink_cover(await self.abs.cover(item_id))
            except ABSError:
                self._cover16[item_id] = None
        return self._cover16[item_id]

    async def theme_icon(self, slot, item_id: str, pos: float | None) -> dict | None:
        """The Yoto library icon matching the title of the chapter at pos (theme style), or None."""
        chapters = await self.chapters(item_id)
        number = icons.chapter_number(chapters, pos) if pos is not None else None
        if not number or not await self.icon_library.load():
            return None
        return self.icon_library.match(chapters[number - 1].get("title") or "", self.store.state.icon_allow_all)

    async def icon_png(self, slot, item_id: str, pos: float | None, style: str | None = None) -> bytes:
        """The icon of the chapter at pos (None: no chapter number) in the card's style."""
        style = style or self.icon_style(slot)
        if style == "custom":
            number = icons.chapter_number(await self.chapters(item_id), pos) if pos is not None else None
            pic = self.custom_picture(slot, item_id, number) if slot else None
            try:
                if pic:
                    return icons.png(pic[1].read_bytes())
            except OSError:
                pass
            style = self.fallback_style()
        if style == "theme":
            ic = await self.theme_icon(slot, item_id, pos)
            raw = await self.icon_library.png(ic) if ic else None
            px = await icons.shrink_cover(raw) if raw else None
            if px:
                return icons.png(px)
            style = icons.DEFAULT_STYLE          # no matching icon (e.g. "Chapter 7"): cover and number
        chapters = await self.chapters(item_id)
        number = icons.chapter_number(chapters, pos) if pos is not None else None
        fraction = None
        duration = self._durations.get(item_id) or 0
        if pos is not None and duration:
            start = float(chapters[number - 1].get("start", 0)) if number else pos
            fraction = min(1.0, max(0.0, start / duration))
        return icons.render_style(style, await self.cover16(item_id), number, fraction, seed=f"{item_id}:{number or 0}")

    async def uploaded_icon(self, slot, item_id: str, pos: float) -> str | None:
        """An uploaded Yoto icon for the chapter at pos (for offline copies), reused across copies.
        None when uploads are not allowed (the Yoto app lacks the user:icons:manage scope)."""
        style = self.icon_style(slot)
        if not self.cfg.chapter_icons or style == "none":
            return None
        if style == "theme":
            ic = await self.theme_icon(slot, item_id, pos)
            if ic:
                return f"yoto:#{ic['mediaId']}"     # Yoto's own icon: nothing to upload
        if self.icon_upload_error:
            return None
        number = icons.chapter_number(await self.chapters(item_id), pos)
        tag = self.icon_tag(slot)
        pic = self.custom_picture(slot, item_id, number) if style == "custom" else None
        key = (f"custom:{pic[0]}" if pic else
               f"{icons.STYLE}:{tag + ':' if tag else ''}{item_id}:{number or 0}")   # a new icon style uploads afresh
        if key not in self.store.state.icons:
            try:
                ref = await self.yoto.upload_icon(await self.icon_png(slot, item_id, pos), f"abs-{item_id[:8]}-{number or 0}")
            except Exception as e:
                self.icon_upload_error = str(e)[:200]
                log.warning("chapter icons for offline copies are off: icon upload failed (%s). "
                            "Add the user:icons:manage scope to the Yoto app and press Connect Yoto again.", e)
                return None
            self.store.state.icons[key] = ref
            self.store.save()
        return self.store.state.icons[key]

    async def connect_yoto(self):
        """After login (or at startup): find the players, connect MQTT, make one remote playlist per player."""
        try:
            self.yoto_devices = await self.yoto.devices()
            ids = [d["deviceId"] for d in self.yoto_devices]
            for d in self.yoto_devices:
                log.info("Yoto player %s: %s (online=%s)", d["deviceId"], d.get("name"), d.get("online"))
            for d in self.yoto_devices:
                await self.ensure_remote_slot(d["deviceId"], d.get("name") or d["deviceId"])
            await self.refresh_playlist_urls()
            if ids:
                await self.control.connect(ids)
            self.last_error = ""
            self.offline.resume_interrupted()        # copies a restart cut short carry on
        except YotoNotLoggedIn:
            pass
        except Exception as e:
            self.last_error = f"Yoto connect failed: {e}"
            log.exception("Yoto connect failed")

    async def save_playlist(self, slot, cover: bool = True):
        """Write a slot's Yoto playlist: title, stream URL(s) and (for cards) the ABS cover."""
        title = (slot.series_name or slot.title) if slot.kind == "card" else slot.yoto_title
        cover_url = await self.cover_for_yoto(slot.item_id) if (cover and slot.kind == "card" and slot.item_id) else None
        description = ("" if slot.kind == "card" else
                       "Used by the Yoto bridge to play any Audiobookshelf book on this player. No card needed.")
        if self.chapter_mode(slot):
            segs = await self.build_layout(slot)
            lid = chapter_tracks.store_layout(slot, segs)
            multi = len({s.item_id for s in segs}) > 1
            base = f"{self.cfg.public_url}/s"
            tracks = [{"key": f"{i:02d}", "title": self.track_title(seg, multi), "url": f"{base}/{slot.slot}/{lid}/{i}.mp3",
                       "icon_url": self.track_icon_url(slot, lid, i)}
                      for i, seg in enumerate(segs, 1)]
            slot.yoto_card_id = await self.yoto.save_stream_tracks_playlist(
                title, tracks, card_id=slot.yoto_card_id or None, cover_url=cover_url, description=description)
            slot.yoto_title, slot.stream_url, slot.icon_url = title, self.playlist_url(slot), self.icon_url(slot.slot)
            self.store.save()
            log.info("Yoto playlist %s: %d chapter tracks from %s", title, len(tracks),
                     f"{segs[0].book} ch. {segs[0].number}" if segs else "-")
            return
        slot.yoto_card_id = await self.yoto.save_stream_playlist(
            title, self.stream_url(slot.slot), card_id=slot.yoto_card_id or None, cover_url=cover_url,
            description="" if slot.kind == "card" else
            "Used by the Yoto bridge to play any Audiobookshelf book on this player. No card needed.",
            icon_url=self.icon_url(slot.slot) or None)
        slot.yoto_title, slot.stream_url, slot.icon_url = title, self.stream_url(slot.slot), self.icon_url(slot.slot)
        self.store.save()

    async def refresh_playlist_urls(self):
        """If public_url changed (new IP, or http -> https after test a), rewrite every playlist once."""
        for slot in list(self.store.state.slots.values()):
            if slot.offline:
                continue                 # holds an offline copy; the stream URL goes back on "Back to streaming"
            if slot.yoto_card_id and slot.stream_url and (slot.stream_url != self.playlist_url(slot)
                                                          or slot.icon_url != self.icon_url(slot.slot)):
                log.info("updating Yoto playlist %s to %s", slot.yoto_title, self.playlist_url(slot))
                await self.save_playlist(slot)
            elif slot.yoto_card_id and self.chapter_mode(slot) and slot.slot not in self.pm.by_slot:
                try:
                    if not chapter_tracks.same_layout(slot, await self.build_layout(slot)):
                        log.info("updating the chapter tracks of %s", slot.yoto_title)
                        await self.save_playlist(slot, cover=False)
                except Exception as e:
                    log.warning("could not check the chapter tracks of %s: %s", slot.yoto_title, e)

    async def ensure_remote_slot(self, device_id: str, name: str):
        slot = self.store.remote_for(device_id)
        if slot and slot.yoto_card_id:
            return slot
        slot = slot or self.store.new_slot("remote", device_id=device_id)
        slot.yoto_title = f"Audiobookshelf on {name}"
        await self.save_playlist(slot)
        return slot

    def on_yoto_event(self, device_id: str, ev: dict):
        self.pm.on_event(device_id, ev)
        self.offline.on_event(device_id, self.pm.devices.get(device_id, {}))
        if self.transfer:
            self.transfer.on_event(device_id, ev, self.pm.devices.get(device_id, {}))

    async def cover_for_yoto(self, item_id: str) -> str | None:
        try:
            image = await icons.to_jpeg(await self.abs.cover(item_id))
            if not image:
                log.warning("cover for %s is in a format ffmpeg cannot read", item_id)
                return None
            return await self.yoto.upload_cover(image)
        except Exception as e:
            log.warning("cover for %s unavailable: %s", item_id, e)
            return None

    def device_name(self, device_id: str) -> str:
        if device_id in self.cfg.devices and self.cfg.devices[device_id].name:
            return self.cfg.devices[device_id].name
        d = next((d for d in self.yoto_devices if d.get("deviceId") == device_id), None)
        return (d or {}).get("name") or device_id


# ---------------------------------------------------------------- stream endpoint

def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    a, _, b = spec.partition("-")
    if a == "":                      # suffix range: last N bytes
        n = int(b)
        return max(0, total - n), total - 1
    start = int(a)
    end = int(b) if b else total - 1
    return start, min(end, total - 1)


async def stream_icon(request: web.Request) -> web.Response:
    """The 16x16 icon a streaming card's track points at: the book's cover with the number of the chapter
    being listened to (or, when the card is idle, the chapter ABS has it at). Public wherever /s/ is published;
    the slot id is unguessable and the icon shows nothing beyond a tiny cover and a number."""
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    if not slot or not slot.item_id:
        raise web.HTTPNotFound()
    pb = br.pm.by_slot.get(slot.slot)
    if pb:
        book, pos = br.pm.book_position(pb)
        item_id = book.item_id
    else:
        item_id, pos = slot.item_id, 0.0
        try:
            prog = await br.abs.progress(item_id, br.pm.key_for(slot, slot.device_id)) or {}
            pos = float(prog.get("currentTime") or 0)
        except ABSError:
            pass
    body = await br.icon_png(slot, item_id, pos)
    return web.Response(body=body, content_type="image/png", headers={"Cache-Control": "no-cache"})


async def stream_track_icon(request: web.Request) -> web.Response:
    """The icon of one chapter track: the book's cover with that chapter's number (fixed per track)."""
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    layout = chapter_tracks.current_layout_for(slot, request.match_info["layout"]) if slot else ""
    try:
        seg = chapter_tracks.segment(slot, layout, int(request.match_info["k"])) if slot else None
    except ValueError:
        seg = None
    if not seg:
        raise web.HTTPNotFound()
    body = await br.icon_png(slot, seg.item_id, seg.start + 0.5)
    return web.Response(body=body, content_type="image/png", headers={"Cache-Control": "max-age=86400"})


async def stream(request: web.Request) -> web.StreamResponse:
    """The whole book (or series) as one stream track, from the ABS position. Cards made before
    chapter tracks, and every card when chapter_tracks is off."""
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    if not slot or not slot.item_id:
        raise web.HTTPNotFound(text="No book is assigned to this card. Assign one on the bridge page.")
    ip = request.remote

    async def head_length() -> int:
        # Report a length without opening an ABS session.
        device = slot.device_id or br.cfg.device_for_ip(ip) or br.pm._device_from_events(slot.yoto_card_id)
        key = br.pm.key_for(slot, device)
        following = await br.pm.resolve_series(slot, key) if slot.series_id else []
        prog = await br.abs.progress(slot.item_id, key) or {}
        item = await br.abs.item(slot.item_id)
        dur = float(item.get("media", {}).get("duration") or prog.get("duration") or 0) + sum(b.duration for b in following)
        base = float(prog.get("currentTime") or 0) if br.cfg.resume_mode == "server" else 0
        return int(max(0, dur - base) * br.cfg.byterate / br.pm.speed_for(slot, device))

    return await _respond(request, br, slot, lambda start_byte: br.pm.open_stream(slot, start_byte, ip), head_length)


async def resolve_track(br: Bridge, slot, layout_id: str, k: int, probe: bool = False) -> dict | None:
    """What chapter track k of a layout plays now (rules in chapters.py). Returns the request for
    open_track, or {"jump": track} when a fresh tap asked for a track other than the listener's.
    probe: a HEAD request, which the player sends before the GET; it must not use up a one-time
    "no rewind" (exact_next), or the GET right after it would rewind."""
    seg = chapter_tracks.segment(slot, layout_id, k)
    if not seg:
        return None
    req = {"layout": layout_id, "track": k, "item_id": seg.item_id, "title": seg.book, "start": seg.start,
           "end": seg.end, "book_duration": seg.book_duration}
    pb = br.pm.by_slot.get(slot.slot)
    pending = br.jumping.get(slot.slot)              # (track the player was told to start, when)
    if pending and time.monotonic() - pending[1] >= 15:
        br.jumping.pop(slot.slot, None)              # the player never followed: stop waiting for it
        pending = None
    if pending:
        if pending[0] != k:
            return {"jump": pending[0], "pending": True}   # still moving: more silence, no second command
    elif pb and not br.pm.card_out_since_start(pb):
        if pb.layout == layout_id and pb.track == k and pb.item_id == seg.item_id:
            # the same track again: a Yoto hangs up on pause and asks for the track afresh on resume
            # (no Range header), so carry on from where it was rather than from the top of the track
            # where it paused: after a card goes back in, the player reports "playing" seconds before it
            # asks for the audio, and the clock would count that wait as listening
            pos = pb.pause_pos if pb.pause_pos is not None else br.pm.position(pb)
            if seg.start < pos < seg.end - 1:
                back = br.pm.pause_rewind(pb)
                req["start"] = max(seg.start, pos - back)
                if not probe:
                    log.info("%s: resuming track %d at %.0fs (%.0fs back)", seg.book, k, req["start"], back)
        return req                                   # within a listen: next/previous/auto-advance, whole chapter
    key = br.pm.key_for(slot, slot.device_id or br.pm._device_from_events(slot.yoto_card_id))
    if slot.series_id:
        await br.pm.resolve_series(slot, key)        # the book ABS says is current
    item = slot.item_id if slot.series_id else seg.item_id
    prog = await br.abs.progress(item, key) or {}
    duration, chapters = await br.book_info(item)
    pos = float(prog.get("currentTime") or 0)
    if prog.get("isFinished"):
        pos = duration
    exact = slot.slot in br.pm.exact_next            # just moved there with the phone's seek buttons: no rewind
    start = pos if exact else br.pm._resume_point(pos, duration)
    if item == seg.item_id and seg.start <= start < seg.end - 1:
        if not probe:
            br.pm.exact_next.discard(slot.slot)
            br.jumping.pop(slot.slot, None)          # the player is where it should be
        req["start"] = max(seg.start, start)         # a plain tap on the listener's chapter: resume
        return req
    if holds_copy(slot) and layout_id == slot.layout_id:
        # an offline copy still uploading: its uploaded tracks play from the card, so a request for a
        # streamed one is the listener choosing it (skip, next track), never a stale resume to correct
        if not probe:
            br.pm.exact_next.discard(slot.slot)
            br.jumping.pop(slot.slot, None)
        return req
    right = chapter_tracks.track_for(slot, item, start) if layout_id == slot.layout_id else None
    if right and right != k and br.control.connected and not pending:
        log.info("%s: the player asked for track %d but the listener is in track %d; moving it there",
                 slot.title, k, right)
        return {"jump": right}
    # no way to move the player (or it ignored the move): play from the listener's spot under this track
    if not probe:
        br.pm.exact_next.discard(slot.slot)
    spans = chapter_tracks.chapter_spans(chapters, duration, br.cfg.part_seconds, br.snapper(item))
    end = spans[chapter_tracks.index_at(spans, start)][1] if spans else duration
    req.update(item_id=item, title=slot.title if slot.series_id else seg.book, start=start,
               end=max(end, start + 1), book_duration=duration)
    log.info("%s: track %d plays from the listener's spot at %.0fs", slot.title, k, start)
    return req


async def _jump(br: Bridge, slot, track: int):
    """Tell the player that just tapped this card to start `track` instead (its card id event may
    arrive a moment after the request for audio)."""
    br.jumping[slot.slot] = (track, time.monotonic())
    try:
        device = slot.device_id
        for _ in range(40):
            device = device or br.pm._device_from_events(slot.yoto_card_id, within=15)
            if device:
                break
            await asyncio.sleep(0.1)
        if not device:
            log.warning("%s: could not tell which player to move to track %d", slot.title, track)
            return
        br.pm.expect(device, slot.yoto_card_id)
        await br.control.play(device, slot.yoto_card_id, None, track=track)
    except Exception:
        br.jumping.pop(slot.slot, None)
        raise


async def stream_track(request: web.Request) -> web.StreamResponse:
    """One chapter track: /s/<slot>/<layout>/<k>.mp3."""
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    try:
        k = int(request.match_info["k"])
    except ValueError:
        raise web.HTTPNotFound()
    if not slot:
        raise web.HTTPNotFound()
    layout = chapter_tracks.current_layout_for(slot, request.match_info["layout"])
    if layout != request.match_info["layout"]:
        log.info("%s: player asked for an old track list (%s); serving the current one", slot.title,
                 request.match_info["layout"])
    req = await resolve_track(br, slot, layout, k, probe=request.method == "HEAD")
    if not req:
        raise web.HTTPNotFound(text="This track list is out of date; take the card out and put it back in.")
    if "jump" in req:
        # a few seconds of silence while the player is moved to the right track; only a real request for
        # audio sends the move (the player probes each track with HEAD first)
        silence = (await br.encoder.silence()) * 20
        if request.method == "HEAD":
            return web.Response(headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(silence)),
                                         "Accept-Ranges": "bytes", "Cache-Control": "no-store"})
        if not req.get("pending"):
            br.jumping[slot.slot] = (req["jump"], time.monotonic())
            br.pm._bg(_jump(br, slot, req["jump"]))
        return web.Response(body=silence, content_type="audio/mpeg", headers={"Cache-Control": "no-store"})
    ip = request.remote

    async def head_length() -> int:
        speed = br.pm.speed_for(slot, slot.device_id or br.pm._device_from_events(slot.yoto_card_id))
        return int(max(0.0, req["end"] - req["start"]) * br.cfg.byterate / speed)

    return await _respond(request, br, slot, lambda start_byte: br.pm.open_track(slot, start_byte, ip, req), head_length)


async def _respond(request, br: Bridge, slot, opener, head_length) -> web.StreamResponse:
    t0 = time.monotonic()
    ip = request.remote
    log.info("%s %s from %s range=%s", request.method, request.path, ip, request.headers.get("Range"))
    headers = {"Content-Type": "audio/mpeg", "Accept-Ranges": "bytes", "Cache-Control": "no-store"}

    if request.method == "HEAD":
        headers["Content-Length"] = str(await head_length())
        return web.Response(headers=headers)

    total_guess = 10**12
    rng = parse_range(request.headers.get("Range"), total_guess)
    start_byte = rng[0] if rng else 0
    if request.headers.get("Range", "").startswith("bytes=-"):
        start_byte = 1                      # suffix probe (e.g. for an ID3v1 tag): never a restart
    try:
        pb, start_sec = await opener(start_byte)
    except ABSError as e:
        log.error("ABS refused playback: %s", e)
        br.alerts.play_failed(slot.series_name or slot.title or slot.yoto_title or slot.slot, f"Audiobookshelf refused it: {e}")
        raise web.HTTPBadGateway(text=str(e))
    except Exception as e:
        log.exception("could not start %s", slot.slot)
        br.alerts.play_failed(slot.series_name or slot.title or slot.yoto_title or slot.slot, f"{type(e).__name__}: {e}")
        raise
    total = br.pm.length_bytes(pb)
    if rng:
        rng = parse_range(request.headers.get("Range"), total)
        if rng[0] >= total:
            br.pm.close_stream(pb)
            return web.Response(status=416, headers={"Content-Range": f"bytes */{total}"})
        n = rng[1] - rng[0] + 1
        end_sec = pb.base + (rng[1] + 1) / br.pm.rate(pb)
        resp = web.StreamResponse(status=206, headers={**headers, "Content-Range": f"bytes {rng[0]}-{rng[1]}/{total}"})
    else:
        n = total
        end_sec = pb.duration
        resp = web.StreamResponse(status=200, headers=headers)
    resp.content_length = n
    main = start_byte == 0
    try:
        await resp.prepare(request)
        src = br.pm.audio(br.encoder, pb, start_sec, end_sec)
        first = main
        async for chunk in br.encoder.exact(src, n):
            await resp.write(chunk)
            br.pm.on_bytes(pb, len(chunk), main)
            if first:
                first = False
                log.info("%s: first audio sent %.2fs after the request%s", pb.title or slot.slot,
                         time.monotonic() - t0, " (warm start)" if pb.warm else "")
        await resp.write_eof()
    except ConnectionResetError:
        log.info("player closed the stream for %s after %d bytes", slot.slot, pb.main_bytes)
    finally:
        br.pm.close_stream(pb)
    return resp


# ---------------------------------------------------------------- JSON API for the phone page

def require_pin(request: web.Request):
    pin = request.app[BRIDGE].cfg.ui_pin
    if pin and request.headers.get("X-Pin") != pin:
        raise web.HTTPUnauthorized(text="PIN required")


async def api_status(request):
    br: Bridge = request.app[BRIDGE]
    devices = []
    for d in br.yoto_devices:
        did = d["deviceId"]
        ev = br.pm.devices.get(did, {})
        pb = next((p for p in br.pm.by_slot.values() if p.device_id == did), None)
        book, at = br.pm.book_position(pb) if pb else (None, None)
        devices.append({"id": did, "name": br.device_name(did), "online": d.get("online"),
                        "status": ev.get("playbackStatus"),
                        "now_playing": book.title if pb else None, "item_id": book.item_id if pb else None,
                        "position": round(at) if pb else None,
                        "duration": round(book.duration) if pb else None,
                        "speed": float(br.store.state.speeds.get(did, 1.0)),
                        "playing_speed": pb.speed if pb else None})
    return web.json_response({
        "yoto_logged_in": br.auth.logged_in, "yoto_client_id_set": bool(br.cfg.yoto_client_id),
        "login_pending": br.auth.pending if br.auth.pending and br.auth.pending["expires_at"] > time.time() else None, "mqtt": br.control.connected, "devices": devices,
        "pin_required": bool(br.cfg.ui_pin), "icon_upload_error": br.icon_upload_error, "resume_mode": br.cfg.resume_mode, "error": br.last_error, "speeds": list(SPEEDS),
        "problems": br.alerts.problems, "alerts": br.alerts.enabled,
        "icons": {"styles": [{"id": k, "label": v} for k, v in icons.STYLES.items()],
                  "style": br.store.state.icon_style or icons.DEFAULT_STYLE,
                  "allow_all": br.store.state.icon_allow_all, "enabled": br.cfg.chapter_icons,
                  "ai": br.ai.info()}})


async def health(request):
    """Answered through the public address, so the alert check knows the players can reach this bridge."""
    return web.Response(text=request.app[BRIDGE].alerts.token, headers={"Cache-Control": "no-store"})


async def api_alert_test(request):
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    if not br.alerts.enabled:
        raise web.HTTPBadRequest(text="Set discord_webhook_url or ntfy_url in config.yaml and restart the bridge first.")
    ok = await br.alerts.send("Yoto bridge test", "Alerts from the Yoto bridge reach this phone.",
                              priority="default", tags="bell", event_type="test", subsystem="Yoto Bridge")
    if not ok:
        raise web.HTTPBadGateway(text="No alert destination accepted the test alert; see the bridge log.")
    await br.alerts.check_all()
    return web.json_response({"ok": True, "problems": br.alerts.problems})


async def api_yoto_login(request):
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    try:
        async def done():
            br.icon_upload_error = ""   # a new login may carry the icon scope
            await br.connect_yoto()
        return web.json_response(await br.auth.start_login(on_done=done))
    except YotoNotLoggedIn as e:
        raise web.HTTPBadRequest(text=str(e))


async def api_yoto_finish(request):
    """Fallback for when the redirect page does not load (e.g. a 127.0.0.1 callback opened on a phone):
    the user pastes the address it landed on, which carries the code and state."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    url = (await request.json()).get("url", "")
    q = parse_qs(urlsplit(url.strip()).query)
    if q.get("error"):
        raise web.HTTPBadRequest(text=f"Yoto refused the login: {q.get('error_description', q['error'])[0]}")
    if not q.get("code"):
        raise web.HTTPBadRequest(text="That address has no login code in it. Copy the whole address of the page Yoto sent you to.")
    try:
        await br.auth.finish_login(q["code"][0], (q.get("state") or [""])[0])
    except YotoLoginError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True})


CALLBACK_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yoto bridge</title><body style="font-family:system-ui,sans-serif;max-width:28em;margin:3em auto;padding:0 16px">
<h2>{title}</h2><p>{text}</p></body>"""


async def yoto_callback(request):
    """Yoto sends the browser here after sign-in. Reachable from the internet wherever /s/ is published, so
    it only completes a login this bridge started (state check) and shows nothing else."""
    br: Bridge = request.app[BRIDGE]
    q = request.query
    if q.get("error"):
        title, text, status = "Yoto login failed", f"Yoto said: {q.get('error_description') or q['error']}", 400
    else:
        try:
            await br.auth.finish_login(q.get("code", ""), q.get("state", ""))
            title, text, status = "Yoto connected", "You can close this tab and go back to the bridge page.", 200
        except YotoLoginError as e:
            title, text, status = "Yoto login failed", str(e), 400
    return web.Response(text=CALLBACK_PAGE.format(title=html.escape(title), text=html.escape(text)),
                        content_type="text/html", status=status)


def _book(item: dict, progress: dict | None = None) -> dict:
    md = item.get("media", {}).get("metadata", {})
    out = {"id": item["id"], "title": md.get("title") or item.get("title") or "",
           "author": md.get("authorName") or ", ".join(a.get("name", "") for a in md.get("authors", []) or []),
           "duration": item.get("media", {}).get("duration")}
    if progress:
        out["position"] = progress.get("currentTime")
        out["finished"] = progress.get("isFinished")
    return out


async def api_book(request):
    """One book, with the series it belongs to (for "make a series card")."""
    br: Bridge = request.app[BRIDGE]
    item = await br.abs.item(request.match_info["item"])
    out = _book(item)
    out["series"] = []
    for sr in _series_entries(item.get("media", {}).get("metadata", {}).get("series")):
        name, books = await group_books(br.abs, "series", item.get("libraryId", ""), sr["id"])
        out["series"].append({"id": sr["id"], "name": name or sr.get("name", ""), "sequence": sr.get("sequence"),
                              "library_id": item.get("libraryId", ""), "books": len(books)})
    try:
        out["collections"] = await br.collection_index.for_item(item["id"])
    except ABSError as e:
        log.warning("could not list ABS collections: %s", e)
        out["collections"] = []
    return web.json_response(out)


async def _point_card(br: Bridge, slot, body: dict):
    """Set what a card plays: a book ({"item_id"}), a whole series ({"series_id", "library_id"}) or a
    whole collection ({"collection_id"})."""
    if body.get("series_id") or body.get("collection_id"):
        slot.group_kind = "collection" if body.get("collection_id") else "series"
        slot.series_id = body.get("collection_id") or body["series_id"]
        slot.series_library = body.get("library_id", "")
        slot.series_name = body.get("series_name") or body.get("collection_name", "")
        await br.pm.resolve_series(slot, br.pm.key_for(slot, slot.device_id))
    else:
        item = await br.abs.item(body["item_id"])
        slot.series_id = slot.series_name = slot.series_library = slot.group_kind = ""
        slot.item_id, slot.title = body["item_id"], _book(item)["title"]
    br.store.save()


async def api_libraries(request):
    br: Bridge = request.app[BRIDGE]
    libs = await br.abs.libraries()
    if br.cfg.library_ids:
        libs = [l for l in libs if l["id"] in br.cfg.library_ids]
    return web.json_response([{"id": l["id"], "name": l["name"]} for l in libs])


async def api_books(request):
    br: Bridge = request.app[BRIDGE]
    lib = request.query["library"]
    if br.cfg.library_ids and lib not in br.cfg.library_ids:
        raise web.HTTPForbidden()
    q = request.query.get("q", "").strip()
    if q:
        items = await br.abs.search(lib, q)
        return web.json_response({"items": [_book(i) for i in items], "total": len(items)})
    data = await br.abs.library_items(lib, page=int(request.query.get("page", 0)))
    return web.json_response({"items": [_book(i) for i in data.get("results", [])], "total": data.get("total", 0)})


async def api_continue(request):
    br: Bridge = request.app[BRIDGE]
    items = await br.abs.items_in_progress()
    out = []
    for i in items[:12]:
        prog = await br.abs.progress(i["id"])
        if prog and not prog.get("isFinished"):
            out.append(_book(i, prog))
    return web.json_response(out)


async def api_cover(request):
    br: Bridge = request.app[BRIDGE]
    try:
        data = await br.abs.cover(request.match_info["item"])
    except ABSError:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


async def api_play(request):
    """Play any ABS book on a player, no card needed: point that player's remote slot at the book
    and tell the player (MQTT) to start its remote playlist. The stream starts at the ABS position."""
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    device_id, item_id = body["device_id"], body["item_id"]
    if not br.control.connected:
        raise web.HTTPServiceUnavailable(text="Not connected to Yoto (MQTT). Connect Yoto first.")
    slot = await br.ensure_remote_slot(device_id, br.device_name(device_id))
    # Stop whatever this player is streaming from the bridge, so its position is saved first.
    for pb in list(br.pm.by_slot.values()):
        if pb.device_id == device_id:
            await br.pm.finish_slot(pb.slot, "switched from phone")
    item = await br.abs.item(item_id)
    slot.item_id, slot.title = item_id, _book(item)["title"]
    br.store.save()
    if br.chapter_mode(slot):
        await br.save_playlist(slot, cover=False)    # the remote playlist's tracks follow the book's chapters
    seconds_in = None
    if br.cfg.resume_mode == "seconds_in":
        prog = await br.abs.progress(item_id, br.cfg.abs_key_for(device_id)) or {}
        seconds_in = max(0, int(prog.get("currentTime") or 0) - br.cfg.rewind_seconds)
    track = 1
    if br.chapter_mode(slot):
        prog = await br.abs.progress(item_id, br.cfg.abs_key_for(device_id)) or {}
        duration, _ = await br.book_info(item_id)
        at = br.pm._resume_point(float(prog.get("currentTime") or 0), duration)
        track = chapter_tracks.track_for(slot, item_id, at) or 1
    br.pm.expect(device_id, slot.yoto_card_id)
    await br.control.play(device_id, slot.yoto_card_id, seconds_in, track=track)
    return web.json_response({"ok": True, "title": slot.title})


SEEK_ACTIONS = ("back30", "fwd30", "prev", "next")


async def api_seek(request):
    """Skip on a playing Yoto from the phone: back/forward 30 s, previous/next chapter. The Yoto cannot
    seek inside a stream, so the bridge moves the ABS position and restarts the card there."""
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    action, device_id = body.get("action"), body.get("device_id")
    if action not in SEEK_ACTIONS:
        raise web.HTTPBadRequest(text="action must be one of " + ", ".join(SEEK_ACTIONS))
    if not br.control.connected:
        raise web.HTTPServiceUnavailable(text="Not connected to Yoto (MQTT). Connect Yoto first.")
    pb = br.pm.current_on(device_id)
    if not pb:
        raise web.HTTPConflict(text=f"Nothing from the bridge is playing on {br.device_name(device_id)}.")
    slot = br.store.get(pb.slot)
    book, at = br.pm.book_position(pb)
    duration, chapters = await br.book_info(book.item_id)
    duration = duration or book.duration
    spans = (chapter_tracks.chapter_spans(chapters, duration, br.cfg.part_seconds, br.snapper(book.item_id))
             if len(chapters) >= 2 else [])
    if action == "back30":
        target = max(0.0, at - 30)
    elif action == "fwd30":
        target = min(max(0.0, duration - 5), at + 30)
    elif not spans:
        raise web.HTTPConflict(text="This book has no chapters in Audiobookshelf.")
    else:
        i = chapter_tracks.index_at(spans, at)
        if action == "prev":
            target = spans[i][0] if at - spans[i][0] > 5 else spans[max(0, i - 1)][0]
        else:
            if i + 1 >= len(spans):
                raise web.HTTPConflict(text="Already in the last chapter.")
            target = spans[i + 1][0]
    track = await _restart_at(br, device_id, pb, book.item_id, target, duration, "seek from the phone")
    log.info("seek %s on %s: %.0fs -> %.0fs (track %s)", action, br.device_name(device_id), at, target, track)
    return web.json_response({"ok": True, "position": target, "title": book.title})


async def _restart_at(br: Bridge, device_id: str, pb, item_id: str, target: float, duration: float, why: str) -> int:
    """Close the playing session, put ABS at `target` and start the card again there (exactly, no
    rewind). Used by the phone's seek buttons and by speed changes, which need the audio re-encoded."""
    slot = br.store.get(pb.slot)
    br.seeking.add(slot.slot)                     # its own relayout below; skip the after-listen one
    try:
        await br.pm.finish_slot(pb.slot, why)
        await br.abs.patch_progress(item_id, target, duration, pb.abs_key)
        br.pm.exact_next.add(slot.slot)
        if br.chapter_mode(slot) and slot.yoto_card_id and not chapter_tracks.same_layout(slot, await br.build_layout(slot)):
            await br.save_playlist(slot, cover=False)    # only when the book changed: keep the track numbers
    finally:
        br.seeking.discard(slot.slot)
    track = (chapter_tracks.track_for(slot, item_id, target) if br.chapter_mode(slot) else None) or 1
    br.pm.expect(device_id, slot.yoto_card_id)
    await br.control.play(device_id, slot.yoto_card_id, None, track=track)
    return track


SPEEDS = (0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


def _parse_speed(value, allow_none: bool) -> float:
    """A speed from the page: 0.5 to 2.5 (atempo's range is wider; past 2.5 speech is hard to follow).
    allow_none: 0 or null means "no own speed" (a card that follows its player)."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="speed must be a number")
    if allow_none and v == 0:
        return 0.0
    if not 0.5 <= v <= 2.5:
        raise web.HTTPBadRequest(text="speed must be between 0.5 and 2.5")
    return round(v, 2)


async def _apply_speed(br: Bridge, pbs: list) -> None:
    """A speed changed: restart whatever is playing at the wrong speed right where it is, so the
    new speed is heard at once; and redo warm starts, which are encoded at a speed."""
    for pb in pbs:
        slot = br.store.get(pb.slot)
        if not slot or not pb.device_id or br.pm.speed_for(slot, pb.device_id) == pb.speed:
            continue
        book, at = br.pm.book_position(pb)
        if br.control.connected:
            await _restart_at(br, pb.device_id, pb, book.item_id, at, book.duration, "speed changed")
            log.info("%s on %s: speed now %gx, restarted at %.0fs", book.title, br.device_name(pb.device_id),
                     br.pm.speed_for(slot, pb.device_id), at)
    br.pm._bg(br.pm.prepare_all())


async def api_player_speed(request):
    """Playback speed of a player: every card it plays, unless the card has its own speed."""
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    device_id, speed = body.get("device_id"), _parse_speed(body.get("speed"), allow_none=False)
    if not device_id:
        raise web.HTTPBadRequest(text="device_id required")
    if speed == 1.0:
        br.store.state.speeds.pop(device_id, None)
    else:
        br.store.state.speeds[device_id] = speed
    br.store.save()
    await _apply_speed(br, [pb for pb in br.pm.by_slot.values() if pb.device_id == device_id])
    return web.json_response({"ok": True, "speed": speed})


async def api_card_speed(request):
    """A card's own playback speed, on whichever player; 0 = the player's speed."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    if not slot or slot.kind != "card":
        raise web.HTTPNotFound()
    body = await request.json()
    slot.speed = _parse_speed(body.get("speed"), allow_none=True)
    br.store.save()
    pb = br.pm.by_slot.get(slot.slot)
    await _apply_speed(br, [pb] if pb else [])
    return web.json_response({"ok": True, "speed": slot.speed})


async def api_card_own_place(request):
    """A card that keeps its own place: its listening is never written to ABS, and it resumes from the
    place the bridge keeps for it (a bedtime card left running all night leaves the book alone).
    Turning it on starts the card from where ABS has the book now; turning it off goes back to ABS's place."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    body = await request.json()
    on = bool(body.get("own_place"))
    if on == slot.own_place:
        return web.json_response({"ok": True, "own_place": on})
    if on:
        await br.pm.finish_slot(slot.slot, "now keeps its own place")    # the last ABS write, then no more
        plain = br.cfg.abs_key_for(slot.device_id)
        br.abs.local.forget(slot.slot)
        if slot.series_id:
            await br.pm.resolve_series(slot, plain)
        if slot.item_id:
            prog = await br.abs.progress(slot.item_id, plain) or {}
            br.abs.local.set(slot.slot, slot.item_id, float(prog.get("currentTime") or 0),
                             float(prog.get("duration") or 0), bool(prog.get("isFinished")))
    else:
        await br.pm.finish_slot(slot.slot, "follows ABS again")
    slot.own_place = on
    br.store.save()
    log.info("%s %s", slot.series_name or slot.title, "keeps its own place now" if on else "follows ABS again")
    return web.json_response({"ok": True, "own_place": on})


def _card(br, request):
    slot = br.store.get(request.match_info["slot"])
    if not slot or slot.kind != "card":
        raise web.HTTPNotFound()
    return slot


async def api_icon_defaults(request):
    """The icon style of every card without its own, and whether theme icons may be anything."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    if "style" in body:
        style = body["style"] or icons.DEFAULT_STYLE
        if style not in icons.STYLES or style == "custom":
            raise web.HTTPBadRequest(text="unknown icon style")
        br.store.state.icon_style = "" if style == icons.DEFAULT_STYLE else style
    if "allow_all" in body:
        br.store.state.icon_allow_all = bool(body["allow_all"])
    br.store.save()
    await br.apply_icon_style()
    return web.json_response({"ok": True, "style": br.store.state.icon_style or icons.DEFAULT_STYLE,
                              "allow_all": br.store.state.icon_allow_all})


async def api_card_icon(request):
    """A card's own icon style; "" = the default style."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    style = (await request.json()).get("style") or ""
    if style and style not in icons.STYLES:
        raise web.HTTPBadRequest(text="unknown icon style")
    if style == "custom" and not (slot.icon_custom or slot.icon_chapters):
        raise web.HTTPBadRequest(text="Choose a picture for this card first.")
    slot.icon_style = style
    br.store.save()
    await br.apply_icon_style([slot])
    return web.json_response({"ok": True, "icon_style": slot.icon_style, "playing": slot.slot in br.pm.by_slot})


async def api_card_icon_image(request):
    """A picture of the owner's choosing for a card (any image format ffmpeg reads), shrunk to
    16x16; the card switches to the custom style."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    px = await icons.shrink_cover(await request.read())
    if not px:
        raise web.HTTPBadRequest(text="That picture could not be read.")
    path = br.custom_icon_path(slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(px)
    slot.icon_custom = hashlib.sha1(px).hexdigest()[:8]
    slot.icon_style = "custom"
    br.store.save()
    await br.apply_icon_style([slot])
    return web.json_response({"ok": True, "icon_style": slot.icon_style, "playing": slot.slot in br.pm.by_slot})


async def api_card_chapter_icons(request):
    """The chapters of the book a card is on, with which have a picture of their own."""
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    if not slot.item_id:
        return web.json_response([])
    chapters = await br.chapters(slot.item_id)
    if len(chapters) < 2:
        return web.json_response([])
    return web.json_response([{"item_id": slot.item_id, "number": n, "title": c.get("title") or f"Chapter {n}",
                               "picture": slot.icon_chapters.get(f"{slot.item_id}:{n}", "")}
                              for n, c in enumerate(chapters, 1)])


def _chapter_key(br, slot, request) -> str:
    try:
        n = int(request.match_info["n"])
    except ValueError:
        raise web.HTTPBadRequest()
    item = request.match_info["item"]
    if item != slot.item_id and not any(s.get("item_id") == item for segs in slot.layouts.values() for s in segs):
        raise web.HTTPNotFound()
    return f"{item}:{n}"


async def api_card_chapter_icon(request):
    """PUT: a picture for one chapter of a card (the card switches to custom pictures); DELETE: remove it.
    GET: that picture, enlarged on the page."""
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    if request.method not in ("GET", "PUT", "DELETE"):
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "PUT", "DELETE"])
    key = _chapter_key(br, slot, request)
    path = br.custom_icon_path(slot, key)
    if request.method == "GET":
        if key not in slot.icon_chapters or not path.exists():
            raise web.HTTPNotFound()
        return web.Response(body=icons.png(path.read_bytes()), content_type="image/png", headers={"Cache-Control": "no-cache"})
    require_pin(request)
    if request.method == "DELETE":
        slot.icon_chapters.pop(key, None)
        path.unlink(missing_ok=True)
    else:
        px = await icons.shrink_cover(await request.read())
        if not px:
            raise web.HTTPBadRequest(text="That picture could not be read.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(px)
        slot.icon_chapters[key] = hashlib.sha1(px).hexdigest()[:8]
        slot.icon_style = "custom"
    br.store.save()
    await br.apply_icon_style([slot])
    return web.json_response({"ok": True, "icon_style": slot.icon_style, "playing": slot.slot in br.pm.by_slot})


async def api_card_ai_pictures(request):
    """POST: make AI pictures for the chapters of the card's book that have none ({"overwrite": true}: all);
    DELETE: stop."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    if request.method == "DELETE":
        br.ai.cancel(slot.slot)
        return web.json_response({"ok": True})
    if not slot.item_id:
        raise web.HTTPBadRequest(text="This card has no book.")
    body = await request.json() if request.can_read_body else {}
    try:
        job = br.ai.start(slot, bool(body.get("overwrite")), "card" if body.get("target") == "card" else "chapters")
    except RuntimeError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response(job)


async def api_ai_claude(request):
    """The owner's Claude API key and model for AI pictures; key "" removes it. The key is never sent back."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    try:
        br.ai.set_claude(body.get("key"), body.get("model"))
    except RuntimeError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response(br.ai.info())


async def api_card_icon_preview(request):
    """The card's icon for the chapter it is at, in its style or ?style=, for the page (shown enlarged)."""
    br: Bridge = request.app[BRIDGE]
    slot = _card(br, request)
    if not slot.item_id:
        raise web.HTTPNotFound()
    style = request.query.get("style") or None
    if style and (style not in icons.STYLES or style == "custom" and not (slot.icon_custom or slot.icon_chapters)):
        style = None
    if (style or br.icon_style(slot)) == "none":
        raise web.HTTPNotFound()
    pb = br.pm.by_slot.get(slot.slot)
    if pb:
        book, pos = br.pm.book_position(pb)
        item_id = book.item_id
    else:
        item_id, pos = slot.item_id, 0.0
        try:
            prog = await br.abs.progress(item_id, br.pm.key_for(slot, slot.device_id)) or {}
            pos = float(prog.get("currentTime") or 0)
        except ABSError:
            pass
    body = await br.icon_png(slot, item_id, pos, style)
    return web.Response(body=body, content_type="image/png", headers={"Cache-Control": "no-cache"})


async def api_control(request):
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    action, device_id = body["action"], body["device_id"]
    if action not in ("pause", "resume", "stop"):
        raise web.HTTPBadRequest()
    await getattr(br.control, action)(device_id)
    return web.json_response({"ok": True})


async def api_cards(request):
    br: Bridge = request.app[BRIDGE]
    return web.json_response([{"slot": s.slot, "item_id": s.item_id, "title": s.title,
                               "yoto_card_id": s.yoto_card_id, "yoto_title": s.yoto_title,
                               "stream_url": br.stream_url(s.slot), "offline": _offline_info(br, s),
                               "series_id": s.series_id, "series_name": s.series_name, "speed": s.speed,
                               "own_place": s.own_place,
                               "icon_style": s.icon_style, "icon_custom": bool(s.icon_custom),
                               "icon_chapters": len(s.icon_chapters), "ai_job": br.ai.status(s.slot),
                               "group_kind": (s.group_kind or "series") if s.series_id else ""}
                              for s in br.store.cards()])


def _offline_info(br, s) -> dict | None:
    o = s.offline
    if not o:
        return None
    keep = ("status", "step", "error", "mode", "hours", "speed", "start", "end", "last_pos", "last_event_at",
            "from_title", "to_title", "last_book", "books_left_off", "on_card", "done", "current", "phase")
    info = {**{k: o[k] for k in keep if k in o}, "tracks": len(o.get("tracks", []))}
    if o.get("status") == "building":
        info["progress"] = offline_progress(o)
        info["queued"] = s.slot in br.offline.waiting or bool(o.get("queued"))
        info["queue_position"] = br.offline.queue_position(s.slot)
    return info


async def api_card_offline(request):
    """Make offline: upload the next hours of the card's book (mode "next"), the whole book ("book") or the
    whole series from the current book ("series") to the card as ordinary Yoto tracks."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    if not slot or slot.kind != "card":
        raise web.HTTPNotFound()
    body = await request.json() if request.can_read_body else {}
    try:
        info = br.offline.start(slot, body.get("hours"), body.get("speed"), body.get("mode"), body.get("redo_from"))
    except ValueError as e:
        raise web.HTTPConflict(text=str(e))
    return web.json_response(info)


async def api_card_online(request):
    """Back to streaming: put the stream track back; the next tap resumes from ABS."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    if not slot or slot.kind != "card":
        raise web.HTTPNotFound()
    await br.offline.go_online(slot)
    return web.json_response({"ok": True})


async def api_card_create(request):
    """New card: create a Yoto playlist that streams a new slot. Then link it to a blank MYO card
    in the Yoto app (linking a physical card is app-only)."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    slot = br.store.new_slot("card")
    try:
        await _point_card(br, slot, body)
    except Exception:
        br.store.delete(slot.slot)
        raise
    await br.save_playlist(slot)
    return web.json_response({"slot": slot.slot, "yoto_card_id": slot.yoto_card_id})


async def api_card_convert(request):
    """Repurpose an existing MYO playlist (already linked to a physical card): replace its audio with
    a bridge stream. The physical card keeps working with no re-linking. Destroys that playlist's
    current audio, so the page asks first."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    if not body.get("confirm"):
        raise web.HTTPBadRequest(text="confirm required: this replaces the playlist's audio")
    yoto_card_id = body["yoto_card_id"]
    if br.store.by_yoto_card(yoto_card_id):
        raise web.HTTPConflict(text="That playlist is already a bridge card.")
    slot = br.store.new_slot("card", yoto_card_id=yoto_card_id)
    try:
        await _point_card(br, slot, body)
    except Exception:
        br.store.delete(slot.slot)
        raise
    await br.save_playlist(slot)
    return web.json_response({"slot": slot.slot})


async def api_card_assign(request):
    """Point an existing bridge card at a different book. Local change; the Yoto playlist's title
    and cover are updated too so the Yoto app shows the right book (best effort)."""
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    slot = br.store.get(request.match_info["slot"])
    if not slot or slot.kind != "card":
        raise web.HTTPNotFound()
    body = await request.json()
    await br.pm.finish_slot(slot.slot, "card reassigned")
    if slot.offline:
        await br.offline.go_online(slot)   # the offline copy is of the old book
    await _point_card(br, slot, body)
    try:
        await br.save_playlist(slot)
    except Exception as e:
        log.warning("Yoto title update failed (card still plays the new book): %s", e)
    return web.json_response({"ok": True})


async def api_card_delete(request):
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    await br.pm.finish_slot(request.match_info["slot"], "card removed")
    br.ai.cancel(request.match_info["slot"])
    task = br.offline.tasks.pop(request.match_info["slot"], None)
    if task:
        task.cancel()
    slot = br.store.get(request.match_info["slot"])
    if slot and slot.icon_custom:
        br.custom_icon_path(slot).unlink(missing_ok=True)
    if slot and slot.icon_chapters:
        shutil.rmtree(br.custom_icon_path(slot, "x:0").parent, ignore_errors=True)
    br.store.delete(request.match_info["slot"])
    return web.json_response({"ok": True})


async def api_yoto_playlists(request):
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    mine = await br.yoto.my_playlists()
    return web.json_response([{"cardId": c.get("cardId"), "title": c.get("title"),
                               "bridge": bool(br.store.by_yoto_card(c.get("cardId", "")))} for c in mine
                              if not c.get("deleted")])


async def api_yoto_groups(request):
    br: Bridge = request.app[BRIDGE]
    groups = await br.yoto.groups()
    return web.json_response(groups)


async def api_yoto_library(request):
    br: Bridge = request.app[BRIDGE]
    cards = await br.yoto.family_library()
    return web.json_response(cards)


async def api_yoto_card_detail(request):
    br: Bridge = request.app[BRIDGE]
    cid = request.match_info["card_id"]
    card = await br.yoto.card_detail(cid)
    if not card:
        raise web.HTTPNotFound()
    return web.json_response(card)


async def api_device_volume(request):
    require_pin(request)
    br: Bridge = request.app[BRIDGE]
    body = await request.json()
    device_id = body.get("device_id")
    volume = body.get("volume")
    if not device_id or volume is None:
        raise web.HTTPBadRequest(text="device_id and volume required")
    if br.control and br.control.connected:
        await br.control.set_volume(device_id, int(volume))
        return web.json_response({"ok": True, "device_id": device_id, "volume": int(volume)})
    return web.json_response({"ok": False, "error": "Control connection not connected"}, status=503)


async def index(request):
    if not (STATIC / "index.html").exists():
        return web.Response(status=503, text="The phone page has not been built. In web/, run: npm ci && npm run build")
    return web.FileResponse(STATIC / "index.html")


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except YotoNotLoggedIn as e:
        return web.json_response({"error": str(e)}, status=409)
    except ABSError as e:
        return web.json_response({"error": str(e)}, status=502)
    except Exception as e:
        log.exception("request failed: %s", request.path)
        return web.json_response({"error": str(e)}, status=500)


def make_app(br: Bridge) -> web.Application:
    app = web.Application(middlewares=[errors])
    app[BRIDGE] = br
    r = app.router
    r.add_get(CALLBACK_PATH, yoto_callback)
    r.add_get("/s/icon/{slot}.png", stream_icon)
    r.add_get("/s/icon/{slot}/{layout}/{k}.png", stream_track_icon)
    r.add_get("/s/icon/{tag}/{slot}.png", stream_icon)              # the same, in a non-default style
    r.add_get("/s/icon/{tag}/{slot}/{layout}/{k}.png", stream_track_icon)
    r.add_route("GET", "/s/{slot}/{layout}/{k}.mp3", stream_track)
    r.add_route("HEAD", "/s/{slot}/{layout}/{k}.mp3", stream_track)
    r.add_get("/s/health", health)
    r.add_route("GET", "/s/{slot}.mp3", stream)
    r.add_route("HEAD", "/s/{slot}.mp3", stream)
    r.add_get("/", index)
    if STATIC.is_dir():                 # built from web/ (see Dockerfile); absent in a bare checkout
        r.add_static("/static", STATIC)
    r.add_get("/api/status", api_status)
    r.add_post("/api/yoto/login", api_yoto_login)
    r.add_post("/api/yoto/finish", api_yoto_finish)
    r.add_get("/api/yoto/playlists", api_yoto_playlists)
    r.add_get("/api/yoto/groups", api_yoto_groups)
    r.add_get("/api/yoto/library", api_yoto_library)
    r.add_get("/api/yoto/card/{card_id}", api_yoto_card_detail)
    r.add_post("/api/yoto/volume", api_device_volume)
    r.add_get("/api/book/{item}", api_book)
    r.add_get("/api/libraries", api_libraries)
    r.add_get("/api/books", api_books)
    r.add_get("/api/continue", api_continue)
    r.add_get("/api/cover/{item}", api_cover)
    r.add_post("/api/play", api_play)
    r.add_post("/api/control", api_control)
    r.add_post("/api/seek", api_seek)
    r.add_post("/api/speed", api_player_speed)
    r.add_post("/api/alerts/test", api_alert_test)
    r.add_get("/api/cards", api_cards)
    r.add_post("/api/cards", api_card_create)
    r.add_post("/api/cards/convert", api_card_convert)
    r.add_put("/api/cards/{slot}", api_card_assign)
    r.add_post("/api/cards/{slot}/offline", api_card_offline)
    r.add_post("/api/cards/{slot}/online", api_card_online)
    r.add_put("/api/cards/{slot}/speed", api_card_speed)
    r.add_put("/api/cards/{slot}/own-place", api_card_own_place)
    r.add_put("/api/cards/{slot}/icon", api_card_icon)
    r.add_put("/api/cards/{slot}/icon/image", api_card_icon_image)
    r.add_get("/api/cards/{slot}/icon.png", api_card_icon_preview)
    r.add_get("/api/cards/{slot}/icon/chapters", api_card_chapter_icons)
    r.add_route("*", "/api/cards/{slot}/icon/chapters/{item}/{n}", api_card_chapter_icon)
    r.add_post("/api/icons", api_icon_defaults)
    r.add_post("/api/cards/{slot}/icon/ai", api_card_ai_pictures)
    r.add_post("/api/ai/claude", api_ai_claude)
    r.add_delete("/api/cards/{slot}/icon/ai", api_card_ai_pictures)
    r.add_delete("/api/cards/{slot}", api_card_delete)
    chapter_api.register(app, br, require_pin)
    transfer.register(app, br, require_pin, _point_card)
    return app
