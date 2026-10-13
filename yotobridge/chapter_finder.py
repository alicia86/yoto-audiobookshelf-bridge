"""Batch chapter finder: books in ABS without real chapter markers (none, one, or just one per audio
file) get proposed chapters from Audible
(through ABS's own Audnexus lookup), and nothing is written to ABS until the listener approves a
book on the phone page's Chapters tab.

ABS endpoints used (server/routers/ApiRouter.js, v2.37):
  GET  /api/search/books?provider=audible[.<region>]&title&author&id   -> Audible matches, nearest duration first
  GET  /api/search/chapters?asin&region                                 -> Audnexus chapter data, or {"error"}
  POST /api/items/<id>/chapters {"chapters": [{id, start, end, title}]} -> needs the "update" permission
  PATCH /api/items/<id>/media {"metadata": {"asin"}}                    -> remembers an ASIN found by search
The chapter maths follows the ABS web chapter editor (client/pages/audiobook/_id/chapters.vue):
chapters starting after the audio ends are dropped, the rest are clipped to the audio, and Audible's
"brand" intro/outro is removed when that makes the Audible running time fit this audio better.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from .abs_client import ABSClient, ABSError

log = logging.getLogger(__name__)

ASIN = re.compile(r"^[A-Z0-9]{10}$")
GOOD_SECONDS = 15        # audio within this of Audible's running time: chapters line up
CHECK_SECONDS = 120      # up to this: probably the same recording, worth a look; beyond: likely another edition
TLD = {"us": ".com", "ca": ".ca", "uk": ".co.uk", "au": ".com.au", "fr": ".fr", "de": ".de", "jp": ".co.jp",
       "it": ".it", "in": ".in", "es": ".es"}            # ABS providers/Audible.js regionMap
REGIONS = tuple(TLD)
LOOKUPS_AT_ONCE = 3
# ABS's Audible search tries several title/author variants and waits out Audnexus rate limits, so one
# lookup can take well over the usual 30 s, more so with several running at once
AUDIBLE_TIMEOUT = 180


def chapter_reason(item: dict, every: bool = False) -> str:
    """Why a book's chapters are worth replacing: "none" (no chapters, or one), "files" (one chapter
    per audio file, which ABS makes by itself from file names and is rarely the real chapters),
    "own" (chapters of its own, only with `every`, to check them against Audible), or ""."""
    media = item.get("media") or {}
    tracks = media.get("numTracks") or media.get("numAudioFiles") or len(media.get("tracks") or [])
    if not tracks or not media.get("duration"):
        return ""
    n = media.get("numChapters")
    if n is None:
        n = len(media.get("chapters") or [])
    if n <= 1:
        return "none"
    if tracks > 1 and n == tracks:
        return "files"
    return "own" if every else ""


def needs_chapters(item: dict) -> bool:
    return bool(chapter_reason(item))


def same_chapters(current: list[dict], proposed: list[dict], tolerance: float = 3.0) -> bool:
    """The book's chapters already are Audible's (same count, every start within a few seconds)."""
    return (len(current) == len(proposed) and len(current) > 1
            and all(abs(float(a.get("start") or 0) - b["start"]) <= tolerance for a, b in zip(current, proposed)))


def _strip_branding(data: dict) -> tuple[list[dict], float]:
    """Audible chapter list without the "This is Audible" intro and outro, like the ABS editor's
    "Remove Audible branding" box. Returns (chapters, running time in seconds)."""
    intro = float(data.get("brandIntroDurationMs") or 0)
    outro = float(data.get("brandOutroDurationMs") or 0)
    out = []
    for i, c in enumerate(data.get("chapters") or []):
        start = float(c.get("startOffsetMs") or 0)
        start = i * 1000.0 if start < intro else start - intro
        out.append({**c, "startOffsetMs": start})
    if out and float(out[-1].get("lengthMs") or 0) <= outro:
        out.pop()
    return out, (float(data.get("runtimeLengthMs") or 0) - intro - outro) / 1000


def build_proposal(data: dict, duration: float) -> dict:
    """Chapters for audio of `duration` seconds from Audnexus chapter data, with what to check."""
    raw = list(data.get("chapters") or [])
    runtime = float(data.get("runtimeLengthMs") or 0) / 1000
    branding = bool(data.get("brandIntroDurationMs") or data.get("brandOutroDurationMs"))
    removed = False
    if branding and duration:
        stripped, stripped_runtime = _strip_branding(data)
        if abs(stripped_runtime - duration) < abs(runtime - duration):
            raw, runtime, removed = stripped, stripped_runtime, True

    chapters, dropped = [], 0
    for c in raw:
        start = float(c.get("startOffsetMs") or 0) / 1000
        if duration and start >= duration:
            dropped += 1
            continue
        end = start + float(c.get("lengthMs") or 0) / 1000
        chapters.append({"title": str(c.get("title") or f"Chapter {len(chapters) + 1}").strip(),
                         "start": round(start, 3), "end": round(min(end, duration) if duration else end, 3)})
    chapters.sort(key=lambda c: c["start"])
    for a, b in zip(chapters, chapters[1:]):      # no gaps or overlaps between neighbours
        a["end"] = b["start"]
    tail = 0.0
    if chapters and duration:
        tail = duration - chapters[-1]["end"]
        chapters[-1]["end"] = round(duration, 3)  # audio after Audible's last chapter belongs to it

    diff = duration - runtime if runtime else 0.0
    warnings = []
    if not chapters:
        warnings.append("Audible has no chapters for this book.")
    elif len(chapters) == 1:
        warnings.append("Audible has only one chapter for this book, so this changes nothing useful.")
    if runtime and abs(diff) > GOOD_SECONDS:
        warnings.append(f"Your audio is {_mmss(abs(diff))} {'longer' if diff > 0 else 'shorter'} than the Audible "
                        "edition, so chapter starts may be off" + ("; probably a different recording." if abs(diff) > CHECK_SECONDS else "."))
    if dropped:
        warnings.append(f"{dropped} Audible chapter{'s' if dropped > 1 else ''} start after your audio ends and {'were' if dropped > 1 else 'was'} left out.")
    if tail > 60:
        warnings.append(f"The last chapter was stretched by {_mmss(tail)} to reach the end of your audio.")
    if data.get("isAccurate") is False:
        warnings.append("Audible marks these chapter times as approximate.")
    quality = ("bad" if not chapters or len(chapters) == 1 or (runtime and abs(diff) > CHECK_SECONDS)
               else "check" if (runtime and abs(diff) > GOOD_SECONDS) or dropped or data.get("isAccurate") is False
               else "good")
    return {"chapters": chapters, "runtime": round(runtime, 1), "duration": round(duration, 1),
            "difference": round(diff, 1), "branding_removed": removed, "dropped": dropped,
            "warnings": warnings, "quality": quality}


def _mmss(s: float) -> str:
    s = int(round(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h {m}m" if h else f"{m}m {sec}s" if m else f"{sec}s"


def _provider(region: str) -> str:
    return "audible" if region in ("", "us") else f"audible.{region}"


class ChapterFinder:
    """Scan state lives in <data_dir>/chapters.json, so a review survives bridge restarts."""

    def __init__(self, abs_client: ABSClient, cfg, on_applied=None):
        self.abs = abs_client
        self.cfg = cfg
        self.on_applied = on_applied or (lambda item_id: None)
        self.path = Path(cfg.data_dir) / "chapters.json"
        self.books: dict[str, dict] = {}
        self.scan = {"running": False, "done": 0, "total": 0, "started_at": None, "finished_at": None, "error": ""}
        self.task: asyncio.Task | None = None
        self.edit_check: dict | None = None
        try:
            saved = json.loads(self.path.read_text())
            self.books = saved.get("books", {})
            self.scan.update({k: v for k, v in saved.get("scan", {}).items() if k != "running"})
            for b in self.books.values():         # a lookup cut short by a restart is looked up again
                if b["status"] == "looking":
                    b["status"] = "queued"
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("could not read %s (%s); starting with an empty review", self.path, e)

    @property
    def edit_key(self) -> str:
        return getattr(self.cfg, "chapters_api_key", "") or self.cfg.abs_api_key

    @property
    def region(self) -> str:
        r = (getattr(self.cfg, "audible_region", "") or "us").lower()
        return r if r in REGIONS else "us"

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"scan": self.scan, "books": self.books}))
        tmp.replace(self.path)

    # ---------------- can the key edit books? ----------------

    async def can_edit(self) -> dict:
        """Whether the ABS user behind the editing key may change books (asked once per bridge run)."""
        if self.edit_check is None:
            try:
                me = await self.abs._req("GET", "/api/me", self.edit_key)
                ok = me.get("type") in ("root", "admin") or bool((me.get("permissions") or {}).get("update"))
                self.edit_check = {"ok": ok, "user": me.get("username", "")}
            except ABSError as e:
                return {"ok": False, "user": "", "error": str(e)}
        return self.edit_check

    # ---------------- scan ----------------

    def start_scan(self, library_ids: list[str], rescan: bool = False, every: bool = False) -> bool:
        """every: also check books with chapters of their own (slow: one Audible lookup per book)."""
        if self.task and not self.task.done():
            return False
        self.task = asyncio.create_task(self._scan(library_ids, rescan, every))
        return True

    def resume(self) -> bool:
        """Carry on with lookups a restart (a deploy) cut off. Called when the bridge starts."""
        queued = [k for k, b in self.books.items() if b["status"] == "queued"]
        if not queued or (self.task and not self.task.done()):
            return False
        log.info("chapter scan: resuming %d lookups cut off by a restart", len(queued))
        self.task = asyncio.create_task(self._lookup_all(queued, resumed=True))
        return True

    async def _lookup_all(self, todo: list[str], resumed: bool = False):
        """Look books up a few at a time (ABS queues Audible calls and retries when rate-limited)."""
        if resumed:
            self.scan.update(running=True, done=0, total=len(todo), finished_at=None, error="")
        sem = asyncio.Semaphore(LOOKUPS_AT_ONCE)

        async def one(item_id):
            async with sem:
                if item_id in self.books:
                    await self.lookup(item_id)
                self.scan["done"] += 1
        try:
            await asyncio.gather(*(one(i) for i in todo))
        finally:
            if resumed:
                self.scan.update(running=False, finished_at=time.time())
                self.save()

    async def _scan(self, library_ids: list[str], rescan: bool, every: bool = False):
        self.scan.update(running=True, done=0, total=0, started_at=time.time(), finished_at=None, error="")
        try:
            found: list[dict] = []
            for lib in library_ids:
                page = 0
                while True:
                    data = await self.abs._req("GET", f"/api/libraries/{lib}/items", None, params={
                        "limit": 200, "page": page, "sort": "media.metadata.title", "minified": 1})
                    results = data.get("results", [])
                    found += [dict(i, libraryId=i.get("libraryId") or lib) for i in results if chapter_reason(i, every)]
                    page += 1
                    if len(results) < 200 or page * 200 >= int(data.get("total") or 0):
                        break
            still = {i["id"] for i in found}
            for item_id in [k for k, b in self.books.items() if k not in still and b["status"] != "applied"
                            and (every or b.get("reason") != "own") and b.get("source") != "audio"]:
                # a quick scan keeps the full check's books, and chapters found in the audio stay until applied
                del self.books[item_id]                # got chapters some other way, or left the library
            todo = []
            for item in found:
                b = self.books.get(item["id"])
                if b and b["status"] in ("applied", "skipped") and not rescan:
                    continue
                if b and b["status"] in ("ready", "matches", "no_match", "error") and not rescan:
                    continue                           # already looked up; "Look up again" redoes one
                self.books[item["id"]] = self._entry(item, b, every)
                todo.append(item["id"])
            self.scan["total"] = len(todo)
            self.save()
            log.info("chapter scan: %d books without chapters, %d to look up", len(found), len(todo))
            await self._lookup_all(todo)
        except Exception as e:
            self.scan["error"] = str(e)[:300]
            log.exception("chapter scan failed")
        finally:
            self.scan.update(running=False, finished_at=time.time())
            self.save()

    def _entry(self, item: dict, old: dict | None, every: bool = False) -> dict:
        md = (item.get("media") or {}).get("metadata") or {}
        asin = (md.get("asin") or "").strip().upper()
        manual = old and old.get("asin_source") == "manual"
        return {"item_id": item["id"], "library_id": item.get("libraryId", ""), "title": md.get("title") or "",
                "author": md.get("authorName") or "", "duration": float((item.get("media") or {}).get("duration") or 0),
                "num_chapters": (item.get("media") or {}).get("numChapters") or 0,
                "reason": chapter_reason(item, every), "current": [],
                "abs_asin": asin if ASIN.match(asin) else "",
                "asin": old["asin"] if manual else (asin if ASIN.match(asin) else ""),
                "asin_source": "manual" if manual else ("abs" if ASIN.match(asin) else ""),
                "region": (old or {}).get("region") or self.region,
                "candidates": [], "proposal": None, "status": "queued", "error": "", "updated_at": time.time()}

    # ---------------- one book ----------------

    async def lookup(self, item_id: str, asin: str | None = None, region: str | None = None) -> dict:
        """Find chapters for one book. With `asin`, use that Audible edition (set by hand on the page)."""
        b = self.books[item_id]
        if region:
            b["region"] = region if region in REGIONS else self.region
        if asin:
            b["asin"], b["asin_source"] = asin.strip().upper(), "manual"
        b.update(status="looking", error="", proposal=None, audible=None, source="audible")
        try:
            data, used = None, ""
            if b["asin"]:
                data, used = await self._chapters(b["asin"], b["region"]), b["asin"]
            if not data and b["asin_source"] != "manual":
                # no ASIN, or ABS has one without Audible chapters (a Kindle or other edition's): search
                b["candidates"] = await self._search(b)
                for c in [c for c in b["candidates"] if c["asin"] != b["asin"]][:3]:
                    data, used = await self._chapters(c["asin"], b["region"]), c["asin"]
                    if data:
                        break
            if data:
                if used != b["asin"]:
                    b["asin"], b["asin_source"] = used, "search"
                b["proposal"] = build_proposal(data, b["duration"])
                b["status"] = "ready"
                b["audible"] = await self._audible_book(b)
                if b.get("reason") in ("files", "own"):
                    b["current"] = await self._current(item_id)
                    if same_chapters(b["current"], b["proposal"]["chapters"]):
                        b["status"] = "matches"     # its chapters already are Audible's
                    else:
                        b["proposal"]["warnings"].insert(0, f"Replaces the {len(b['current'])} chapters ABS made "
                                                         "from the book's files." if b["reason"] == "files" else
                                                         f"Replaces the book's own {len(b['current'])} chapters.")
            else:
                b["status"] = "no_match"
                b["error"] = ("Audible has no chapters for ASIN " + b["asin"] if b["asin"]
                              else "No Audible match for this title and author." if not b["candidates"]
                              else "Audible's matches have no chapter data.")
        except ABSError as e:
            b.update(status="error", error=str(e)[:300])
        except asyncio.TimeoutError:
            b.update(status="error", error="Audible took too long to answer. Try again.")
        except Exception as e:
            log.exception("chapter lookup failed for %s", b.get("title"))
            b.update(status="error", error=(str(e) or type(e).__name__)[:300])
        b["updated_at"] = time.time()
        self.save()
        return b

    async def _current(self, item_id: str) -> list[dict]:
        item = await self.abs._req("GET", f"/api/items/{item_id}", None, params={"expanded": 1})
        return [{"title": c.get("title") or "", "start": float(c.get("start") or 0), "end": float(c.get("end") or 0)}
                for c in (item.get("media") or {}).get("chapters") or []]

    async def _chapters(self, asin: str, region: str) -> dict | None:
        data = await self.abs._req("GET", "/api/search/chapters", None, timeout=AUDIBLE_TIMEOUT,
                                   params={"asin": asin, "region": region})
        return data if isinstance(data, dict) and not data.get("error") and data.get("chapters") else None

    async def _audible_book(self, b: dict) -> dict | None:
        """The Audible edition the chapters came from, for showing next to the ABS book."""
        found = next((c for c in b.get("candidates") or [] if c["asin"] == b["asin"]), None)
        if not found:
            try:      # ABS's Audible search treats a title that is an ASIN as a lookup of that edition
                found = next(iter(await self._search(b, title=b["asin"])), None)
            except ABSError as e:
                log.info("no Audible details for %s: %s", b["asin"], e)
        if not found:
            return None
        return {**found, "url": f"https://www.audible{TLD.get(b['region'], '.com')}/pd/{b['asin']}"}

    async def _search(self, b: dict, title: str | None = None) -> list[dict]:
        params = {"provider": _provider(b["region"]), "title": title, "author": "", "id": ""} if title else {
            "provider": _provider(b["region"]), "title": b["title"], "author": b["author"], "id": b["item_id"]}
        results = await self.abs._req("GET", "/api/search/books", None, timeout=AUDIBLE_TIMEOUT, params={k: v for k, v in params.items() if v})
        out = []
        for r in results if isinstance(results, list) else []:
            if not isinstance(r, dict) or not ASIN.match(str(r.get("asin") or "").upper()):
                continue
            out.append({"asin": r["asin"].upper(), "title": r.get("title") or "", "author": r.get("author") or "",
                        "narrator": r.get("narrator") or "", "duration": float(r.get("duration") or 0) * 60,
                        "cover": r.get("cover") or "",
                        "confidence": r.get("matchConfidence")})
        return out[:6]

    async def add_audio_proposal(self, item_id: str, chapters: list[dict], warnings: list[str], quality: str,
                                 note: str) -> dict:
        """Chapters found in the book's own audio (spoken "Chapter N" plus pauses, by tools/audio_chapters.py),
        for books Audible can't help with. Shown for review like Audible's; nothing is written until applied."""
        item = await self.abs._req("GET", f"/api/items/{item_id}", None, params={"expanded": 1})
        media = item.get("media") or {}
        duration = float(media.get("duration") or 0)
        cleaned = []
        for c in sorted(chapters, key=lambda c: float(c["start"])):
            start = max(0.0, float(c["start"]))
            if duration and start >= duration:
                continue
            cleaned.append({"title": str(c.get("title") or f"Chapter {len(cleaned) + 1}").strip(), "start": round(start, 3)})
        if len(cleaned) < 2:
            raise ValueError("need at least two chapters")
        cleaned[0]["start"] = 0.0
        for a, b in zip(cleaned, cleaned[1:]):
            a["end"] = b["start"]
        cleaned[-1]["end"] = round(duration, 3)
        md = media.get("metadata") or {}
        old = self.books.get(item_id)
        asin = (md.get("asin") or "").strip().upper()
        n = len(media.get("chapters") or [])
        tracks = len(media.get("tracks") or media.get("audioFiles") or [])
        b = {"item_id": item_id, "library_id": item.get("libraryId", ""), "title": md.get("title") or "",
             "author": md.get("authorName") or ", ".join(a.get("name", "") for a in md.get("authors") or []),
             "duration": duration, "num_chapters": n,
             "reason": "none" if n <= 1 else "files" if tracks > 1 and n == tracks else "own",
             "current": [{"title": c.get("title") or "", "start": float(c.get("start") or 0), "end": float(c.get("end") or 0)}
                         for c in media.get("chapters") or []],
             "abs_asin": asin if ASIN.match(asin) else "", "asin": asin if ASIN.match(asin) else "", "asin_source": "abs" if ASIN.match(asin) else "",
             "region": (old or {}).get("region") or self.region, "candidates": (old or {}).get("candidates") or [],
             "audible": None, "source": "audio", "status": "ready", "error": "", "updated_at": time.time(),
             "proposal": {"chapters": cleaned, "runtime": round(duration, 1), "duration": round(duration, 1), "difference": 0.0,
                          "branding_removed": False, "dropped": 0, "warnings": [str(w) for w in warnings][:10],
                          "quality": quality if quality in ("good", "check", "bad") else "check", "note": str(note)[:300]}}
        self.books[item_id] = b
        self.save()
        log.info("chapters found in the audio of %s: %d (%s)", b["title"], len(cleaned), b["proposal"]["quality"])
        return b

    def set_skipped(self, item_id: str, skipped: bool) -> dict:
        b = self.books[item_id]
        if skipped:
            b["status"] = "skipped"
        else:
            b["status"] = "ready" if b.get("proposal") else "no_match"
        self.save()
        return b

    async def apply(self, item_id: str) -> dict:
        """Write the approved chapters to ABS (and the ASIN, when it was found by search)."""
        b = self.books[item_id]
        if b["status"] not in ("ready", "error") or not b.get("proposal") or not b["proposal"]["chapters"]:
            raise ValueError(f"{b['title']}: nothing to apply")
        chapters = [{"id": i, "start": c["start"], "end": c["end"], "title": c["title"]}
                    for i, c in enumerate(b["proposal"]["chapters"])]
        try:
            await self.abs._req("POST", f"/api/items/{item_id}/chapters", self.edit_key, json={"chapters": chapters})
            if b["asin"] and b["asin"] != b.get("abs_asin"):
                try:
                    await self.abs._req("PATCH", f"/api/items/{item_id}/media", self.edit_key,
                                        json={"metadata": {"asin": b["asin"]}})
                except ABSError as e:
                    log.warning("chapters saved but the ASIN was not for %s: %s", b["title"], e)
        except ABSError as e:
            if e.status == 403:
                user = (self.edit_check or {}).get("user") or "the bridge's ABS user"
                b.update(status="error", error=f"ABS refused: {user} may not edit books. Put an ABS API key of a user "
                         "with the Update permission in chapters_api_key and restart the bridge.")
            else:
                b.update(status="error", error=str(e)[:300])
            self.save()
            raise
        b.update(status="applied", error="", num_chapters=len(chapters), applied_at=time.time())
        self.save()
        self.on_applied(item_id)
        log.info("chapters applied to %s: %d from ASIN %s", b["title"], len(chapters), b["asin"] or "?")
        return b

    def summary(self) -> list[dict]:
        order = {"ready": 0, "error": 1, "no_match": 2, "looking": 3, "queued": 4, "skipped": 5, "matches": 6, "applied": 7}
        return sorted(self.books.values(), key=lambda b: (order.get(b["status"], 9), b["title"].lower()))
