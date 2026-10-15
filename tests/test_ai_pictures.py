"""AI chapter pictures against a fake OpenAI-format server (and Ollama's own image call)."""
import asyncio
import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from yotobridge import icons
from yotobridge.ai_pictures import AISettings

from test_bridge import decode_png


class FakeAI:
    def __init__(self):
        self.calls: list[str] = []
        self.audio: list[int] = []
        self.down = 0                       # answer 503 this many times first (a computer asleep)
        self.subject = "a friendly dragon"

    def app(self):
        a = web.Application()

        async def transcribe(r):
            self.calls.append("transcribe")
            if self.down:
                self.down -= 1
                return web.Response(status=503)
            form = await r.post()
            self.audio.append(len(form["file"].file.read()))
            return web.json_response({"text": "Once upon a time a dragon flew over the castle."})

        async def chat(r):
            body = await r.json()
            prompt = body["messages"][0]["content"]
            self.calls.append("chat")
            if prompt.startswith("Chapter title") and "List 6" in prompt:
                answer = "dragon, castle, sword"
            elif "Which of these" in prompt:
                answer = "2"
            else:
                answer = self.subject
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": answer}}]})

        def picture():
            return base64.b64encode(icons.png(bytes([10, 200, 30, 255]) * 256)).decode()

        async def images(r):
            body = await r.json()
            self.calls.append("image:" + body["prompt"])
            return web.json_response({"data": [{"b64_json": picture()}]})

        async def ollama(r):
            body = await r.json()
            self.calls.append(("unload:" if body.get("keep_alive") == 0 else "ollama-image:") + body["model"])
            return web.json_response({"model": body["model"], "image": picture(), "done": True})

        a.router.add_post("/v1/audio/transcriptions", transcribe)
        a.router.add_post("/v1/chat/completions", chat)
        a.router.add_post("/v1/images/generations", images)
        a.router.add_post("/api/generate", ollama)
        return a


@pytest.fixture
async def ai():
    fake = FakeAI()
    srv = TestServer(fake.app())
    await srv.start_server()
    fake.url = str(srv.make_url("/v1"))
    yield fake
    await srv.close()


def settings(ai, image=None):
    raw = {"transcribe": {"url": ai.url, "model": "whisper-1"}, "text": {"url": ai.url, "model": "small", "unload": True},
           "sample_seconds": 5}
    if image:
        raw["image"] = image
    return raw


async def _wait(client, slot):
    for _ in range(200):
        cards = await (await client.get("/api/cards")).json()
        job = next(c for c in cards if c["slot"] == slot)["ai_job"]
        if job and job["status"] != "running":
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("AI pictures did not finish")


async def test_library_mode_picks_a_yoto_icon_per_chapter(env, ai):
    br, client, _ = await env.make(chapter_tracks=True, ai_pictures=settings(ai))
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    st = await (await client.get("/api/status")).json()
    assert st["icons"]["ai"] == {"enabled": True, "mode": "library"}
    r = await client.post(f"/api/cards/{s}/icon/ai", json={})
    assert r.status == 200
    job = await _wait(client, s)
    assert job["status"] == "done" and job["done"] == 3 and job["skipped"] == 0
    slot = br.store.get(s)
    assert slot.icon_style == "custom" and len(slot.icon_chapters) == 3
    # keywords dragon, castle, sword -> candidates Dragon, Sword(tag dragon)... kid-safe drops the sword;
    # the model answered "2" -> the second candidate
    px = br.custom_icon_path(slot, "li1:1").read_bytes()
    assert tuple(px[:3]) in {(200, 40, 40), (120, 120, 200), (220, 60, 60)}
    assert tuple(px[:3]) != (180, 180, 190)                       # never the sword
    assert all(a > 1000 for a in ai.audio)                        # the start of each chapter was sent
    assert "unload:small" in ai.calls                             # the text model was dropped after use
    cid = slot.yoto_card_id
    url = env.yoto.content[cid]["content"]["chapters"][0]["tracks"][0]["display"]["iconUrl16x16"]
    got = decode_png(await (await client.get(url[len("http://bridge.test:8080"):])).read())[2]
    assert got[:3] == px[:3]                                      # the card shows the picture


async def test_image_mode_draws_pictures_openai_and_ollama(env, ai):
    br, client, _ = await env.make(chapter_tracks=True, ai_pictures=settings(ai, {"url": ai.url, "model": "img"}))
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    await client.post(f"/api/cards/{s}/icon/ai", json={})
    job = await _wait(client, s)
    assert job["status"] == "done" and job["mode"] == "images"
    px = br.custom_icon_path(br.store.get(s), "li1:2").read_bytes()
    assert tuple(px[:3]) == (10, 200, 30)
    assert any(c.startswith("image:a friendly dragon, simple flat pixel art") for c in ai.calls)

    br2, client2, _ = await env.make(chapter_tracks=True,
                                     ai_pictures=settings(ai, {"url": ai.url, "model": "klein", "api": "ollama"}))
    s2 = (await (await client2.post("/api/cards", json={"item_id": "li2"})).json())["slot"]
    await client2.post(f"/api/cards/{s2}/icon/ai", json={})
    assert (await _wait(client2, s2))["status"] == "done"
    assert "ollama-image:klein" in ai.calls


async def test_unsafe_subject_is_skipped(env, ai):
    ai.subject = "a sword dripping blood"
    br, client, _ = await env.make(chapter_tracks=True, ai_pictures=settings(ai, {"url": ai.url, "model": "img"}))
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    await client.post(f"/api/cards/{s}/icon/ai", json={})
    job = await _wait(client, s)
    assert job["skipped"] == 3 and not br.store.get(s).icon_chapters
    assert not any(c.startswith("image:") for c in ai.calls)


async def test_waits_for_a_sleeping_computer_and_keeps_existing_pictures(env, ai):
    br, client, _ = await env.make(chapter_tracks=True, ai_pictures=settings(ai))
    br.ai.RETRY_SLEEP = 0.01
    ai.down = 2
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    br.store.get(s).icon_chapters["li1:1"] = "handmade"
    await client.post(f"/api/cards/{s}/icon/ai", json={})
    job = await _wait(client, s)
    assert job["status"] == "done" and job["total"] == 2              # chapter 1 already had a picture
    assert br.store.get(s).icon_chapters["li1:1"] == "handmade"
    assert ai.calls[:3] == ["transcribe", "transcribe", "transcribe"]


async def test_off_without_settings(env):
    br, client, _ = await env.make(chapter_tracks=True)
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    r = await client.post(f"/api/cards/{s}/icon/ai", json={})
    assert r.status == 400


def test_settings_reject_typos():
    with pytest.raises(SystemExit):
        AISettings.parse({"transcibe": {"url": "x"}})
    with pytest.raises(SystemExit):
        AISettings.parse({"image": {"url": "x", "api": "comfy"}})
