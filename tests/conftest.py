"""Test harness: a fake Audiobookshelf server serving real audio files, a fake Yoto REST API, and a
fake MQTT control. The bridge runs unmodified against them."""
import json
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotobridge.app import Bridge, make_app  # noqa: E402
from yotobridge.config import Config  # noqa: E402

KEY = "abs-test-key"
KEY2 = "abs-test-key-child2"    # a second ABS user, with its own progress
# A 60 s book in three files with different tones, so tests can tell where in the book audio comes from.
FILES = [("part1.m4a", 440), ("part2.mp3", 880), ("part3.mp3", 1320)]
PART = 20.0


@pytest.fixture(scope="session")
def audio_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    for name, hz in FILES:
        codec = ["-c:a", "aac"] if name.endswith("m4a") else ["-c:a", "libmp3lame"]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={PART}",
                        *codec, str(d / name)], check=True)
    return d


class FakeABS:
    def __init__(self, audio_dir: Path):
        self.audio_dir = audio_dir
        self.progress: dict[str, dict] = {}
        self.progress2: dict[str, dict] = {}     # the KEY2 user's progress
        self.calls: list[tuple] = []
        self.sessions: dict[str, dict] = {}
        self.extra: dict[str, dict] = {}         # more books (full item JSON), e.g. for old-card transfer tests
        self.down = 0                            # answer this many session requests with 503 (ABS away)
        self.down_files = 0                      # and this many audio file requests

    SERIES = {"li1": "1", "li2": "2", "li3": "10"}   # series "ser1", sequence per book

    COLLECTION = ["li3", "li1"]   # collection "col1", in its user-set order

    def collection_json(self):
        return {"id": "col1", "libraryId": "lib1", "name": "Bedtime",
                "books": [self.item(i) for i in self.COLLECTION]}

    def item(self, item_id):
        if item_id in self.extra:
            return json.loads(json.dumps(self.extra[item_id]))
        series = [{"id": "ser1", "name": "Saga", "sequence": self.SERIES[item_id]}] if item_id in self.SERIES else []
        return {"id": item_id, "libraryId": "lib1", "mediaType": "book", "media": {
            "duration": PART * 3, "metadata": {"title": f"Book {item_id}", "authorName": "Author", "series": series},
            "tracks": [{"index": i + 1, "startOffset": i * PART, "duration": PART,
                        "contentUrl": f"/api/items/{item_id}/file/{i}"} for i in range(3)],
            "chapters": [{"id": i, "start": i * PART, "end": (i + 1) * PART, "title": f"Chapter {i + 1}"}
                         for i in range(3)]}}

    def _p(self, r):
        return self.progress2 if r.headers.get("Authorization") == f"Bearer {KEY2}" else self.progress

    def app(self):
        a = web.Application()

        @web.middleware
        async def auth(request, handler):
            if not request.path.endswith("/cover") and request.headers.get("Authorization") not in (f"Bearer {KEY}", f"Bearer {KEY2}"):
                return web.Response(status=401)
            return await handler(request)
        a.middlewares.append(auth)

        async def libraries(r):
            return web.json_response({"libraries": [{"id": "lib1", "name": "Kids", "mediaType": "book"}]})

        async def items(r):
            f = r.query.get("filter", "")
            if f.startswith("series."):
                import base64
                sid = base64.b64decode(f[7:]).decode()
                ids = [i for i in ("li3", "li1", "li2") if sid == "ser1"]     # deliberately out of order
                ids += [i for i, it in self.extra.items()
                        if any(x["id"] == sid for x in it["media"]["metadata"].get("series") or [])]
                # like ABS 2.37: with a series filter, metadata.series is one object, not a list
                results = [self.item(i) for i in ids]
                for r in results:
                    r["media"]["metadata"]["series"] = next(x for x in r["media"]["metadata"]["series"] if x["id"] == sid)
                return web.json_response({"results": results, "total": len(ids)})
            self.calls.append(("items", r.query.get("sort"), r.query.get("desc")))
            return web.json_response({"results": [self.item("li1"), self.item("li2")], "total": 2})

        async def collections(r):
            self.calls.append(("collections",))
            return web.json_response({"collections": [self.collection_json()]})

        async def collection(r):
            if r.match_info["cid"] != "col1":
                return web.Response(status=404)
            return web.json_response(self.collection_json())

        async def me(r):
            return web.json_response({"mediaProgress": [{"libraryItemId": k, **v} for k, v in self._p(r).items()]})

        async def item(r):
            return web.json_response(self.item(r.match_info["id"]))

        async def play(r):
            if self.down > 0:
                self.down -= 1
                return web.Response(status=503, text="restarting")
            iid = r.match_info["id"]
            body = await r.json()
            sid = f"ps_{len(self.sessions) + 1}"
            p = self._p(r).get(iid, {})
            self.sessions[sid] = {"item": iid, "open": True, "progress": self._p(r)}
            self.calls.append(("play", iid, body["deviceInfo"]["deviceId"]))
            return web.json_response({
                "id": sid, "libraryItemId": iid, "duration": PART * 3,
                "startTime": p.get("currentTime", 0), "currentTime": p.get("currentTime", 0),
                "audioTracks": [{"index": i + 1, "startOffset": i * PART, "duration": PART,
                                 "contentUrl": f"/api/items/{iid}/file/{i}"} for i in range(3)]})

        async def sync(r):
            sid = r.match_info["sid"]
            body = await r.json()
            if sid not in self.sessions:
                return web.Response(status=404)
            self.calls.append((r.match_info["op"], sid, body["currentTime"], body["timeListened"]))
            self.sessions[sid]["progress"][self.sessions[sid]["item"]] = {"currentTime": body["currentTime"], "duration": PART * 3,
                                                         "lastUpdate": int(time.time() * 1000)}
            if r.match_info["op"] == "close":
                self.sessions.pop(sid)
            return web.json_response({})

        async def progress(r):
            p = self._p(r).get(r.match_info["id"])
            return web.json_response(p) if p else web.Response(status=404)

        async def patch_progress(r):
            body = await r.json()
            self.calls.append(("patch", r.match_info["id"], body["currentTime"]) + (("finished",) if body.get("isFinished") else ()))
            self._p(r)[r.match_info["id"]] = {"currentTime": body["currentTime"], "duration": body["duration"],
                                                 "isFinished": bool(body.get("isFinished")),
                                                 "lastUpdate": int(time.time() * 1000)}
            return web.json_response({})

        async def in_progress(r):
            return web.json_response({"libraryItems": [self.item(i) for i in self._p(r)]})

        async def search(r):
            q = r.query["q"].lower()
            hits = [self.item(i) for i in [*self.extra, "li1", "li2", "li3"]
                    if q in self.item(i)["media"]["metadata"]["title"].lower()]
            self.calls.append(("search", q))
            return web.json_response({"book": [{"libraryItem": h} for h in hits], "series": []})

        async def file(r):
            if self.down_files > 0:
                self.down_files -= 1
                return web.Response(status=503, text="restarting")
            return web.FileResponse(self.audio_dir / FILES[int(r.match_info["n"])][0])

        async def cover(r):
            return web.Response(body=b"\xff\xd8fakejpeg", content_type="image/jpeg")

        a.router.add_get("/api/libraries", libraries)
        a.router.add_get("/api/libraries/{lib}/items", items)
        a.router.add_get("/api/me", me)
        a.router.add_get("/api/libraries/{lib}/search", search)
        a.router.add_get("/api/collections", collections)
        a.router.add_get("/api/collections/{cid}", collection)
        a.router.add_get("/api/items/{id}", item)
        a.router.add_post("/api/items/{id}/play", play)
        a.router.add_post("/api/session/{sid}/{op}", sync)
        a.router.add_get("/api/me/progress/{id}", progress)
        a.router.add_patch("/api/me/progress/{id}", patch_progress)
        a.router.add_get("/api/me/items-in-progress", in_progress)
        a.router.add_get("/api/items/{id}/file/{n}", file)
        a.router.add_get("/api/items/{id}/cover", cover)
        return a


