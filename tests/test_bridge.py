import math
import array
import asyncio
import subprocess
import time

import pytest

from yotobridge.store import Slot

BYTERATE = 8000  # 64 kbps


def tone_at(mp3: bytes, at: float = 0.2, length: float = 0.5) -> float:
    """Dominant frequency (zero-crossing estimate) of decoded audio at `at` seconds into mp3."""
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "44100",
                          "pipe:1"], input=mp3, capture_output=True, check=True).stdout
    s = array.array("h", pcm)[int(at * 44100): int((at + length) * 44100)]
    crossings = sum(1 for a, b in zip(s, s[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / length


def duration_of(mp3: bytes) -> float:
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "8000",
                          "pipe:1"], input=mp3, capture_output=True, check=True).stdout
    return len(pcm) / 2 / 8000


def card(br, item="li1", yoto_card_id="yc1"):
    s = br.store.new_slot("card", item_id=item, title="Book", yoto_card_id=yoto_card_id)
    return s


async def test_stream_starts_at_abs_position(env):
    """Card tapped: audio starts where ABS says (minus the 5 s rewind), from the right file."""
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    assert r.status == 200
    assert r.headers["Content-Type"] == "audio/mpeg"
    assert int(r.headers["Content-Length"]) == (60 - 20) * BYTERATE
    body = await r.read()
    assert len(body) == (60 - 20) * BYTERATE
    assert abs(tone_at(body) - 880) < 40          # 20 s in = start of part 2
    assert abs(tone_at(body, at=21) - 1320) < 40  # 41 s in = part 3
    assert abs(duration_of(body) - 40) < 1.0
    assert ("play", "li1", f"yoto-{s.slot}") in env.abs.calls


async def test_finished_book_restarts(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 59.0, "duration": 60.0}
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == 60 * BYTERATE
    assert abs(tone_at(await r.read()) - 440) < 40


async def test_unassigned_card_404(env):
    br, client, _ = await env.make()
    s = br.store.new_slot("card")
    r = await client.get(f"/s/{s.slot}.mp3")
    assert r.status == 404


async def test_range_request_continues_same_session(env):
    br, client, _ = await env.make()
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    await r.read()
    plays = [c for c in env.abs.calls if c[0] == "play"]
    r = await client.get(f"/s/{s.slot}.mp3", headers={"Range": f"bytes={30 * BYTERATE}-"})
    assert r.status == 206
    assert r.headers["Content-Range"] == f"bytes {30 * BYTERATE}-{60 * BYTERATE - 1}/{60 * BYTERATE}"
    body = await r.read()
    assert len(body) == 30 * BYTERATE
    assert abs(tone_at(body) - 880) < 40          # 30 s in
    assert [c for c in env.abs.calls if c[0] == "play"] == plays  # no new ABS session


async def test_mqtt_position_synced_to_abs(env):
    """Player reports position 12 s into the stream; ABS gets base + 12."""
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    pb, start = await br.pm.open_stream(br.store.get(s.slot), 0, "10.0.0.5")
    assert start == 20.0
    pb.main_started -= 30                     # stream has been open 30 s
    br.pm.on_bytes(pb, 30 * BYTERATE, True)   # and 30 s of audio delivered
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 12, "playbackStatus": "paused"})
    assert pb.device_id == "dev-mini"
    await asyncio.sleep(0.05)                 # paused triggers an immediate sync
    syncs = [c for c in env.abs.calls if c[0] == "sync"]
    assert syncs and abs(syncs[-1][2] - 32.0) < 0.5
    assert env.abs.progress["li1"]["currentTime"] == pytest.approx(32.0, abs=0.5)


async def test_mqtt_position_clamped_by_delivered_audio(env):
    """A position beyond what was sent (e.g. a counter that did not reset) is clamped."""
    br, client, _ = await env.make()
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    pb.main_started -= 10
    br.pm.on_bytes(pb, 10 * BYTERATE, True)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 500, "playbackStatus": "paused"})
    assert br.pm.position(pb) <= 10.01


async def test_counter_carried_over_after_reconnect(env):
    """If the player's position counter does not restart on a new stream, the offset is removed."""
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 105.0, "duration": 60.0}  # beyond end -> restart at 0
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    pb.main_started -= 20
    br.pm.on_bytes(pb, 30 * BYTERATE, True)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 300, "playbackStatus": "playing"})
    assert pb.event_offset == pytest.approx(280, abs=1)
    br.pm.on_event("dev-mini", {"position": 290, "playbackStatus": "paused"})
    assert br.pm.position(pb) == pytest.approx(10, abs=1)


async def test_no_mqtt_estimate_and_close_on_stop(env):
    """No MQTT at all: position = delivered audio minus the player buffer; closed after the grace period."""
    br, client, _ = await env.make()
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    pb.main_started -= 140
    br.pm.on_bytes(pb, 40 * BYTERATE, True)
    br.pm.close_stream(pb)
    pb.ended -= 100                           # stream closed 100 s ago after 40 s of audio
    await br.pm.tick()
    close = [c for c in env.abs.calls if c[0] == "close"]
    assert close and close[-1][2] == pytest.approx(20.0, abs=0.5)   # 40 delivered - 20 buffer
    assert s.slot not in br.pm.by_slot


async def test_other_cards_events_ignored(env):
    br, client, _ = await env.make()
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 1, "playbackStatus": "playing"})
    br.pm.on_event("dev-mini", {"cardId": "some-shop-card", "position": 999, "playbackStatus": "playing"})
    assert pb.event.get("position") == 1


async def test_restart_from_zero_rereads_abs(env):
    """Re-tapping the card (a new request from byte 0) closes the old session and starts from ABS's
    latest position."""
    br, client, _ = await env.make()
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    await r.read()
    pb = br.pm.by_slot[s.slot]
    pb.main_started -= 30                                   # pretend 30 s were heard
    r = await client.get(f"/s/{s.slot}.mp3")
    calls = [c[0] for c in env.abs.calls]
    assert calls == ["play", "close", "play"]
    closed_at = env.abs.calls[1][2]
    assert int(r.headers["Content-Length"]) == int((60 - (closed_at - 5)) * BYTERATE)
    await r.read()


async def test_phone_position_wins_after_yoto_stops(env):
    """Yoto stopped, then the phone moved on before the bridge's final write: the phone's position stays."""
    br, client, _ = await env.make()
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    pb.main_started -= 140
    br.pm.on_bytes(pb, 40 * BYTERATE, True)
    br.pm.close_stream(pb)
    pb.ended -= 100
    await asyncio.sleep(0.01)
    env.abs.progress["li1"] = {"currentTime": 50.0, "duration": 60.0, "lastUpdate": int(time.time() * 1000) + 5000}
    await br.pm.tick()
    assert not [c for c in env.abs.calls if c[0] == "close"]
    assert env.abs.progress["li1"]["currentTime"] == 50.0
    assert s.slot not in br.pm.by_slot


async def test_phone_play_on_yoto(env):
    br, client, control = await env.make()
    await br.connect_yoto()
    assert ("connect", ("dev-mini", "dev-player")) in control.commands
    remote = br.store.remote_for("dev-mini")
    assert remote and remote.yoto_card_id
    posted = [p for p in env.yoto.posts if p["title"] == "Audiobookshelf on Mini"][0]
    track = posted["content"]["chapters"][0]["tracks"][0]
    assert track == {"key": "01", "title": "Audiobookshelf on Mini", "overlayLabel": "1", "type": "stream",
                     "format": "mp3", "trackUrl": f"http://bridge.test:8080/s/{remote.slot}.mp3",
                     "display": {"iconUrl16x16": f"http://bridge.test:8080/s/icon/{remote.slot}.png"}}
    env.abs.progress["li2"] = {"currentTime": 33.0, "duration": 60.0}
    r = await client.post("/api/play", json={"device_id": "dev-mini", "item_id": "li2"})
    assert r.status == 200
    assert control.commands[-1] == ("play", "dev-mini", remote.yoto_card_id, None)
    r = await client.get(f"/s/{remote.slot}.mp3")
    assert int(r.headers["Content-Length"]) == (60 - 28) * BYTERATE
    await r.read()
    assert br.pm.by_slot[remote.slot].device_id == "dev-mini"
    assert ("play", "li2", "yoto-dev-mini") in env.abs.calls


async def test_convert_existing_myo_playlist(env):
    br, client, _ = await env.make()
    r = await client.post("/api/cards/convert", json={"yoto_card_id": "existing1", "item_id": "li1"})
    assert r.status == 400   # needs explicit confirm
    r = await client.post("/api/cards/convert", json={"yoto_card_id": "existing1", "item_id": "li1", "confirm": True})
    assert r.status == 200
    slot = (await r.json())["slot"]
    post = env.yoto.posts[-1]
    assert post["cardId"] == "existing1"
    assert post["content"]["chapters"][0]["tracks"][0]["trackUrl"] == f"http://bridge.test:8080/s/{slot}.mp3"
    assert post["metadata"]["cover"]["imageL"] == "https://cdn.example/cover.jpg"
    # reassign the card to another book: local + title update, same stream URL
    r = await client.put(f"/api/cards/{slot}", json={"item_id": "li2"})
    assert r.status == 200
    assert br.store.get(slot).item_id == "li2"
    assert env.yoto.posts[-1]["title"] == "Book li2"
    assert env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]["trackUrl"].endswith(f"/s/{slot}.mp3")


async def test_create_card_and_pin(env):
    br, client, _ = await env.make(ui_pin="1234")
    r = await client.post("/api/cards", json={"item_id": "li1"})
    assert r.status == 401
    r = await client.post("/api/cards", json={"item_id": "li1"}, headers={"X-Pin": "1234"})
    assert r.status == 200
    cards = await (await client.get("/api/cards")).json()
    assert cards[0]["title"] == "Book li1" and cards[0]["yoto_card_id"]


async def test_seconds_in_mode(env):
    """Fallback mode if test (b) passes and you prefer it: whole book stream, MQTT secondsIn resume."""
    br, client, control = await env.make(resume_mode="seconds_in")
    env.abs.progress["li1"] = {"currentTime": 40.0, "duration": 60.0}
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == 60 * BYTERATE   # whole book
    pb = br.pm.by_slot[s.slot]
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 2, "playbackStatus": "playing", "source": "card"})
    await asyncio.sleep(0.05)
    assert control.commands[-1] == ("play", "dev-mini", "yc1", 35)
    await r.read()
    br.pm.on_event("dev-mini", {"position": 41, "playbackStatus": "paused"})
    assert br.pm.position(pb) == pytest.approx(41, abs=0.5)


async def test_status_and_browse(env):
    br, client, _ = await env.make()
    await br.connect_yoto()
    st = await (await client.get("/api/status")).json()
    assert st["yoto_logged_in"] and st["mqtt"] and [d["name"] for d in st["devices"]] == ["Mini", "Player"]
    libs = await (await client.get("/api/libraries")).json()
    books = await (await client.get(f"/api/books?library={libs[0]['id']}")).json()
    assert books["total"] == 2
    env.abs.progress["li1"] = {"currentTime": 10.0, "duration": 60.0}
    cont = await (await client.get("/api/continue")).json()
    assert cont[0]["id"] == "li1" and cont[0]["position"] == 10.0
    assert (await client.get("/api/cover/li1")).status == 200
    page = await client.get("/")
    from yotobridge.app import STATIC
    if (STATIC / "index.html").exists():
        assert page.status == 200
    else:                                      # bare checkout: the page is built by the Dockerfile from web/
        assert page.status == 503 and "npm run build" in await page.text()


