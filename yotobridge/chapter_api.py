"""JSON API behind the phone page's Chapters tab (see chapter_finder.py). Reads are open like the rest
of the page; anything that starts lookups or writes to ABS asks for the PIN when one is set."""
from __future__ import annotations

from aiohttp import web

from .abs_client import ABSError
from .chapter_finder import REGIONS, ChapterFinder

FINDER = web.AppKey("chapter_finder", ChapterFinder)


def register(app: web.Application, br, require_pin):
    def forget_cached(item_id: str):
        # the bridge caches chapters per book; the next card layout must see the new ones
        br._chapters.pop(item_id, None)
        br._durations.pop(item_id, None)

    app[FINDER] = ChapterFinder(br.abs, br.cfg, on_applied=forget_cached)

    async def resume(app):
        app[FINDER].resume()             # lookups a deploy restart cut off carry on by themselves
    app.on_startup.append(resume)

    def finder(request) -> ChapterFinder:
        return request.app[FINDER]

    def book(request) -> str:
        item_id = request.match_info["item"]
        if item_id not in finder(request).books:
            raise web.HTTPNotFound(text="That book is not in the chapter review; scan again.")
        return item_id

    async def overview(request):
        f = finder(request)
        return web.json_response({"scan": f.scan, "books": f.summary(), "can_edit": await f.can_edit(),
                                  "region": f.region, "regions": list(REGIONS)})

    async def scan(request):
        require_pin(request)
        body = await request.json() if request.can_read_body else {}
        libs = [l["id"] for l in await br.abs.libraries()]
        if br.cfg.library_ids:
            libs = [l for l in libs if l in br.cfg.library_ids]
        if body.get("library_id"):
            libs = [l for l in libs if l == body["library_id"]]
        if not libs:
            raise web.HTTPBadRequest(text="No book library to scan.")
        if not finder(request).start_scan(libs, rescan=bool(body.get("rescan")), every=bool(body.get("every"))):
            raise web.HTTPConflict(text="A scan is already running.")
        return web.json_response({"ok": True})

    async def lookup(request):
        require_pin(request)
        item_id = book(request)
        body = await request.json() if request.can_read_body else {}
        asin = str(body.get("asin") or "").strip().upper()
        if asin and (len(asin) != 10 or not asin.isalnum()):
            raise web.HTTPBadRequest(text="An ASIN is 10 letters and digits, like B002V5D1CG.")
        return web.json_response(await finder(request).lookup(item_id, asin or None, body.get("region")))

    async def skip(request):
        require_pin(request)
        item_id = book(request)
        body = await request.json() if request.can_read_body else {}
        return web.json_response(finder(request).set_skipped(item_id, body.get("skipped", True)))

    async def audio_proposal(request):
        """From tools/audio_chapters.py: {"chapters": [{"title", "start"}], "warnings", "quality", "note"}."""
        require_pin(request)
        body = await request.json()
        try:
            b = await finder(request).add_audio_proposal(request.match_info["item"], body.get("chapters") or [],
                                                          body.get("warnings") or [], body.get("quality", "check"),
                                                          body.get("note", ""))
        except (KeyError, TypeError, ValueError) as e:
            raise web.HTTPBadRequest(text=f"bad chapter list: {e}")
        return web.json_response(b)

    async def apply(request):
        """Apply the listed books one after another; report each, so one failure doesn't hide the rest."""
        require_pin(request)
        body = await request.json()
        f = finder(request)
        results = []
        for item_id in body.get("item_ids") or []:
            if item_id not in f.books:
                results.append({"item_id": item_id, "ok": False, "error": "not in the review"})
                continue
            try:
                await f.apply(item_id)
                results.append({"item_id": item_id, "ok": True})
            except (ABSError, ValueError) as e:
                results.append({"item_id": item_id, "ok": False, "error": f.books[item_id].get("error") or str(e)})
        return web.json_response({"results": results, "applied": sum(r["ok"] for r in results)})

    r = app.router
    r.add_get("/api/chapters", overview)
    r.add_post("/api/chapters/scan", scan)
    r.add_post("/api/chapters/apply", apply)
    r.add_post("/api/chapters/{item}/lookup", lookup)
    r.add_post("/api/chapters/{item}/skip", skip)
    r.add_post("/api/chapters/{item}/proposal", audio_proposal)
