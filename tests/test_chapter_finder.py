"""Chapters tab: scan ABS for books without chapters, propose Audible chapters, apply only approved ones."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotobridge import chapter_api  # noqa: E402
from yotobridge.abs_client import ABSClient  # noqa: E402
from yotobridge.chapter_finder import build_proposal, chapter_reason, needs_chapters  # noqa: E402
from yotobridge.config import Config  # noqa: E402

KEY, EDIT_KEY = "viewer-key", "editor-key"
HOUR = 3600.0


def audnexus(lengths_s, intro_ms=0, outro_ms=0, accurate=True):
    """Audnexus-shaped chapter data; chapter times include the brand intro like Audible's do."""
    chapters, at = [], 0.0
    for i, L in enumerate(lengths_s):
        ms = L * 1000 + (intro_ms if i == 0 else 0) + (outro_ms if i == len(lengths_s) - 1 else 0)
        chapters.append({"title": f"Chapter {i + 1}", "startOffsetMs": at, "startOffsetSec": int(at / 1000), "lengthMs": ms})
        at += ms
    return {"asin": "B000000001", "chapters": chapters, "runtimeLengthMs": at, "runtimeLengthSec": int(at / 1000),
            "brandIntroDurationMs": intro_ms, "brandOutroDurationMs": outro_ms, "isAccurate": accurate}


# ---------------------------------------------------------------- proposal maths

def test_exact_match_is_good_and_contiguous():
    p = build_proposal(audnexus([600, 900, 300]), 1800.0)
    assert p["quality"] == "good" and not p["warnings"]
    assert [(c["start"], c["end"]) for c in p["chapters"]] == [(0, 600), (600, 1500), (1500, 1800)]


def test_branding_removed_when_audio_has_none():
    """Audio without "This is Audible": the intro is cut so chapter 2 starts at 600 s, not 602 s."""
    p = build_proposal(audnexus([600, 900, 300], intro_ms=2000, outro_ms=3000), 1800.0)
    assert p["branding_removed"] and p["quality"] == "good"
    assert p["chapters"][1]["start"] == 600


def test_branding_kept_when_audio_has_it():
    p = build_proposal(audnexus([600, 900, 300], intro_ms=2000, outro_ms=3000), 1805.0)
    assert not p["branding_removed"] and p["chapters"][1]["start"] == 602


def test_other_edition_flagged_and_late_chapters_dropped():
    """Audio 20 min shorter than Audible's: chapters past the end are left out and the book is marked bad."""
    p = build_proposal(audnexus([600, 600, 600, 600]), 1200.0)
    assert p["quality"] == "bad" and p["dropped"] == 2
    assert p["chapters"][-1]["end"] == 1200
    assert any("shorter" in w for w in p["warnings"]) and any("left out" in w for w in p["warnings"])


def test_small_difference_needs_a_check():
    p = build_proposal(audnexus([600, 600]), 1260.0)
    assert p["quality"] == "check" and "1m 0s longer" in p["warnings"][0]
    assert p["chapters"][-1]["end"] == 1260            # the extra minute joins the last chapter


def test_needs_chapters():
    assert needs_chapters({"media": {"numTracks": 1, "numChapters": 0, "duration": 10}})
    assert needs_chapters({"media": {"numTracks": 1, "numChapters": 1, "duration": 10}})
    assert chapter_reason({"media": {"numTracks": 3, "numChapters": 3, "duration": 10}}) == "files"
    assert not needs_chapters({"media": {"numTracks": 3, "numChapters": 12, "duration": 10}})   # its own chapters
    assert not needs_chapters({"media": {"numTracks": 1, "numChapters": 12, "duration": 10}})
    assert not needs_chapters({"media": {"numTracks": 0, "numChapters": 0, "duration": 0}})   # ebook only


# ---------------------------------------------------------------- against a fake ABS