async def test_yoto_control_maps_mqtt_events(monkeypatch):
    """The real YotoControl, with yoto-api's MQTT client replaced: events arrive in wire names,
    card/start goes to the right topic with the configured uri."""
    import json as _json
    import yoto_api.mqtt.client as mc
    from yoto_api.mqtt.parser import parse_message
    from yotobridge.yoto import YotoControl

    class FakeMqtt:
        is_connected = True
        def __init__(self): self.published = []
        async def connect(self, token, ids, cb, token_getter=None): self.cb = cb
        async def disconnect(self): pass
        async def _publish(self, topic, payload=None): self.published.append((topic, payload))

    monkeypatch.setattr(mc, "YotoMqttClient", FakeMqtt)
    got = []
    ctl = YotoControl(auth=type("A", (), {"access_token": None})(), uri_format="https://yoto.io/{card_id}", on_event=lambda d, e: got.append((d, e)))
    await ctl.connect(["dev-mini"])
    patch = parse_message("device/dev-mini/data/events", _json.dumps(
        {"cardId": "yc1", "position": 42, "trackLength": 0, "playbackStatus": "paused", "source": "card"}).encode())
    ctl.client.cb(patch)
    assert got == [("dev-mini", {"cardId": "yc1", "position": 42, "trackLength": 0, "playbackStatus": "paused",
                                 "source": "card"})]
    await ctl.play("dev-mini", "yc1", 120)
    topic, payload = ctl.client.published[-1]
    assert topic == "device/dev-mini/command/card/start"
    assert _json.loads(payload) == {"uri": "https://yoto.io/yc1", "chapterKey": "01", "trackKey": "01", "secondsIn": 120}


async def test_public_url_change_rewrites_playlists(env):
    """After test (a) forces https (or the host gets a new IP), a restart rewrites every playlist."""
    br, client, _ = await env.make()
    await br.connect_yoto()
    await client.post("/api/cards", json={"item_id": "li1"})
    n = len(env.yoto.posts)
    br.cfg.public_url = "https://yoto-bridge.example.duckdns.org"
    await br.connect_yoto()
    new = env.yoto.posts[n:]
    assert len(new) == 3   # two remote playlists + one card
    assert all(p["content"]["chapters"][0]["tracks"][0]["trackUrl"].startswith("https://yoto-bridge.example") for p in new)
    assert all(p.get("cardId") for p in new)


# ---------------------------------------------------------------- Yoto login (authorization code + PKCE)

async def _start_login(br, client):
    br.auth.tokens = {}
    br.auth.file.unlink(missing_ok=True)
    r = await client.post("/api/yoto/login")
    assert r.status == 200
    status = await (await client.get("/api/status")).json()
    return status["login_pending"]


async def test_login_link_uses_pkce_and_hides_verifier(env):
    from urllib.parse import parse_qs, urlsplit
    br, client, _ = await env.make()
    pending = await _start_login(br, client)
    q = {k: v[0] for k, v in parse_qs(urlsplit(pending["url"]).query).items()}
    assert urlsplit(pending["url"]).path == "/authorize"
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256" and q["client_id"] == "cid"
    assert q["redirect_uri"] == "http://bridge.test:8080/s/yoto-callback" == pending["redirect_uri"]
    assert "offline_access" in q["scope"] and q["state"]
    assert "verifier" not in str(pending)


async def test_login_callback_completes_and_connects(env):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlsplit
    br, client, control = await env.make()
    pending = await _start_login(br, client)
    q = {k: v[0] for k, v in parse_qs(urlsplit(pending["url"]).query).items()}
    r = await client.get("/s/yoto-callback", params={"code": "good-code", "state": q["state"]})
    assert r.status == 200 and "Yoto connected" in await r.text()
    sent = env.yoto.token_requests[-1]
    assert sent["grant_type"] == "authorization_code" and sent["redirect_uri"] == q["redirect_uri"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"]
    status = await (await client.get("/api/status")).json()
    assert status["yoto_logged_in"] and status["login_pending"] is None
    assert br.auth.file.exists() and br.auth.tokens["refresh_token"] == "new-rt"
    assert ("connect", ("dev-mini", "dev-player")) in control.commands


async def test_login_callback_rejects_wrong_state(env):
    br, client, _ = await env.make()
    await _start_login(br, client)
    r = await client.get("/s/yoto-callback", params={"code": "good-code", "state": "someone-else"})
    assert r.status == 400
    assert not env.yoto.token_requests and not br.auth.logged_in


async def test_login_shows_yoto_error(env):
    from urllib.parse import parse_qs, urlsplit
    br, client, _ = await env.make()
    pending = await _start_login(br, client)
    state = parse_qs(urlsplit(pending["url"]).query)["state"][0]
    r = await client.get("/s/yoto-callback", params={"error": "access_denied", "error_description": "User said no",
                                                    "state": state})
    assert r.status == 400 and "User said no" in await r.text()
    r = await client.get("/s/yoto-callback", params={"code": "bad-code", "state": state})
    assert r.status == 400 and "Invalid authorization code" in await r.text()
    assert not br.auth.logged_in


async def test_login_by_pasting_landing_address(env):
    from urllib.parse import parse_qs, urlsplit
    br, client, _ = await env.make(yoto_redirect_uri="http://127.0.0.1:8787/callback")
    pending = await _start_login(br, client)
    q = parse_qs(urlsplit(pending["url"]).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8787/callback"]
    r = await client.post("/api/yoto/finish", json={"url": "http://127.0.0.1:8787/callback?code=bad-code"})
    assert r.status == 400   # no state: not this login
    r = await client.post("/api/yoto/finish",
                          json={"url": f" http://127.0.0.1:8787/callback?code=good-code&state={q['state'][0]} "})
    assert r.status == 200 and br.auth.logged_in
    assert env.yoto.token_requests[-1]["redirect_uri"] == "http://127.0.0.1:8787/callback"


# ---------------------------------------------------------------- offline copies

from yotobridge.offline import plan_segments, plan_whole  # noqa: E402


def test_plan_segments_cuts_at_chapters_and_splits_long_ones():
    ch = [{"start": 0, "end": 600, "title": "One"}, {"start": 600, "end": 4800, "title": "Two"},
          {"start": 4800, "end": 4900, "title": "Three"}, {"start": 4900, "end": 5000, "title": "Four"}]
    segs = plan_segments(300, 5000, ch, 3300)
    assert segs[0][:2] == (300, 600)                       # starts exactly at the resume point
    assert [round(s[1] - s[0]) for s in segs[1:3]] == [2100, 2100]  # 70 min chapter -> two 35 min tracks
    assert segs[1][2] == "Two (1/2)"
    assert segs[-1][:2] == (4800, 5000)                    # two short chapters merged into one track
    assert all(e - s <= 3300 for s, e, _ in segs)
    assert all(a[1] == b[0] for a, b in zip(segs, segs[1:]))   # contiguous, no gaps


def test_plan_segments_without_chapters():
    segs = plan_segments(0, 3 * 3600, [], 3300)
    assert len(segs) == 4 and segs[-1][1] == 3 * 3600


async def _wait_offline(client, slot, want=("ready", "failed")):
    for _ in range(400):
        cards = await (await client.get("/api/cards")).json()
        info = next(c for c in cards if c["slot"] == slot)["offline"]
        if info and info["status"] in want:
            return info
        await asyncio.sleep(0.05)
    raise AssertionError("offline copy did not finish")


async def test_make_offline_uploads_chapters_from_abs_position(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    r = await client.post(f"/api/cards/{s.slot}/offline")
    assert r.status == 200
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready", info
    assert info["start"] == 20.0 and info["end"] == 60.0 and info["tracks"] == 4   # 5 s rewind; ch2 split in two
    # every uploaded file is the right stretch of the book
    ups = list(env.yoto.uploads.values())
    assert [round(duration_of(u)) for u in ups] == [10, 10, 10, 10]
    assert abs(tone_at(ups[0]) - 880) < 40      # 20 s into the book = part 2
    assert abs(tone_at(ups[2]) - 1320) < 40     # 40 s = part 3
    body = env.yoto.posts[-1]
    assert body["cardId"] == "yc1"
    chapters = body["content"]["chapters"]
    assert [c["key"] for c in chapters] == ["01", "02", "03", "04"]
    t = chapters[0]["tracks"][0]
    assert t["type"] == "audio" and t["trackUrl"] == "yoto:#sha-up1" and t["duration"] == 10
    assert "Chapter 2" in chapters[0]["title"]


async def test_offline_positions_sync_to_abs(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline")
    await _wait_offline(client, s.slot)
    # player reports chapter 03 (book 40-50 s), 4 s in, paused
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "03", "trackKey": "03", "position": 4,
                                  "playbackStatus": "paused"})
    for _ in range(100):
        if env.abs.progress["li1"]["currentTime"] == 44:
            break
        await asyncio.sleep(0.02)
    assert env.abs.progress["li1"]["currentTime"] == 44
    assert ("patch", "li1", 44) in env.abs.calls


async def test_offline_copy_at_a_speed(env):
    """At 2x: twice the book per track and per hour, sped-up audio, and player positions doubled."""
    br, client, _ = await env.make(offline_track_seconds=15)
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    assert (await client.post(f"/api/cards/{s.slot}/offline", json={"speed": 9})).status == 409
    r = await client.post(f"/api/cards/{s.slot}/offline", json={"speed": 2})
    assert r.status == 200
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready" and info["speed"] == 2.0 and info["tracks"] == 2   # 20 s chapters fit 30 s tracks
    ups = list(env.yoto.uploads.values())
    assert [round(duration_of(u)) for u in ups] == [10, 10]
    assert abs(tone_at(ups[0]) - 880) < 60 and abs(tone_at(ups[1]) - 1320) < 60
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "02", "trackKey": "02", "position": 4,
                                  "playbackStatus": "paused"})
    for _ in range(100):
        if env.abs.progress["li1"]["currentTime"] == 48:
            break
        await asyncio.sleep(0.02)
    assert env.abs.progress["li1"]["currentTime"] == 48            # 40 s + 4 s of audio at 2x