class FakeYotoAPI:
    def __init__(self):
        self.content: dict[str, dict] = {"existing1": {"cardId": "existing1", "title": "Old MYO card"}}
        self.posts: list[dict] = []
        self.token_requests: list[dict] = []
        self.uploads: dict[str, bytes] = {}      # uploadId -> MP3 bytes the bridge PUT
        self.polls: dict[str, int] = {}
        self.fail_uploads = False
        self.stuck: set[str] = set()            # upload ids Yoto never finishes processing
        self.icons: list[bytes] = []
        self.icon_status = 200                  # 403 = the app lacks the user:icons:manage scope
        self.base = ""                           # set once the server is up, for signed upload URLs
        self.library = [                         # Yoto's public icons: (mediaId, title, tags, colour)
            ("m-dragon", "Dragon", ["fantasy", "animal"], (200, 40, 40)),
            ("m-sword", "Sword", ["knight", "dragon"], (180, 180, 190)),
            ("m-castle", "Castle", ["knight", "princess"], (120, 120, 200)),
            ("m-redcastle", "Big Red Castle", ["building"], (220, 60, 60)),
        ]
        self.library_requests = 0

    def app(self):
        a = web.Application()

        async def devices(r):
            return web.json_response({"devices": [{"deviceId": "dev-mini", "name": "Mini", "online": True},
                                                  {"deviceId": "dev-player", "name": "Player", "online": True}]})

        async def post_content(r):
            body = await r.json()
            self.posts.append(body)
            cid = body.get("cardId") or f"card{len(self.content) + 1}"
            self.content[cid] = {**body, "cardId": cid}
            return web.json_response({"card": {"cardId": cid}})

        async def one(r):
            c = self.content.get(r.match_info["cid"])
            return web.json_response({"card": c}) if c else web.Response(status=404)

        async def mine(r):
            return web.json_response({"cards": list(self.content.values())})

        async def cover(r):
            return web.json_response({"coverImage": {"mediaUrl": "https://cdn.example/cover.jpg"}})

        async def token(r):
            form = dict(await r.post())
            self.token_requests.append(form)
            if form.get("grant_type") != "authorization_code":
                return web.json_response({"error": "unsupported_grant_type"}, status=403)
            if form.get("code") != "good-code":
                return web.json_response({"error": "invalid_grant", "error_description": "Invalid authorization code"},
                                         status=403)
            return web.json_response({"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 86400})

        a.router.add_post("/oauth/token", token)

        async def upload_url(r):
            uid = f"up{len(self.uploads) + 1}"
            self.uploads[uid] = b""
            return web.json_response({"upload": {"uploadUrl": f"{self.base}/signed/{uid}", "uploadId": uid}})

        async def signed_put(r):
            if self.fail_uploads:
                return web.Response(status=500, text="storage down")
            assert "Authorization" not in r.headers   # signed URL: no Yoto token
            self.uploads[r.match_info["uid"]] = await r.read()
            return web.Response()

        async def transcoded(r):
            uid = r.match_info["uid"]
            self.polls[uid] = self.polls.get(uid, 0) + 1
            if self.polls[uid] < 2 or uid in self.stuck:   # first poll: still transcoding
                return web.json_response({"transcode": {}})
            data = self.uploads[uid]
            return web.json_response({"transcode": {"transcodedSha256": f"sha-{uid}", "transcodedInfo": {
                "duration": round(len(data) / 8000), "fileSize": len(data), "channels": "mono", "format": "aac"}}})

        async def upload_icon(r):
            if self.icon_status != 200:
                return web.json_response({"error": "insufficient scope"}, status=self.icon_status)
            self.icons.append(await r.read())
            return web.json_response({"displayIcon": {"mediaId": f"icon{len(self.icons)}", "new": True}})

        async def public_icons(r):
            self.library_requests += 1
            return web.json_response({"displayIcons": [
                {"mediaId": m, "userId": "yoto", "title": t, "publicTags": tags, "url": f"{self.base}/iconfile/{m}.png",
                 "public": True, "new": True, "displayIconId": f"d-{m}"} for m, t, tags, _ in self.library]})

        async def icon_file(r):
            from yotobridge.icons import png
            colour = next(c for m, _, _, c in self.library if m == r.match_info["m"])
            return web.Response(body=png(bytes([*colour, 255]) * 256), content_type="image/png")

        a.router.add_post("/media/displayIcons/user/me/upload", upload_icon)
        a.router.add_get("/media/displayIcons/user/yoto", public_icons)
        a.router.add_get("/iconfile/{m}.png", icon_file)
        a.router.add_get("/media/transcode/audio/uploadUrl", upload_url)
        a.router.add_put("/signed/{uid}", signed_put)
        a.router.add_get("/media/upload/{uid}/transcoded", transcoded)
        a.router.add_get("/device-v2/devices/mine", devices)
        a.router.add_post("/content", post_content)
        a.router.add_get("/content/mine", mine)
        a.router.add_get("/content/{cid}", one)
        a.router.add_post("/media/coverImage/user/me/upload", cover)
        return a


class FakeControl:
    def __init__(self):
        self.connected = True
        self.commands: list[tuple] = []
        self.tracks: list[int] = []

    async def connect(self, ids):
        self.commands.append(("connect", tuple(ids)))

    async def play(self, device_id, card_id, seconds_in=None, track=1):
        self.commands.append(("play", device_id, card_id, seconds_in))
        self.tracks.append(track)

    async def pause(self, d):
        self.commands.append(("pause", d))

    async def resume(self, d):
        self.commands.append(("resume", d))

    async def stop(self, d):
        self.commands.append(("stop", d))


@pytest_asyncio.fixture
async def env(audio_dir, tmp_path):
    fabs, fyoto = FakeABS(audio_dir), FakeYotoAPI()
    abs_srv, yoto_srv = TestServer(fabs.app()), TestServer(fyoto.app())
    await abs_srv.start_server()
    await yoto_srv.start_server()
    fyoto.base = str(yoto_srv.make_url("")).rstrip("/")
    (tmp_path / "yoto_tokens.json").write_text(json.dumps(
        {"access_token": "t", "refresh_token": "r", "expires_in": 86400, "obtained_at": time.time()}))
    created = []

    async def make(**overrides):
        cfg = Config(abs_url=str(abs_srv.make_url("")), abs_api_key=KEY, public_url="http://bridge.test:8080",
                     data_dir=str(tmp_path), yoto_client_id="cid", yoto_api=str(yoto_srv.make_url("")),
                     yoto_login=str(yoto_srv.make_url("")).rstrip("/"),
                     sync_interval=15, **{"chapter_tracks": False, **overrides})
        http = aiohttp.ClientSession()
        control = FakeControl()
        br = Bridge(cfg, http, control_factory=lambda: control)
        br.yoto.TRANSCODE_POLL = 0.01
        client = TestClient(TestServer(make_app(br)))
        await client.start_server()
        created.append((client, http))
        return br, client, control

    yield type("Env", (), {"abs": fabs, "yoto": fyoto, "make": staticmethod(make)})
    for client, http in created:
        await client.close()
        await http.close()
    await abs_srv.close()
    await yoto_srv.close()