class FakeABS:
    """Books: b1 no ASIN (found by search), b2 ASIN in ABS, b3 nothing on Audible, b4 has chapters already."""
    BOOKS = {
        "b1": {"title": "Lantern Hill", "asin": "", "duration": 1800.0, "chapters": 0},
        "b2": {"title": "Percy Jackson", "asin": "B00000PJ01", "duration": 3000.0, "chapters": 1},
        "b3": {"title": "Home Recording", "asin": "", "duration": 900.0, "chapters": 0},
        "b4": {"title": "Already Done", "asin": "", "duration": 900.0, "chapters": 12},
        # chapters made from files: b5's two files are not its chapters, b6's three files already are
        "b5": {"title": "Split Files", "asin": "B00000SF01", "duration": 1800.0, "chapters": 2, "files": 2},
        "b6": {"title": "Good Files", "asin": "B00000MT01", "duration": 1800.0, "chapters": 3, "files": 3},
    }
    CHAPTERS = {"B00000MT01": audnexus([600, 900, 300]), "B00000PJ01": audnexus([1000, 1000, 1000]),
                "B00000SF01": audnexus([300] * 6)}
    FILE_CHAPTERS = {"b5": [(0, 900), (900, 1800)], "b6": [(0, 601), (601, 1499), (1499, 1800)]}

    def __init__(self):
        self.writes: list[tuple] = []

    def app(self):
        a = web.Application()

        async def items(r):
            res = [{"id": k, "libraryId": "lib1", "media": {"numTracks": v.get("files", 1), "numChapters": v["chapters"],
                    "duration": v["duration"], "metadata": {"title": v["title"], "authorName": "A", "asin": v["asin"]}}}
                   for k, v in self.BOOKS.items()]
            return web.json_response({"results": res, "total": len(res)})

        async def libraries(r):
            return web.json_response({"libraries": [{"id": "lib1", "name": "Kids", "mediaType": "book"}]})

        async def search_books(r):
            assert r.query["provider"] == "audible"
            if r.query["title"] == "Lantern Hill":
                return web.json_response([{"asin": "B00000MT01", "title": "Lantern Hill", "author": "A", "duration": 30,
                                           "cover": "https://m.media-amazon.com/images/I/mth.jpg"}])
            if r.query["title"] in self.CHAPTERS:          # ABS treats an ASIN title as a lookup of that edition
                return web.json_response([{"asin": r.query["title"], "title": "Audible " + r.query["title"],
                                           "author": "A", "narrator": "N", "duration": 50, "cover": "https://x/c.jpg"}])
            return web.json_response([])

        async def search_chapters(r):
            data = self.CHAPTERS.get(r.query["asin"])
            return web.json_response(data or {"error": "Chapters not found", "stringKey": "MessageChaptersNotFound"})

        async def item(r):
            spans = self.FILE_CHAPTERS.get(r.match_info["id"], [])
            v = self.BOOKS[r.match_info["id"]]
            return web.json_response({"id": r.match_info["id"], "libraryId": "lib1", "media": {
                "duration": v["duration"], "metadata": {"title": v["title"], "authorName": "A", "asin": v["asin"]},
                "tracks": [{}] * v.get("files", 1), "chapters": [
                {"id": i, "start": a, "end": b, "title": f"Track {i + 1}.mp3"} for i, (a, b) in enumerate(spans)]}})

        async def me(r):
            editor = r.headers["Authorization"] == f"Bearer {EDIT_KEY}"
            return web.json_response({"username": "editor" if editor else "viewer", "type": "user",
                                      "permissions": {"update": editor}})

        async def set_chapters(r):
            if r.headers["Authorization"] != f"Bearer {EDIT_KEY}":
                return web.Response(status=403)
            self.writes.append(("chapters", r.match_info["id"], (await r.json())["chapters"]))
            return web.json_response({"success": True, "updated": True})

        async def patch_media(r):
            if r.headers["Authorization"] != f"Bearer {EDIT_KEY}":
                return web.Response(status=403)
            self.writes.append(("media", r.match_info["id"], await r.json()))
            return web.json_response({"updated": True})

        a.router.add_get("/api/libraries", libraries)
        a.router.add_get("/api/libraries/{lib}/items", items)
        a.router.add_get("/api/search/books", search_books)
        a.router.add_get("/api/search/chapters", search_chapters)
        a.router.add_get("/api/me", me)
        a.router.add_get("/api/items/{id}", item)
        a.router.add_post("/api/items/{id}/chapters", set_chapters)
        a.router.add_patch("/api/items/{id}/media", patch_media)
        return a


@pytest.fixture
async def chapters_env(tmp_path):
    fabs = FakeABS()
    srv = TestServer(fabs.app())
    await srv.start_server()
    http = aiohttp.ClientSession()
    clients = []

    async def make(**cfg_over):
        cfg = Config(abs_url=str(srv.make_url("")).rstrip("/"), abs_api_key=KEY, data_dir=str(tmp_path), **cfg_over)
        br = SimpleNamespace(cfg=cfg, abs=ABSClient(http, cfg.abs_url, KEY), _chapters={"b1": ["old"]}, _durations={})
        app = web.Application()
        chapter_api.register(app, br, lambda request: None)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return br, client, app[chapter_api.FINDER]

    yield SimpleNamespace(abs=fabs, make=make, data=tmp_path)
    for c in clients:
        await c.close()
    await http.close()
    await srv.close()


async def scan(client, finder, **body):
    r = await client.post("/api/chapters/scan", json=body)
    assert r.status == 200, await r.text()
    await finder.task
    return await (await client.get("/api/chapters")).json()