async def test_offline_copy_defaults_to_the_cards_speed(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    s = card(br)
    s.speed = 1.5
    await client.post(f"/api/cards/{s.slot}/offline")
    assert (await _wait_offline(client, s.slot))["speed"] == 1.5


async def test_offline_copy_never_drags_phone_backwards(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline")
    await _wait_offline(client, s.slot)
    # meanwhile the phone listened on to 55 s
    env.abs.progress["li1"] = {"currentTime": 55.0, "duration": 60.0, "lastUpdate": int((time.time() + 10) * 1000)}
    await br.offline.write(s, 22.0, "dev-mini")
    assert env.abs.progress["li1"]["currentTime"] == 55.0


async def test_back_to_streaming_restores_stream_track(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline")
    await _wait_offline(client, s.slot)
    r = await client.post(f"/api/cards/{s.slot}/online")
    assert r.status == 200
    track = env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]
    assert track["type"] == "stream" and track["trackUrl"] == f"http://bridge.test:8080/s/{s.slot}.mp3"
    assert br.store.get(s.slot).offline == {}


async def test_failed_upload_leaves_card_streaming(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.yoto.fail_uploads = True
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline")
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "failed" and "500" in info["error"]
    assert env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]["type"] == "stream"


def test_plan_whole_one_chapter_per_track_when_it_fits():
    # 10, 10, 70 (a book listed as one huge chapter), 10 and 10 minutes
    starts = [0, 600, 1200, 5400, 6000]
    ch = [{"start": st, "title": f"Ch {i + 1}"} for i, st in enumerate(starts)]
    dur = 6600
    [plan] = plan_whole([(ch, dur)], math.inf)
    assert [t[2] for t in plan] == ["Ch 1", "Ch 2", "Ch 3 (1/3)", "Ch 3 (2/3)", "Ch 3 (3/3)", "Ch 4", "Ch 5"]
    assert plan[0][0] == 0 and plan[-1][1] == dur
    assert all(a[1] == b[0] for a, b in zip(plan, plan[1:]))


def test_plan_whole_series_fills_tracks_to_stay_under_99():
    # 6 books x 40 chapters of 5 min = 240 chapters, 20 h: tracks have to hold several chapters
    books = [([{"start": i * 300, "end": (i + 1) * 300, "title": f"B{b} C{i + 1}"} for i in range(40)], 40 * 300)
             for b in range(6)]
    plans = plan_whole(books, 3300)
    n = sum(len(p) for p in plans)
    assert n == 84          # 3 chapters a track (14 per book); 2 a track would be 120, over 99
    for p, (_, dur) in zip(plans, books):
        assert p[0][0] == 0 and p[-1][1] == dur            # each book whole, no track spans two books
        assert all(a[1] == b[0] for a, b in zip(p, p[1:]))
        assert all(e - s <= 3300 for s, e, _ in p)
        assert all(round(s) % 300 == 0 for s, _, _ in p)   # cut only at chapter boundaries
    assert plans[0][0][2] == "B0 C1 – B0 C3"


def test_plan_whole_without_a_track_limit_keeps_chapters_up_to_an_hour():
    ch = [{"start": 0, "end": 3500, "title": "Long"}, {"start": 3500, "end": 3800, "title": "Short"}]
    [plan] = plan_whole([(ch, 3800)], math.inf)
    assert plan == [(0, 3500, "Long"), (3500, 3800, "Short")]
    books = [([{"start": i * 300, "end": (i + 1) * 300, "title": f"C{i}"} for i in range(150)], 45000)] * 2
    plans = plan_whole(books, math.inf)
    assert sum(len(p) for p in plans) <= 99 and all(p[-1][1] == 45000 for p in plans)


def test_plan_whole_never_makes_a_whole_long_book_one_track():
    """A 19 h book with one "chapter" (or none) goes in 30 min parts, packed to fit 99 tracks; a 19 h,
    548 MB track never finished processing at Yoto."""
    long_book = ([{"start": 0, "title": "Opening"}, {"start": 60, "title": "Book"}], 19 * 3600)
    books = [long_book] + [([{"start": i * 1200, "title": f"C{i}"} for i in range(30)], 36000)] * 4
    plans = plan_whole(books, math.inf)
    assert sum(len(p) for p in plans) <= 99
    assert max(e - s for p in plans for s, e, _ in p) <= 2 * 3600


def test_plan_whole_book_without_chapters():
    [plan] = plan_whole([([], 2 * 3600)], 3300)
    assert len(plan) == 4 and plan[-1][1] == 2 * 3600          # 30 min parts, as on a streaming card


def test_plan_whole_covers_audio_no_chapter_covers():
    # broken chapter data: a 20 h book listed as a 106 min chapter and an empty one
    chapters = [{"start": 0, "end": 6360, "title": "Chapter 1"}, {"start": 6360, "end": 6360, "title": "Chapter 2"}]
    [plan] = plan_whole([(chapters, 1248 * 60)], math.inf)
    assert plan[0][0] == 0 and plan[-1][1] == 1248 * 60
    assert max(e - s for s, e, _ in plan) <= 1800


async def test_an_upload_yoto_never_finishes_is_uploaded_again(env):
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    br.yoto.TRANSCODE_TIMEOUT = 0.1
    env.yoto.stuck.add("up1")                                      # the first upload's job never finishes
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready", info
    shas = [c["tracks"][0]["trackUrl"] for c in env.yoto.posts[-1]["content"]["chapters"]]
    assert shas[0] == "yoto:#sha-up2"                              # track 1 went up again
    env.yoto.stuck.update({"up5", "up6", "up7"})                   # never finished, three times: fails
    await client.post(f"/api/cards/{s.slot}/online")
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "failed" and "3 tries" in info["error"], info


async def test_audiobookshelf_away_mid_copy_waits_and_carries_on(env):
    """ABS restarting (connection refused, 503) part-way through a copy: the copy waits and carries on with
    the tracks it has, and no track is uploaded with silence where the audio should be."""
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    br.offline.RETRY_WAITS = (0.05,) * 3
    s = card(br)
    env.abs.down, env.abs.down_files = 1, 1  # the first session and the first audio request are refused
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready", info
    ups = list(env.yoto.uploads.values())
    assert len(ups) == 3 and abs(tone_at(ups[0]) - 440) < 40 and abs(tone_at(ups[2]) - 1320) < 40
    # away for longer than the copy waits: it fails, keeping what was uploaded for the next try
    await client.post(f"/api/cards/{s.slot}/online")
    env.abs.down = 1000
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "failed" and "503" in info["error"], info
    assert env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]["type"] == "stream"


async def test_whole_book_copy_one_chapter_per_track(env):
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    env.abs.progress["li1"] = {"currentTime": 45.0, "duration": 60.0}
    s = card(br)
    r = await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    assert r.status == 200
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready", info
    assert (info["mode"], info["start"], info["end"], info["tracks"]) == ("book", 0, 60.0, 3)
    chapters = env.yoto.posts[-1]["content"]["chapters"]
    assert [c["title"] for c in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    ups = list(env.yoto.uploads.values())
    assert abs(tone_at(ups[0]) - 440) < 40 and abs(tone_at(ups[2]) - 1320) < 40
    # the player starts the new copy at track 1: ABS is not dragged back, and the player is moved on to
    # where the book was left (45 s less the 5 s rewind = 40 s, the start of track 3)
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "01", "position": 2, "playbackStatus": "playing"})
    await asyncio.sleep(0.05)
    assert env.abs.progress["li1"]["currentTime"] == 45.0
    assert br.control.commands[-1] == ("play", "dev-mini", "yc1", 0) and br.control.tracks[-1] == 3
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "01", "position": 3, "playbackStatus": "playing"})
    await asyncio.sleep(0.05)
    assert len([c for c in br.control.commands if c[0] == "play"]) == 1     # only once
    # once there, positions sync as usual, going back included
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "03", "position": 7, "playbackStatus": "paused"})
    for _ in range(100):
        if env.abs.progress["li1"]["currentTime"] == 47:
            break
        await asyncio.sleep(0.02)
    assert env.abs.progress["li1"]["currentTime"] == 47
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "01", "position": 5, "playbackStatus": "paused"})
    for _ in range(100):
        if env.abs.progress["li1"]["currentTime"] == 5:
            break
        await asyncio.sleep(0.02)
    assert env.abs.progress["li1"]["currentTime"] == 5


async def test_listening_while_the_copy_is_made_moves_the_resume_point(env):
    """The card streams while the copy uploads; the place it got to then is where the copy resumes."""
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    env.abs.progress["li1"] = {"currentTime": 5.0, "duration": 60.0}
    s = card(br)
    real = br.yoto.upload_audio

    async def upload(audio, name, **kw):
        env.abs.progress["li1"] = {"currentTime": 50.0, "duration": 60.0}    # streamed on meanwhile
        return await real(audio, name, **kw)
    br.yoto.upload_audio = upload
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    assert (await _wait_offline(client, s.slot))["status"] == "ready"
    assert br.store.get(s.slot).offline["resume"] == {"item_id": "li1", "pos": 45.0}
    br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "01", "position": 1, "playbackStatus": "playing"})
    await asyncio.sleep(0.05)
    assert env.abs.progress["li1"]["currentTime"] == 50.0                 # not dragged back
    assert br.control.commands[-1] == ("play", "dev-mini", "yc1", 5) and br.control.tracks[-1] == 3


async def test_copy_cut_short_by_a_restart_carries_on(env):
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    s = card(br)
    real, n = br.yoto.upload_audio, []

    async def upload(audio, name, **kw):
        n.append(name)
        if len(n) == 2:
            raise asyncio.CancelledError                 # the bridge stops during the second upload
        return await real(audio, name, **kw)
    br.yoto.upload_audio = upload
    br.offline.start(s, None, None, "book")
    with pytest.raises(asyncio.CancelledError):
        await br.offline.tasks[s.slot]
    off = br.store.get(s.slot).offline
    assert off["status"] == "building" and len(off["uploads"]) == 1
    assert not any(t["type"] == "audio" for p in env.yoto.posts for c in p["content"]["chapters"]
                   for t in c["tracks"])                # the card was not touched: it still streams
    br.yoto.upload_audio = real
    br.offline.resume_interrupted()                      # as at start-up
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready" and info["tracks"] == 3
    assert len(env.yoto.uploads) == 3                    # track 1 was not uploaded again


async def test_make_offline_again_can_redo_tracks_from_a_number(env):
    """Tracks uploaded while ABS was going down (padded with silence by older bridges) are redone."""
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    s = card(br)
    real, n = br.yoto.upload_audio, []

    async def upload(audio, name, **kw):
        n.append(name)
        if len(n) == 3:
            raise RuntimeError("Audiobookshelf went away")
        return await real(audio, name, **kw)
    br.yoto.upload_audio = upload
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "failed" and len(br.store.get(s.slot).offline["uploads"]) == 2
    br.yoto.upload_audio = real
    before = len(env.yoto.uploads)
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book", "redo_from": 2})
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready" and info["tracks"] == 3
    assert len(env.yoto.uploads) - before == 2          # track 1 reused, tracks 2 and 3 uploaded


async def test_tracks_go_offline_one_by_one_while_the_rest_stream(env, monkeypatch):
    import yotobridge.offline as off_mod
    monkeypatch.setattr(off_mod, "LIVE_BATCH", 1)
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    s = card(br)
    real, seen = br.yoto.upload_audio, []

    async def upload(audio, name, **kw):
        if len(seen) == 1:                               # track 2 uploading: what is on the card now?
            off = br.store.get(s.slot).offline
            seen.append([t["on_card"] for t in off["tracks"]])
            url = env.yoto.posts[-1]["content"]["chapters"][2]["tracks"][0]["trackUrl"]
            r = await client.get(url.replace("http://bridge.test:8080", ""))
            seen.append(r.status == 200 and r.content_type == "audio/mpeg")   # track 3 streams from the bridge
            # the player reports track 1 (on the card) and track 3 (streaming)
            br.on_yoto_event("dev-mini", {"cardId": "yc1", "chapterKey": "01", "position": 6,
                                          "playbackStatus": "paused"})
        seen.append(name)
        return await real(audio, name, **kw)
    br.yoto.upload_audio = upload
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    assert (await _wait_offline(client, s.slot))["status"] == "ready"
    kinds = [[t["tracks"][0]["type"] for t in p["content"]["chapters"]] for p in env.yoto.posts
             if p["content"]["chapters"] and len(p["content"]["chapters"]) == 3]
    # track 3 streaming from the bridge counts as the card playing, so the list after track 2 waits
    assert kinds[-3:] == [["stream"] * 3, ["audio", "stream", "stream"], ["audio"] * 3]
    assert seen[1] == [True, False, False] and seen[2] is True
    assert env.abs.progress["li1"]["currentTime"] == 6                  # track 1 synced while uploading


async def test_skipping_to_a_streamed_track_of_a_copy_being_made_plays_it(env):
    # the book was left in track 1 (on the card); the listener skips to track 3, not uploaded yet
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    env.abs.progress["li1"] = {"currentTime": 6.0, "duration": 60.0}
    s = card(br)
    gate, real = asyncio.Event(), br.yoto.upload_audio

    async def upload(audio, name, **kw):
        await gate.wait()
        return await real(audio, name, **kw)
    br.yoto.upload_audio = upload
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    for _ in range(100):
        if br.store.get(s.slot).offline.get("tracks") and env.yoto.posts and \
                env.yoto.posts[-1]["content"]["chapters"][-1]["tracks"][0]["type"] == "stream":
            break
        await asyncio.sleep(0.02)
    url = env.yoto.posts[-1]["content"]["chapters"][2]["tracks"][0]["trackUrl"].replace("http://bridge.test:8080", "")
    n = len(br.control.commands)
    assert (await client.head(url)).status == 200
    r = await client.get(url)
    assert r.status == 200 and len(await r.read()) == 20 * br.cfg.byterate     # chapter 3, not silence
    assert not any(c[0] == "play" for c in br.control.commands[n:])             # no move back to track 1
    gate.set()
    assert (await _wait_offline(client, s.slot))["status"] == "ready"


async def test_no_track_list_change_while_the_card_plays(env, monkeypatch):
    import yotobridge.offline as off_mod
    monkeypatch.setattr(off_mod, "LIVE_BATCH", 1)
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    s = card(br)
    br.pm.devices["dev-mini"] = {"cardId": "yc1", "playbackStatus": "playing", "at": time.time()}
    n = len(env.yoto.posts)
    await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "book"})
    assert (await _wait_offline(client, s.slot))["status"] == "ready"
    assert len(env.yoto.posts) == n + 1                                  # only the finished copy


async def test_whole_series_copy_is_the_whole_series_wherever_the_listener_is(env):
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    env.abs.progress["li1"] = {"currentTime": 60.0, "duration": 60.0, "isFinished": True, "lastUpdate": 1000}
    env.abs.progress["li2"] = {"currentTime": 30.0, "duration": 60.0, "lastUpdate": 2000}
    slot = await _series_card(client)
    r = await client.post(f"/api/cards/{slot}/offline", json={"mode": "series"})
    assert r.status == 200
    info = await _wait_offline(client, slot)
    assert info["status"] == "ready", info
    assert (info["from_title"], info["to_title"], info["tracks"]) == ("Book li1", "Book li3", 9)
    off = br.store.get(slot).offline
    assert [t["item_id"] for t in off["tracks"]] == ["li1"] * 3 + ["li2"] * 3 + ["li3"] * 3
    assert off["tracks"][0]["start"] == 0 and off["books_left_off"] == 0
    assert off["resume"]["item_id"] == "li2"            # the player is still moved to where book 2 was left
    assert br.offline._resume_target(off)[0] == 5       # book 2, chapter 2
    assert env.yoto.posts[-1]["content"]["chapters"][6]["title"] == "Book li3: Chapter 1"


async def test_progress_reset_while_a_series_copy_is_made_moves_the_start_back(env):
    """Seen on a real card: test skips left book 2 half-listened; it was reset in ABS during the upload."""
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    env.abs.progress["li2"] = {"currentTime": 30.0, "duration": 60.0, "lastUpdate": 2000}
    slot = await _series_card(client)
    gate, real = asyncio.Event(), br.yoto.upload_audio

    async def upload(audio, name, **kw):
        await gate.wait()
        return await real(audio, name, **kw)
    br.yoto.upload_audio = upload
    await client.post(f"/api/cards/{slot}/offline", json={"mode": "series"})
    for _ in range(100):
        if br.store.get(slot).offline.get("resume"):
            break
        await asyncio.sleep(0.02)
    assert br.store.get(slot).offline["resume"]["item_id"] == "li2"
    env.abs.progress.pop("li2")                                       # reset in ABS
    env.abs.progress["li1"] = {"currentTime": 12.0, "duration": 60.0, "lastUpdate": 3000}
    gate.set()
    assert (await _wait_offline(client, slot))["status"] == "ready"
    s = br.store.get(slot)
    assert s.offline["resume"] == {"item_id": "li1", "pos": 7.0} and s.item_id == "li1"


async def test_whole_series_too_long_from_book_one_starts_at_the_current_book(env, monkeypatch):
    import yotobridge.offline as off_mod
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    env.abs.progress["li1"] = {"currentTime": 60.0, "duration": 60.0, "isFinished": True, "lastUpdate": 1000}
    env.abs.progress["li2"] = {"currentTime": 60.0, "duration": 60.0, "isFinished": True, "lastUpdate": 1500}
    env.abs.progress["li3"] = {"currentTime": 30.0, "duration": 60.0, "lastUpdate": 2000}
    monkeypatch.setattr(off_mod, "WHOLE_TRACKS", 4)     # books 1 and 2 would crowd out book 3
    slot = await _series_card(client)
    await client.post(f"/api/cards/{slot}/offline", json={"mode": "series"})
    info = await _wait_offline(client, slot)
    assert (info["status"], info["from_title"], info["to_title"]) == ("ready", "Book li3", "Book li3"), info


async def test_whole_series_needs_a_series_card(env):
    br, client, _ = await env.make()
    s = card(br)
    assert (await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "series"})).status == 409
    assert (await client.post(f"/api/cards/{s.slot}/offline", json={"mode": "bogus"})).status == 409


async def test_whole_series_leaves_off_books_that_do_not_fit(env, monkeypatch):
    import yotobridge.offline as off_mod
    br, client, _ = await env.make(offline_track_seconds=25, offline_whole_track_seconds=25)
    slot = await _series_card(client)
    # no size limit by default: tracks are the limit. 3 chapters a book need 3 tracks; room for 7
    monkeypatch.setattr(off_mod, "WHOLE_TRACKS", 7)
    await client.post(f"/api/cards/{slot}/offline", json={"mode": "series"})
    info = await _wait_offline(client, slot)
    assert info["status"] == "ready", info
    assert (info["from_title"], info["to_title"], info["books_left_off"]) == ("Book li1", "Book li2", 1)
    # with offline_max_mb set, books over it are left off too, and a book over it fails
    monkeypatch.setattr(off_mod, "WHOLE_TRACKS", 99)
    br.cfg.offline_max_mb = 1                        # 125 s of audio at 64 kbps
    await client.post(f"/api/cards/{slot}/online")
    await client.post(f"/api/cards/{slot}/offline", json={"mode": "series"})
    info = await _wait_offline(client, slot)
    assert (info["status"], info["to_title"], info["books_left_off"]) == ("ready", "Book li2", 1)
    br.cfg.offline_max_mb = 0.0001
    await client.post(f"/api/cards/{slot}/online")
    await client.post(f"/api/cards/{slot}/offline", json={"mode": "book"})
    info = await _wait_offline(client, slot)
    assert info["status"] == "failed" and "offline_max_mb" in info["error"]


# ---------------------------------------------------------------- series cards

from yotobridge.series import SeriesBook, current_index  # noqa: E402


def test_series_current_book_rules():
    books = [SeriesBook(f"b{i}", f"B{i}", 100) for i in range(1, 4)]
    assert current_index(books, []) == 0                                            # nothing yet: book 1
    assert current_index(books, [{"libraryItemId": "b1", "isFinished": True, "lastUpdate": 5}]) == 1
    # phone is listening to book 3, an old half-listen of book 1 does not win
    assert current_index(books, [{"libraryItemId": "b1", "currentTime": 40, "lastUpdate": 1},
                                 {"libraryItemId": "b3", "currentTime": 10, "lastUpdate": 9}]) == 2
    assert current_index(books, [{"libraryItemId": "b3", "isFinished": True, "lastUpdate": 9}]) == 0  # all done: restart


async def _series_card(client):
    r = await client.post("/api/cards", json={"series_id": "ser1", "library_id": "lib1"})
    assert r.status == 200, await r.text()
    return (await r.json())["slot"]


async def test_series_card_orders_books_and_streams_into_the_next(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 60.0, "duration": 60.0, "isFinished": True, "lastUpdate": 1000}
    slot = await _series_card(client)
    s = br.store.get(slot)
    assert (s.series_name, s.item_id) == ("Saga", "li2")           # book 1 finished -> book 2
    assert env.yoto.posts[-1]["title"] == "Saga"                    # the Yoto playlist is named after the series
    r = await client.get(f"/s/{slot}.mp3")
    mp3 = await r.read()
    assert int(r.headers["Content-Length"]) == 120 * BYTERATE      # rest of book 2 + book "10" (sorted numerically)
    assert abs(duration_of(mp3) - 120) < 1
    assert abs(tone_at(mp3, 25) - 880) < 40                         # book 2, part 2
    assert abs(tone_at(mp3, 62) - 440) < 40                         # then book 3 from its start
    pb = br.pm.by_slot[slot]
    assert [b.item_id for b in pb.chain] == ["li2", "li3"]


async def test_series_card_finishes_a_book_and_moves_abs_on(env):
    br, client, _ = await env.make()
    slot = await _series_card(client)
    assert br.store.get(slot).item_id == "li1"
    r = await client.get(f"/s/{slot}.mp3", headers={"Range": "bytes=0-7999"})
    await r.read()
    pb = br.pm.by_slot[slot]
    br.pm.position = lambda _pb: 70.0                               # listener is 10 s into book 2
    await br.pm.sync(pb, force=True)
    assert ("patch", "li1", 60.0, "finished") in env.abs.calls
    assert env.abs.progress["li2"]["currentTime"] == 10.0
    assert br.store.get(slot).item_id == "li2" and pb.book_idx == 1


async def test_series_card_follows_the_phone(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 30.0, "duration": 60.0, "lastUpdate": 1000}
    slot = await _series_card(client)
    assert br.store.get(slot).item_id == "li1"
    # the phone then listens to book "10" of the series
    env.abs.progress["li3"] = {"currentTime": 40.0, "duration": 60.0, "lastUpdate": 2000}
    r = await client.get(f"/s/{slot}.mp3")
    mp3 = await r.read()
    assert br.store.get(slot).item_id == "li3"
    assert abs(duration_of(mp3) - 25) < 1                           # from 35 s (5 s rewind) to the end of the series
    assert abs(tone_at(mp3, 1) - 880) < 40      # 36 s of book 3: part 2
    assert abs(tone_at(mp3, 10) - 1320) < 40    # 45 s: part 3