async def test_scan_proposes_without_writing(chapters_env):
    br, client, finder = await chapters_env.make(chapters_api_key=EDIT_KEY)
    data = await scan(client, finder)
    books = {b["item_id"]: b for b in data["books"]}
    assert set(books) == {"b1", "b2", "b3", "b5", "b6"}          # b4 already has chapters
    assert books["b1"]["status"] == "ready" and books["b1"]["asin"] == "B00000MT01" and books["b1"]["asin_source"] == "search"
    assert books["b2"]["status"] == "ready" and books["b2"]["asin_source"] == "abs"
    assert books["b1"]["audible"]["cover"].endswith("mth.jpg")    # from the search that found it
    assert books["b2"]["audible"] == {"asin": "B00000PJ01", "title": "Audible B00000PJ01", "author": "A", "narrator": "N",
                                      "duration": 3000.0, "confidence": None, "cover": "https://x/c.jpg",
                                      "url": "https://www.audible.com/pd/B00000PJ01"}
    assert len(books["b2"]["proposal"]["chapters"]) == 3
    assert books["b3"]["status"] == "no_match"
    assert books["b5"]["status"] == "ready" and books["b5"]["reason"] == "files" and len(books["b5"]["current"]) == 2
    assert "Replaces the 2 chapters" in books["b5"]["proposal"]["warnings"][0]
    assert books["b6"]["status"] == "matches"                     # its files already are Audible's chapters
    assert data["scan"]["done"] == 5 and data["can_edit"]["ok"]
    assert chapters_env.abs.writes == []                          # nothing touches ABS before approval


async def test_apply_writes_only_approved_books(chapters_env):
    br, client, finder = await chapters_env.make(chapters_api_key=EDIT_KEY)
    await scan(client, finder)
    r = await client.post("/api/chapters/apply", json={"item_ids": ["b1"]})
    assert (await r.json())["applied"] == 1
    kind, item, chapters = chapters_env.abs.writes[0]
    assert (kind, item) == ("chapters", "b1")
    assert chapters == [{"id": 0, "start": 0, "end": 600, "title": "Chapter 1"},
                        {"id": 1, "start": 600, "end": 1500, "title": "Chapter 2"},
                        {"id": 2, "start": 1500, "end": 1800, "title": "Chapter 3"}]
    assert chapters_env.abs.writes[1] == ("media", "b1", {"metadata": {"asin": "B00000MT01"}})   # remembered ASIN
    assert {w[1] for w in chapters_env.abs.writes} == {"b1"}
    assert "b1" not in br._chapters                                # card layouts re-read the new chapters
    assert finder.books["b1"]["status"] == "applied"


async def test_viewer_key_cannot_apply_and_says_why(chapters_env):
    br, client, finder = await chapters_env.make()
    data = await scan(client, finder)
    assert data["can_edit"] == {"ok": False, "user": "viewer"}
    res = (await (await client.post("/api/chapters/apply", json={"item_ids": ["b2"]})).json())["results"][0]
    assert not res["ok"] and "chapters_api_key" in res["error"]
    assert chapters_env.abs.writes == []


async def test_manual_asin_skip_and_state_survives_restart(chapters_env):
    br, client, finder = await chapters_env.make(chapters_api_key=EDIT_KEY)
    await scan(client, finder)
    r = await client.post("/api/chapters/b3/lookup", json={"asin": "b00000pj01"})
    b3 = await r.json()
    assert b3["status"] == "ready" and b3["asin"] == "B00000PJ01" and b3["asin_source"] == "manual"
    assert b3["proposal"]["quality"] == "bad"                     # 15 min of audio vs a 50 min edition
    assert (await client.post("/api/chapters/b3/lookup", json={"asin": "nope"})).status == 400
    await client.post("/api/chapters/b2/skip", json={"skipped": True})

    saved = json.loads((chapters_env.data / "chapters.json").read_text())
    assert saved["books"]["b2"]["status"] == "skipped"
    _, client2, finder2 = await chapters_env.make(chapters_api_key=EDIT_KEY)
    data = await scan(client2, finder2)                           # a new scan keeps decisions already made
    books = {b["item_id"]: b for b in data["books"]}
    assert books["b2"]["status"] == "skipped" and books["b3"]["asin_source"] == "manual"
    assert data["scan"]["total"] == 0


async def test_check_every_book(chapters_env):
    """The full check also looks at books with chapters of their own; a quick scan later keeps them."""
    br, client, finder = await chapters_env.make(chapters_api_key=EDIT_KEY)
    books = {b["item_id"]: b for b in (await scan(client, finder, every=True))["books"]}
    assert books["b4"]["reason"] == "own" and books["b4"]["status"] == "no_match"
    assert books["b5"]["reason"] == "files"                       # quick-scan books are still told apart
    books = {b["item_id"]: b for b in (await scan(client, finder))["books"]}
    assert "b4" in books
    assert chapters_env.abs.writes == []


async def test_lookups_cut_off_by_a_restart_resume(chapters_env):
    """A deploy restarts the bridge mid-scan: queued books are looked up when it starts again."""
    br, client, finder = await chapters_env.make(chapters_api_key=EDIT_KEY)
    await scan(client, finder)
    finder.books["b1"].update(status="queued", proposal=None)
    finder.books["b2"].update(status="looking", proposal=None)     # was being looked up at the restart
    finder.save()
    _, client2, finder2 = await chapters_env.make(chapters_api_key=EDIT_KEY)
    assert finder2.task is not None
    await finder2.task
    assert finder2.books["b1"]["status"] == "ready" and finder2.books["b2"]["status"] == "ready"
    assert not finder2.scan["running"] and finder2.scan["done"] == 2