async def test_series_offline_copy_spans_books(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.abs.progress["li1"] = {"currentTime": 45.0, "duration": 60.0, "lastUpdate": 1000}
    slot = await _series_card(client)
    await client.post(f"/api/cards/{slot}/offline")
    info = await _wait_offline(client, slot)
    assert info["status"] == "ready", info
    assert (info["from_title"], info["to_title"]) == ("Book li1", "Book li3")
    off = br.store.get(slot).offline
    assert [t["item_id"] for t in off["tracks"]][:2] == ["li1", "li1"] and off["tracks"][-1]["item_id"] == "li3"
    first_li2 = next(t for t in off["tracks"] if t["item_id"] == "li2")
    br.on_yoto_event("dev-mini", {"cardId": br.store.get(slot).yoto_card_id, "chapterKey": first_li2["key"],
                                  "position": 3, "playbackStatus": "paused"})
    for _ in range(100):
        if env.abs.progress.get("li2", {}).get("currentTime") == 3:
            break
        await asyncio.sleep(0.02)
    assert ("patch", "li1", 60.0, "finished") in env.abs.calls
    assert env.abs.progress["li2"]["currentTime"] == 3


# ---------------------------------------------------------------- collection cards

async def test_book_lists_its_series_and_collections(env):
    br, client, _ = await env.make()
    info = await (await client.get("/api/book/li1")).json()
    assert [(s["id"], s["books"]) for s in info["series"]] == [("ser1", 3)]
    assert [(c["id"], c["name"], c["books"]) for c in info["collections"]] == [("col1", "Bedtime", 2)]
    info2 = await (await client.get("/api/book/li2")).json()
    assert info2["collections"] == []


async def test_collection_card_plays_in_collection_order(env):
    br, client, _ = await env.make()
    r = await client.post("/api/cards", json={"collection_id": "col1", "library_id": "lib1"})
    assert r.status == 200, await r.text()
    slot = (await r.json())["slot"]
    s = br.store.get(slot)
    assert (s.group_kind, s.series_name, s.item_id) == ("collection", "Bedtime", "li3")   # collection order, not title
    assert env.yoto.posts[-1]["title"] == "Bedtime"
    r = await client.get(f"/s/{slot}.mp3")
    await r.read()
    assert [b.item_id for b in br.pm.by_slot[slot].chain] == ["li3", "li1"]
    cards = await (await client.get("/api/cards")).json()
    assert cards[0]["group_kind"] == "collection"


async def test_collection_card_moves_on_after_a_finished_book(env):
    br, client, _ = await env.make()
    env.abs.progress["li3"] = {"currentTime": 60.0, "duration": 60.0, "isFinished": True, "lastUpdate": 5000}
    r = await client.post("/api/cards", json={"collection_id": "col1", "library_id": "lib1"})
    slot = (await r.json())["slot"]
    assert br.store.get(slot).item_id == "li1"


# ---------------- per-player ABS users (one ABS account per child) ----------------

def _two_users():
    from tests.conftest import KEY2
    from yotobridge.config import DeviceConfig
    return {"devices": {"dev-player": DeviceConfig(name="Player", abs_api_key=KEY2)}}


async def test_player_with_own_user_known_from_its_event(env):
    """Behind a proxy or tunnel the request IP is useless; the player's 'card inserted' event says whose it is."""
    br, client, control = await env.make(**_two_users())
    env.abs.progress["li1"] = {"currentTime": 10.0, "duration": 60.0}
    env.abs.progress2["li1"] = {"currentTime": 45.0, "duration": 60.0}
    s = card(br)
    br.pm.on_event("dev-player", {"cardId": "yc1", "playbackStatus": "playing"})
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == (60 - 40) * BYTERATE      # the second user's 45 s, minus 5
    await r.read()
    pb = br.pm.by_slot[s.slot]
    assert pb.device_id == "dev-player" and pb.device_sure
    pb.main_started -= 5
    await br.pm.finish_slot(s.slot, "test")
    assert env.abs.progress["li1"]["currentTime"] == 10.0                # first user untouched
    assert env.abs.progress2["li1"]["currentTime"] >= 40 and "lastUpdate" in env.abs.progress2["li1"]   # written as user 2


async def test_event_arriving_while_the_request_waits(env):
    br, client, control = await env.make(**_two_users())
    env.abs.progress2["li1"] = {"currentTime": 45.0, "duration": 60.0}
    s = card(br)

    async def later():
        await asyncio.sleep(0.3)
        br.pm.on_event("dev-player", {"cardId": "yc1", "playbackStatus": "playing"})
    asyncio.get_running_loop().create_task(later())
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == (60 - 40) * BYTERATE
    await r.read()
    assert not [c for c in control.commands if c[0] == "play"]


async def test_late_event_restarts_card_as_that_players_user(env):
    """No event in time: play as the default user, then restart on the player once it identifies itself."""
    br, client, control = await env.make(identify_wait_seconds=0.2, **_two_users())
    env.abs.progress["li1"] = {"currentTime": 10.0, "duration": 60.0}
    env.abs.progress2["li1"] = {"currentTime": 45.0, "duration": 60.0}
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == (60 - 5) * BYTERATE       # default user's 10 s, minus 5
    await r.read()
    assert not br.pm.by_slot[s.slot].device_sure
    br.pm.on_event("dev-player", {"cardId": "yc1", "position": 1, "playbackStatus": "playing"})
    await asyncio.sleep(0.05)
    assert s.slot not in br.pm.by_slot
    assert control.commands[-1] == ("play", "dev-player", "yc1", None)
    assert not [c for c in env.abs.calls if c[0] in ("sync", "close", "patch")]   # nothing written as the wrong user
    r = await client.get(f"/s/{s.slot}.mp3")                              # the player asks again
    assert int(r.headers["Content-Length"]) == (60 - 40) * BYTERATE
    await r.read()
    assert br.pm.by_slot[s.slot].device_id == "dev-player"
    assert env.abs.progress["li1"]["currentTime"] == 10.0


async def test_player_on_default_user_is_not_restarted(env):
    br, client, control = await env.make(identify_wait_seconds=0.1, **_two_users())
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    await r.read()
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 1, "playbackStatus": "playing"})
    assert br.pm.by_slot[s.slot].device_id == "dev-mini"
    assert not [c for c in control.commands if c[0] == "play"]


async def test_other_players_shop_card_never_triggers_a_restart(env):
    br, client, control = await env.make(identify_wait_seconds=0.1, **_two_users())
    s = card(br)
    r = await client.get(f"/s/{s.slot}.mp3")
    await r.read()
    br.pm.on_event("dev-player", {"cardId": "shop-card", "position": 300, "playbackStatus": "playing"})
    assert s.slot in br.pm.by_slot
    assert not [c for c in control.commands if c[0] == "play"]


async def test_one_user_for_all_players_never_waits(env):
    """Without per-player users nothing changes: no waiting for events before the audio starts."""
    br, client, _ = await env.make(identify_wait_seconds=5)
    s = card(br)
    t = time.monotonic()
    r = await client.get(f"/s/{s.slot}.mp3")
    assert time.monotonic() - t < 2
    await r.read()


# ---------------- alerts ----------------

async def _ntfy(env_like):
    """A fake ntfy server that records what it receives."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    got = []

    async def post(r):
        got.append({"title": r.headers.get("Title"), "priority": r.headers.get("Priority"),
                    "auth": r.headers.get("Authorization"), "body": await r.text()})
        return web.json_response({"id": "x"})
    a = web.Application()
    a.router.add_post("/{topic}", post)
    srv = TestServer(a)
    await srv.start_server()
    return srv, got


async def test_alert_after_repeated_failure_and_on_recovery(env):
    srv, got = await _ntfy(env)
    try:
        br, client, control = await env.make(ntfy_url=str(srv.make_url("/kids-topic")), ntfy_token="tk_1",
                                             alert_check_public=False)
        await br.connect_yoto()
        await br.alerts.check_all()
        assert got == [] and br.alerts.problems == []
        good = br.abs.base
        br.abs.base = "http://127.0.0.1:9"              # ABS down
        await br.alerts.check_all()
        assert got == []                                 # one failure could be a restart
        await br.alerts.check_all()
        assert len(got) == 1 and got[0]["title"] == "Yoto bridge problem" and "Audiobookshelf is not answering" in got[0]["body"]
        assert got[0]["auth"] == "Bearer tk_1" and got[0]["priority"] == "high"
        await br.alerts.check_all()
        assert len(got) == 1                             # announced once, not every check
        r = await client.get("/api/status")
        assert "Audiobookshelf is not answering" in (await r.json())["problems"][0]
        br.abs.base = good
        await br.alerts.check_all()
        assert got[-1]["title"] == "Yoto bridge OK again" and br.alerts.problems == []
    finally:
        await srv.close()


async def test_alert_when_yoto_login_lost_or_mqtt_down(env):
    srv, got = await _ntfy(env)
    try:
        br, client, control = await env.make(ntfy_url=str(srv.make_url("/t")), alert_after_failures=1,
                                             alert_check_public=False)
        await br.connect_yoto()
        control.connected = False
        br.auth.tokens = {}
        await br.alerts.check_all()
        bodies = " ".join(g["body"] for g in got)
        assert "press Connect Yoto" in bodies
        assert "MQTT" not in bodies                       # not logged in explains it; no second alert
        br.auth.tokens = {"access_token": "t", "refresh_token": "r", "expires_in": 86400, "obtained_at": time.time()}
        await br.alerts.check_all()
        assert "MQTT" in got[-1]["body"] or "MQTT" in got[-2]["body"]
    finally:
        await srv.close()


async def test_public_address_check_reaches_this_bridge(env):
    srv, got = await _ntfy(env)
    try:
        br, client, _ = await env.make(ntfy_url=str(srv.make_url("/t")), alert_after_failures=1)
        br.cfg.public_url = str(client.make_url("")).rstrip("/")     # the players' address = this bridge
        assert await br.alerts._check_public() is None
        br.cfg.public_url = "http://127.0.0.1:9"
        assert "can't reach the bridge" in await br.alerts._check_public()
    finally:
        await srv.close()


async def test_card_that_cannot_play_alerts_at_once(env):
    srv, got = await _ntfy(env)
    try:
        br, client, _ = await env.make(ntfy_url=str(srv.make_url("/t")))
        s = card(br, item="missing-book")
        s.title = "Wings of Fire"
        br.cfg.abs_api_key = "wrong"                       # the fake ABS rejects this key
        r = await client.get(f"/s/{s.slot}.mp3")
        assert r.status == 502
        await client.get(f"/s/{s.slot}.mp3")             # second tap: no second alert
        await asyncio.sleep(0.2)
        assert len(got) == 1 and got[0]["title"] == "Yoto card could not play" and "Wings of Fire" in got[0]["body"]
    finally:
        await srv.close()


async def test_alert_test_button(env):
    srv, got = await _ntfy(env)
    try:
        br, client, _ = await env.make(ntfy_url=str(srv.make_url("/t")), alert_check_public=False)
        r = await client.post("/api/alerts/test")
        assert r.status == 200 and got[0]["title"] == "Yoto bridge test"
    finally:
        await srv.close()
    br2, client2, _ = await env.make()
    r = await client2.post("/api/alerts/test")
    assert r.status == 400


# ---------------------------------------------------------------- chapter icons

import struct  # noqa: E402
import zlib  # noqa: E402

from yotobridge import icons  # noqa: E402


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 4 + 1) + 1:(y + 1) * (w * 4 + 1)] for y in range(h)]
    return w, h, b"".join(rows)


def changed_pixels(icon_png: bytes, cover: bytes) -> set:
    """Pixels of the icon that differ from the cover: where the number was drawn."""
    _, _, px = decode_png(icon_png)
    return {(i // 4 % 16, i // 64) for i in range(0, len(px), 4) if px[i:i + 3] != cover[i:i + 3]}


def test_icon_draws_tiny_chapter_number_top_right():
    dark = bytes([40, 30, 60, 255]) * 256
    w, h, px = decode_png(icons.render(dark, 7))
    assert (w, h) == (16, 16)
    drawn = changed_pixels(icons.render(dark, 7), dark)
    # "7" is 3x4 in the top-right corner: top bar on row 0, columns 13-15; nothing else touched
    assert {(13, 0), (14, 0), (15, 0)} <= drawn and len(drawn) == 6
    assert max(y for _, y in drawn) == 3
    i = (0 * 16 + 13) * 4
    assert px[i] > 150 and px[i] < 235                        # light ink on a dark cover, blended in
    light = bytes([230, 230, 220, 255]) * 256
    _, _, lp = decode_png(icons.render(light, 7))
    assert lp[i] < 100                                         # dark ink on a light cover
    assert min(x for x, _ in changed_pixels(icons.render(dark, 123), dark)) == 5   # 3 digits: 11 px wide
    assert decode_png(icons.render(dark, None))[2] == dark     # no number: the cover as is


def white_pixels(rgba: bytes) -> set:
    """Pixels that differ from a plain fallback-colour icon (for icons rendered without a cover)."""
    base = bytes([*icons.FALLBACK_BG, 255])
    return {(i // 4 % 16, i // 64) for i in range(0, len(rgba), 4) if rgba[i:i + 4] != base}


def test_chapter_number():
    ch = [{"start": 0}, {"start": 20}, {"start": 40}]
    assert [icons.chapter_number(ch, t) for t in (0, 19, 20, 59)] == [1, 1, 2, 3]
    assert icons.chapter_number([{"start": 0}], 30) is None   # one chapter = no chapters


async def test_shrink_cover_with_ffmpeg():
    jpg = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x3366cc:s=300x200",
                          "-frames:v", "1", "-f", "mjpeg", "pipe:1"], capture_output=True, check=True).stdout
    px = await icons.shrink_cover(jpg)
    assert len(px) == 16 * 16 * 4
    r, g, b = px[0], px[1], px[2]
    assert abs(r - 0x33) < 12 and abs(g - 0x66) < 12 and abs(b - 0xCC) < 12
    assert await icons.shrink_cover(b"not an image") is None


async def test_stream_card_playlist_points_at_live_chapter_icon(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    r = await client.post("/api/cards", json={"item_id": "li1"})
    slot = (await r.json())["slot"]
    track = env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]
    assert track["display"]["iconUrl16x16"] == f"http://bridge.test:8080/s/icon/{slot}.png"
    r = await client.get(f"/s/icon/{slot}.png")
    assert r.status == 200 and r.headers["Content-Type"] == "image/png"
    _, _, px = decode_png(await r.read())
    assert white_pixels(px) == white_pixels(decode_png(icons.render(None, 2))[2])    # 25 s = chapter 2
    assert (await client.get("/s/icon/nope.png")).status == 404


async def test_existing_cards_get_the_icon_on_startup(env):
    br, client, _ = await env.make()
    s = card(br)
    s.stream_url = br.stream_url(s.slot)        # an older card: right stream URL, no icon yet
    await br.refresh_playlist_urls()
    assert env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]["display"]["iconUrl16x16"].endswith(f"/s/icon/{s.slot}.png")


async def test_offline_tracks_get_uploaded_chapter_icons(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline")
    await _wait_offline(client, s.slot)
    chapters = env.yoto.posts[-1]["content"]["chapters"]
    refs = [c["tracks"][0]["display"]["icon16x16"] for c in chapters]
    assert refs == ["yoto:#icon1", "yoto:#icon1", "yoto:#icon2", "yoto:#icon2"]   # ch 2, 2, 3, 3: one upload each
    assert len(env.yoto.icons) == 2 and decode_png(env.yoto.icons[0])[:2] == (16, 16)
    assert chapters[0]["display"]["icon16x16"] == "yoto:#icon1"


async def test_offline_copy_still_works_without_icon_permission(env):
    br, client, _ = await env.make(offline_track_seconds=15)
    env.yoto.icon_status = 403
    s = card(br)
    await client.post(f"/api/cards/{s.slot}/offline")
    info = await _wait_offline(client, s.slot)
    assert info["status"] == "ready"
    assert "display" not in env.yoto.posts[-1]["content"]["chapters"][0]["tracks"][0]
    assert br.icon_upload_error


# ---------------- warm starts ----------------

async def test_warm_start_serves_the_same_audio_without_waiting_for_ffmpeg(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    await br.pm.prepare(s)
    w = br.pm.warm[s.slot]
    assert w.start == 20.0 and len(w.audio) == 30 * BYTERATE
    real = type(br.encoder).stream
    calls = []

    def spy(enc, tracks, start, end, auth, speed=1.0):
        calls.append(start)
        return real(enc, tracks, start, end, auth, speed)
    br.encoder.stream = spy.__get__(br.encoder)
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == 40 * BYTERATE
    body = await r.read()
    assert len(body) == 40 * BYTERATE
    assert body[:30 * BYTERATE] == w.audio
    assert calls and min(calls) >= 50.0 - 0.01      # ffmpeg only for the part after the warm start
    assert abs(tone_at(body) - 880) < 40             # 20 s in = part 2
    assert abs(tone_at(body, at=32) - 1320) < 40     # 52 s in = part 3, after the join
    assert ("play", "li1", f"yoto-{s.slot}") in env.abs.calls   # ABS session still opened


async def test_warm_start_ignored_when_the_phone_moved_the_book(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    await br.pm.prepare(s)
    env.abs.progress["li1"] = {"currentTime": 45.0, "duration": 60.0}
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE
    await r.read()
    assert not br.pm.by_slot[s.slot].warm


async def test_warm_start_short_range_probe(env):
    br, client, _ = await env.make()
    s = card(br)
    await br.pm.prepare(s)
    r = await client.get(f"/s/{s.slot}.mp3", headers={"Range": "bytes=0-999"})
    assert r.status == 206 and len(await r.read()) == 1000


async def test_no_warm_start_near_the_end_or_when_off(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 48.0, "duration": 60.0}
    s = card(br)
    await br.pm.prepare(s)
    assert s.slot not in br.pm.warm                   # 43 s + 30 s would run past the end
    br2, _, _ = await env.make(warm_start_seconds=0)
    s2 = card(br2, yoto_card_id="yc2")
    await br2.pm.prepare(s2)
    assert not br2.pm.warm


async def test_warm_start_refreshed_after_listening(env):
    br, client, _ = await env.make()
    s = card(br)
    await br.pm.prepare(s)
    assert br.pm.warm[s.slot].start == 0.0
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    pb.main_started -= 30
    br.pm.on_bytes(pb, 40 * BYTERATE, True)
    br.pm.close_stream(pb)
    await br.pm.finish_slot(s.slot, "test")
    await br.pm._prepare_later(s.slot, delay=0)
    assert br.pm.warm[s.slot].start == env.abs.progress["li1"]["currentTime"] - 5


async def test_non_jpeg_cover_is_converted_for_yoto():
    # ABS often serves WebP covers, which Yoto's cover upload refuses; any format ffmpeg reads is
    # converted (BMP here, since not every ffmpeg build can encode WebP for the test)
    bmp = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x3366cc:s=60x90",
                          "-frames:v", "1", "-c:v", "bmp", "-f", "image2pipe", "pipe:1"], capture_output=True, check=True).stdout
    assert bmp[:2] == b"BM"
    jpg = await icons.to_jpeg(bmp)
    assert jpg and jpg[:3] == b"\xff\xd8\xff"
    png_bytes = icons.render(None, None)
    assert await icons.to_jpeg(png_bytes) == png_bytes          # PNG/JPEG/GIF go through untouched
    assert await icons.to_jpeg(b"junk") is None


async def test_book_picker_does_not_refetch_collections_each_time(env):
    br, client, _ = await env.make()
    for _ in range(3):
        info = await (await client.get("/api/book/li1")).json()
        assert [c["id"] for c in info["collections"]] == ["col1"]
    assert env.abs.calls.count(("collections",)) == 1


async def test_yoto_buffering_ahead_keeps_the_session_open(env):
    """Real Yoto behaviour: it downloads ~12 minutes in seconds and closes the connection, then plays
    from its buffer. The session stays open and the position keeps moving with the clock."""
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 0.0, "duration": 60.0}
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    br.pm.on_bytes(pb, 50 * BYTERATE, True)          # 50 s of audio delivered...
    br.pm.close_stream(pb)
    pb.main_started -= 30                            # ...and 30 s of wall clock since the stream started
    pb.ended -= 25                                   # connection closed 25 s ago
    pb.duration = 600.0                              # a long book, so the end of the book is not the cap
    await br.pm.tick()
    assert s.slot in br.pm.by_slot                   # still playing from the buffer: not closed
    assert br.pm.position(pb) == pytest.approx(10, abs=1)   # 30 s heard minus the 20 s buffer allowance
    pb.main_started -= 100                           # 130 s since start: the 50 s buffer ran out long ago
    pb.ended -= 100
    await br.pm.tick()
    assert s.slot not in br.pm.by_slot


async def test_stopped_event_closes_even_with_audio_buffered(env):
    br, client, _ = await env.make()
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    br.pm.on_bytes(pb, 50 * BYTERATE, True)
    br.pm.close_stream(pb)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 12, "playbackStatus": "stopped"})
    pb.ended -= 60
    await br.pm.tick()
    assert s.slot not in br.pm.by_slot


# ---------------------------------------------------------------- chapters as tracks

def _tracks_of(post):
    return [c["tracks"][0] for c in post["content"]["chapters"]]


async def _chapter_card(env, client, progress=None, **kw):
    if progress is not None:
        env.abs.progress["li1"] = {"currentTime": progress, "duration": 60.0, "lastUpdate": 1000}
    r = await client.post("/api/cards", json={"item_id": "li1", **kw})
    assert r.status == 200, await r.text()
    return (await r.json())["slot"]


async def resolve(br, slot, lid, k):
    from yotobridge.app import resolve_track
    return await resolve_track(br, br.store.get(slot), lid, k)


def test_chapter_spans_and_parts():
    from yotobridge.chapters import chapter_spans, index_at
    spans = chapter_spans([{"start": 0, "title": "A"}, {"start": 100, "title": "B"}], 250, 1800)
    assert spans == [(0.0, 100.0, "A"), (100.0, 250, "B")]
    assert index_at(spans, 99) == 0 and index_at(spans, 100) == 1 and index_at(spans, 300) == 1
    parts = chapter_spans([], 4000, 1800)
    assert [(s, e, t) for s, e, t in parts] == [(0.0, 1800.0, "Part 1"), (1800.0, 3600.0, "Part 2"), (3600.0, 4000, "Part 3")]


def test_very_long_chapters_are_split_into_parts():
    from yotobridge.chapters import chapter_spans, make_layout
    ch = [{"start": 0, "title": "Side A"}, {"start": 10000, "title": "Side B"}]
    spans = chapter_spans(ch, 12000, 1800)
    assert [t for _, _, t in spans][:2] == ["Side A (1/6)", "Side A (2/6)"] and spans[6][2] == "Side B"   # 2000 s: kept whole
    assert all(e - s <= 1800 for s, e, _ in spans[:6])
    segs = make_layout([("b", "Book", 12000, ch, 5000)], 1800)
    assert segs[0].number == 1 and segs[-1].number == 2                     # chapter numbers, not track numbers


async def test_new_card_on_a_player_closes_the_old_session_and_seek_follows_it(env):
    """Seen on a real Yoto: card A's session (waiting out the player's buffer) was still open when
    card B started on the same player, and the phone's seek buttons then restarted card A."""
    br, client, control = await env.make(chapter_tracks=True)
    a = await _chapter_card(env, client, progress=25.0)
    b = (await (await client.post("/api/cards", json={"item_id": "li2"})).json())["slot"]
    la, lb = br.store.get(a).layout_id, br.store.get(b).layout_id
    await (await client.get(f"/s/{a}/{la}/1.mp3")).read()
    br.pm.on_event("dev-mini", {"cardId": br.store.get(a).yoto_card_id, "position": 1, "playbackStatus": "playing"})
    await (await client.get(f"/s/{b}/{lb}/1.mp3")).read()
    br.pm.on_event("dev-mini", {"cardId": br.store.get(b).yoto_card_id, "position": 1, "playbackStatus": "playing"})
    for _ in range(50):
        if a not in br.pm.by_slot:
            break
        await asyncio.sleep(0.02)
    assert a not in br.pm.by_slot and b in br.pm.by_slot
    assert br.pm.current_on("dev-mini").slot == b
    r = await client.post("/api/seek", json={"device_id": "dev-mini", "action": "fwd30"})
    assert r.status == 200 and (await r.json())["title"] == "Book li2"
    assert control.commands[-1] == ("play", "dev-mini", br.store.get(b).yoto_card_id, None)


async def test_unknown_track_list_falls_back_to_the_current_one(env):
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    r = await client.get(f"/s/{slot}/gone123/2.mp3")
    assert r.status == 200 and int(r.headers["Content-Length"]) == 20 * BYTERATE
    await r.read()
    assert (await client.get(f"/s/icon/{slot}/gone123/2.png")).status == 200


async def test_event_position_never_goes_before_the_stream_start(env):
    """Seen on a real Yoto: a session that resumed at 684 s was closed at 669 s."""
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 35.0, "duration": 60.0}
    s = card(br)
    pb, _ = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    assert pb.base == 30.0
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 40, "playbackStatus": "playing"})   # odd counter
    br.pm.on_event("dev-mini", {"position": 20, "playbackStatus": "paused"})
    assert br.pm.position(pb) >= 30.0


# ---------------------------------------------------------------- chapter tracks: stable numbering

async def _get(client, url):
    r = await client.get(url)
    body = await r.read()
    return r, body


async def test_chapter_tracks_number_chapters_from_one(env):
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    s = br.store.get(slot)
    tracks = _tracks_of(env.yoto.posts[-1])
    assert [t["title"] for t in tracks] == ["Chapter 1", "Chapter 2", "Chapter 3"]     # track N = chapter N
    assert tracks[1]["trackUrl"] == f"http://bridge.test:8080/s/{slot}/{s.layout_id}/2.mp3"
    assert tracks[1]["display"]["iconUrl16x16"] == f"http://bridge.test:8080/s/icon/{slot}/{s.layout_id}/2.png"
    r = await client.get(f"/s/icon/{slot}/{s.layout_id}/2.png")
    assert white_pixels(decode_png(await r.read())[2]) == white_pixels(decode_png(icons.render(None, 2))[2])


async def test_tap_resumes_on_the_listeners_track_and_next_previous_play_whole_chapters(env):
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    r, mp3 = await _get(client, f"/s/{slot}/{lid}/2.mp3")                  # the Yoto remembers track 2 = chapter 2
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE and abs(tone_at(mp3, 1) - 880) < 40   # 20-40: resume
    r, mp3 = await _get(client, f"/s/{slot}/{lid}/3.mp3")                  # next
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE and abs(tone_at(mp3, 1) - 1320) < 40
    r, _ = await _get(client, f"/s/{slot}/{lid}/1.mp3")                    # previous twice: chapter 1, whole
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE and br.pm.by_slot[slot].base == 0.0
    assert [c[0] for c in env.abs.calls].count("play") == 1                # one ABS session throughout


async def test_retap_after_pulling_the_card_resumes_instead_of_restarting_the_chapter(env):
    """Seen on a real Yoto: pulled during chapter 5, re-tapped; the player asks for the same track again."""
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    card_id = br.store.get(slot).yoto_card_id
    await _get(client, f"/s/{slot}/{lid}/2.mp3")
    br.pm.on_event("dev-mini", {"cardId": card_id, "position": 3, "playbackStatus": "playing"})
    br.pm.position = lambda _pb: 33.0
    await br.pm.sync(br.pm.by_slot[slot], force=True)                        # ABS now at 33 s
    br.pm.on_event("dev-mini", {"cardId": "none", "playbackStatus": "stopped"})   # card out
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")                    # back in: same track
    assert br.pm.by_slot[slot].base == 28.0                                # 33 - 5 rewind, not the chapter start


async def test_resume_after_a_pause_carries_on_inside_the_track(env):
    """Seen on a real Yoto: a pause hangs up the stream; resume asks for the same track afresh (no Range)."""
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    card_id = br.store.get(slot).yoto_card_id
    await _get(client, f"/s/{slot}/{lid}/2.mp3")
    br.pm.position = lambda _pb: 31.0
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "paused"})
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "playing"})
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")
    # a short pause backs up 2 s
    assert br.pm.by_slot[slot].base == 29.0 and int(r.headers["Content-Length"]) == 11 * BYTERATE
    # a pause over 30 s backs up rewind_seconds (5 s)
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "paused"})
    br.pm.by_slot[slot].paused_at -= 45
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "playing"})
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")
    assert br.pm.by_slot[slot].base == 26.0 and int(r.headers["Content-Length"]) == 14 * BYTERATE
    # resumed before the player reports "playing": the pause so far counts
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "paused"})
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")
    assert br.pm.by_slot[slot].base == 29.0


async def test_card_out_and_back_in_resumes_where_it_was_taken_out(env):
    """Seen on a real Yoto: card out ("stopped", no card), back in; the player reports "playing" about
    5 s before it asks for the audio, and that wait is not listening."""
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    card_id = br.store.get(slot).yoto_card_id
    await _get(client, f"/s/{slot}/{lid}/2.mp3")
    clock = {"pos": 31.0}
    br.pm.position = lambda _pb: clock["pos"]
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "playing"})
    br.pm.on_event("dev-mini", {"cardId": "none", "playbackStatus": "stopped"})       # card out at 31 s
    br.pm.on_event("dev-mini", {"cardId": card_id, "playbackStatus": "playing"})       # back in
    clock["pos"] = 36.0                                                                # 5 s waiting for audio
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")
    assert br.pm.by_slot[slot].base == 29.0                                            # 31 s, 2 s back


async def test_tap_on_another_track_than_the_listeners_moves_the_player(env):
    """The phone moved the book on to chapter 3 while the Yoto remembers track 2."""
    br, client, control = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    env.abs.progress["li1"] = {"currentTime": 48.0, "duration": 60.0, "lastUpdate": 2000}
    br.pm.on_event("dev-mini", {"cardId": br.store.get(slot).yoto_card_id, "playbackStatus": "playing"})
    r, body = await _get(client, f"/s/{slot}/{lid}/2.mp3")
    assert r.headers["Content-Type"] == "audio/mpeg" and slot not in br.pm.by_slot   # silence, no session
    for _ in range(50):
        if control.tracks:
            break
        await asyncio.sleep(0.02)
    assert control.tracks[-1] == 3 and control.commands[-1][1] == "dev-mini"
    r, mp3 = await _get(client, f"/s/{slot}/{lid}/3.mp3")                  # the player obeys
    assert int(r.headers["Content-Length"]) == 17 * BYTERATE                # resumes at 43 s
    assert abs(tone_at(mp3, 1) - 1320) < 40


async def test_without_mqtt_a_wrong_track_plays_from_the_listeners_spot(env):
    br, client, control = await env.make(chapter_tracks=True)
    control.connected = False
    slot = await _chapter_card(env, client, progress=48.0)
    lid = br.store.get(slot).layout_id
    r, mp3 = await _get(client, f"/s/{slot}/{lid}/1.mp3")
    assert int(r.headers["Content-Length"]) == 17 * BYTERATE and abs(tone_at(mp3, 1) - 1320) < 40


async def test_track_list_kept_while_the_book_is_the_same(env):
    br, client, _ = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=5.0)
    first = br.store.get(slot).layout_id
    posts = len(env.yoto.posts)
    await _get(client, f"/s/{slot}/{first}/1.mp3")
    br.pm.position = lambda _pb: 30.0
    await br.pm.finish_slot(slot, "test")
    await asyncio.sleep(0.2)
    assert br.store.get(slot).layout_id == first and len(env.yoto.posts) == posts   # numbering unchanged


async def test_series_chapter_tracks_run_into_the_next_book(env):
    br, client, _ = await env.make(chapter_tracks=True)
    env.abs.progress["li1"] = {"currentTime": 45.0, "duration": 60.0, "lastUpdate": 1000}
    r = await client.post("/api/cards", json={"series_id": "ser1", "library_id": "lib1"})
    slot = (await r.json())["slot"]
    tracks = _tracks_of(env.yoto.posts[-1])
    assert [t["title"] for t in tracks][:4] == ["Book li1: Chapter 1", "Book li1: Chapter 2", "Book li1: Chapter 3",
                                                "Book li2: Chapter 1"]
    assert len(tracks) == 9
    lid = br.store.get(slot).layout_id
    r, _ = await _get(client, f"/s/{slot}/{lid}/3.mp3")
    assert br.pm.by_slot[slot].base == 40.0                                # 45 - 5
    br.pm.position = lambda _pb: 60.0                                      # played to the end of li1
    await _get(client, f"/s/{slot}/{lid}/4.mp3")                           # the player moves on to li2
    assert ("patch", "li1", 60.0, "finished") in env.abs.calls
    assert br.pm.by_slot[slot].item_id == "li2" and br.store.get(slot).item_id == "li2"


async def test_seek_from_the_phone_starts_the_right_track(env):
    br, client, control = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    await _get(client, f"/s/{slot}/{lid}/2.mp3")
    br.pm.by_slot[slot].device_id = "dev-mini"
    br.pm.position = lambda _pb: 22.0
    posts = len(env.yoto.posts)
    r = await client.post("/api/seek", json={"device_id": "dev-mini", "action": "next"})
    assert r.status == 200 and (await r.json())["position"] == 40.0
    assert env.abs.progress["li1"]["currentTime"] == 40.0 and control.tracks[-1] == 3
    assert len(env.yoto.posts) == posts                                    # same book: track list untouched
    r, _ = await _get(client, f"/s/{slot}/{lid}/3.mp3")
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE                # exactly 40 s, no rewind
    assert (await client.post("/api/seek", json={"device_id": "dev-player", "action": "back30"})).status == 409


async def test_play_on_yoto_starts_the_right_track_and_survives_the_cached_list(env):
    br, client, control = await env.make(chapter_tracks=True)
    await br.connect_yoto()
    env.abs.progress["li2"] = {"currentTime": 25.0, "duration": 60.0, "lastUpdate": 1000}
    await client.post("/api/play", json={"device_id": "dev-mini", "item_id": "li1"})
    remote = br.store.remote_for("dev-mini")
    old = remote.layout_id
    await client.post("/api/play", json={"device_id": "dev-mini", "item_id": "li2"})
    assert remote.layout_id != old and control.tracks[-1] == 2              # li2 is in chapter 2
    r, mp3 = await _get(client, f"/s/{remote.slot}/{old}/2.mp3")           # the player's cached (li1) list
    assert br.pm.by_slot[remote.slot].item_id == "li2"
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE                # li2 from 20 s (25 - 5) to 40 s


async def test_head_probe_does_not_start_the_move_and_the_moved_to_track_resumes(env):
    """Seen on a real Yoto: it HEADs a track before GETting it; the move was started on the HEAD,
    then the GET played under the wrong track and the moved-to track started at the chapter start."""
    br, client, control = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=30.0)                  # listener in chapter 2, at 30 s
    lid = br.store.get(slot).layout_id
    br.pm.on_event("dev-mini", {"cardId": br.store.get(slot).yoto_card_id, "playbackStatus": "playing"})
    r = await client.head(f"/s/{slot}/{lid}/3.mp3")                         # the player remembers track 3
    assert r.status == 200
    await asyncio.sleep(0.1)
    assert not control.tracks                                               # a HEAD sends no move
    r, _ = await _get(client, f"/s/{slot}/{lid}/3.mp3")
    assert slot not in br.pm.by_slot                                        # silence while moving
    for _ in range(50):
        if control.tracks:
            break
        await asyncio.sleep(0.02)
    assert control.tracks == [2]
    r, _ = await _get(client, f"/s/{slot}/{lid}/3.mp3")                     # a retry of the old track: still silence
    assert slot not in br.pm.by_slot and control.tracks == [2]
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")                     # the player obeys
    assert br.pm.by_slot[slot].base == 25.0                                 # resumes at 30 - 5, not the chapter start
    assert int(r.headers["Content-Length"]) == 15 * BYTERATE


def test_books_with_more_than_99_chapters_get_a_window_that_moves_in_steps():
    from yotobridge.chapters import make_layout
    ch = [{"start": i * 60, "title": f"Ch {i + 1}"} for i in range(250)]
    def first_number(pos):
        return make_layout([("b", "Big", 250 * 60, ch, pos)], 1800)[0].number
    assert first_number(10 * 60) == 1 and first_number(89 * 60) == 1       # chapters 1-99 while in 1-90
    assert first_number(90 * 60) == 91 and first_number(150 * 60) == 91    # then 91-189
    assert first_number(240 * 60) == 152                                   # the last window ends at chapter 250
    assert len(make_layout([("b", "Big", 250 * 60, ch, 0)], 1800)) == 99


async def test_library_lists_recently_added_first(env):
    br, client, _ = await env.make()
    r = await client.get("/api/books?library=lib1")
    assert r.status == 200
    assert ("items", "addedAt", "1") in env.abs.calls


# ---------------------------------------------------------------- playback speed

async def test_card_speed_encodes_faster_and_maps_bytes_to_book_seconds(env):
    """At 2x, the 40 s from 20 s to the end of the book are 20 s of MP3, still from the right place."""
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    r = await client.put(f"/api/cards/{s.slot}/speed", json={"speed": 2})
    assert r.status == 200 and br.store.get(s.slot).speed == 2.0
    r = await client.get(f"/s/{s.slot}.mp3")
    assert int(r.headers["Content-Length"]) == 20 * BYTERATE
    body = await r.read()
    assert abs(duration_of(body) - 20) < 1.0
    assert abs(tone_at(body) - 880) < 60              # book 20 s = part 2, pitch unchanged
    assert abs(tone_at(body, at=11) - 1320) < 60      # 11 s of audio = book 42 s = part 3
    pb = br.pm.by_slot[s.slot]
    assert pb.speed == 2.0
    r = await client.get(f"/s/{s.slot}.mp3", headers={"Range": f"bytes={10 * BYTERATE}-"})
    assert r.headers["Content-Range"] == f"bytes {10 * BYTERATE}-{20 * BYTERATE - 1}/{20 * BYTERATE}"
    assert abs(tone_at(await r.read()) - 1320) < 60   # 10 s of audio in = book 40 s


async def test_speed_scales_positions_and_listening_time(env):
    br, client, _ = await env.make()
    br.store.state.speeds["dev-mini"] = 1.5
    s = card(br)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "playbackStatus": "playing"})
    pb, start = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    assert pb.speed == 1.5 and start == 0.0
    pb.main_started -= 10                             # 10 s of listening
    pb.last_sync -= 10
    br.pm.on_bytes(pb, 20 * BYTERATE, True)           # 20 s of MP3 = 30 s of book delivered
    assert br.pm.position(pb) <= 15.01                # wall clock: 10 s at 1.5x
    br.pm.on_event("dev-mini", {"cardId": "yc1", "position": 8, "playbackStatus": "paused"})
    await asyncio.sleep(0.05)
    syncs = [c for c in env.abs.calls if c[0] == "sync"]
    assert syncs and syncs[-1][2] == pytest.approx(12.0, abs=0.3)   # 8 s of audio = 12 s of book
    assert syncs[-1][3] == pytest.approx(8.0, abs=0.3)              # listened: real time


async def test_card_speed_beats_player_speed_and_unknown_player_uses_shared_speed(env):
    br, _, _ = await env.make()
    br.yoto_devices = [{"deviceId": "dev-mini"}, {"deviceId": "dev-player"}]
    s = card(br)
    assert br.pm.speed_for(s, "dev-mini") == 1.0
    br.store.state.speeds.update({"dev-mini": 1.25, "dev-player": 1.25})
    assert br.pm.speed_for(s) == 1.25 and not br.pm.speeds_differ(s)
    br.store.state.speeds["dev-player"] = 0.75
    assert br.pm.speed_for(s) == 1.0 and br.pm.speeds_differ(s)
    assert br.pm.speed_for(s, "dev-player") == 0.75
    s.speed = 2.0
    assert br.pm.speed_for(s, "dev-player") == 2.0 and not br.pm.speeds_differ(s)


async def test_warm_start_only_used_at_its_own_speed(env):
    br, client, _ = await env.make()
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    await br.pm.prepare(s)
    assert br.pm.warm[s.slot].speed == 1.0
    s.speed = 2.0
    r = await client.get(f"/s/{s.slot}.mp3")
    await r.read()
    assert br.pm.by_slot[s.slot].warm == b""          # 1x audio never spliced into a 2x stream
    await br.pm.finish_slot(s.slot, "test")
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    await br.pm.prepare(s)
    w = br.pm.warm[s.slot]
    assert w.speed == 2.0 and len(w.audio) == 15 * BYTERATE   # 30 s of book at 2x


async def test_changing_player_speed_restarts_what_it_plays_in_place(env):
    br, client, control = await env.make(chapter_tracks=True)
    slot = await _chapter_card(env, client, progress=25.0)
    lid = br.store.get(slot).layout_id
    await _get(client, f"/s/{slot}/{lid}/2.mp3")
    br.pm.by_slot[slot].device_id = "dev-mini"
    br.pm.position = lambda _pb: 31.0
    r = await client.post("/api/speed", json={"device_id": "dev-mini", "speed": 1.5})
    assert r.status == 200 and br.store.state.speeds == {"dev-mini": 1.5}
    assert env.abs.progress["li1"]["currentTime"] == 31.0 and control.tracks[-1] == 2
    assert (await client.head(f"/s/{slot}/{lid}/2.mp3")).status == 200   # the player probes first
    r, _ = await _get(client, f"/s/{slot}/{lid}/2.mp3")
    assert br.pm.by_slot[slot].speed == 1.5
    assert int(r.headers["Content-Length"]) == int(9 * BYTERATE / 1.5)      # 31 s to 40 s, no rewind, at 1.5x
    assert (await client.post("/api/speed", json={"device_id": "dev-mini", "speed": 5})).status == 400
    assert 1.5 in (await (await client.get("/api/status")).json())["speeds"]


async def test_player_speed_back_to_normal_and_card_follows_player_again(env):
    br, client, _ = await env.make()
    s = card(br)
    await client.post("/api/speed", json={"device_id": "dev-mini", "speed": 1.25})
    await client.post("/api/speed", json={"device_id": "dev-mini", "speed": 1})
    assert br.store.state.speeds == {}
    await client.put(f"/api/cards/{s.slot}/speed", json={"speed": 1.5})
    await client.put(f"/api/cards/{s.slot}/speed", json={"speed": 0})
    assert br.store.get(s.slot).speed == 0.0
    cards = await (await client.get("/api/cards")).json()
    assert cards[0]["speed"] == 0.0


async def test_position_counts_only_time_spent_playing(env):
    """The Yoto sends no positions while streaming, so position is the time it has played: at 2x,
    10 s of playing (after the start-up allowance) is 20 s of book, and time paused does not count.
    Switching speed while paused must restart from there, not from where the 2x stream began."""
    br, client, control = await env.make()
    br.store.state.speeds["dev-mini"] = 2.0
    s = card(br)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "playbackStatus": "playing"})
    pb, start = await br.pm.open_stream(br.store.get(s.slot), 0, None)
    assert pb.speed == 2.0 and start == 0.0
    br.pm.on_bytes(pb, 30 * BYTERATE, True)           # 60 s of book delivered ahead
    pb.main_started -= 13                             # 13 s since the request: ~10 s heard
    assert br.pm.position(pb) == pytest.approx(20.0, abs=0.3)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "playbackStatus": "paused"})
    pb.paused_at -= 30                                # paused 30 s ago...
    pb.main_started -= 30                             # ...right after those 13 s
    assert br.pm.position(pb) == pytest.approx(20.0, abs=0.3)
    br.pm.on_event("dev-mini", {"cardId": "yc1", "playbackStatus": "playing"})
    pb.main_started -= 2
    assert br.pm.position(pb) == pytest.approx(24.0, abs=0.3)
    control.connected = False                         # no pause events: the whole buffer is uncertain
    assert br.pm.position(pb) == pytest.approx(max(0.0, 24.0 + 2 * 3 - 2 * 20), abs=0.3)
