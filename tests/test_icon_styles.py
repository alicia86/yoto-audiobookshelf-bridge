"""Pixel icon styles: per card, with a default for every other card (the card's own style wins)."""
import subprocess

from yotobridge import icons
from yotobridge.icon_themes import IconLibrary, words

from test_bridge import card, changed_pixels, decode_png

DARK = bytes([40, 30, 60, 255]) * 256


def test_number_goes_in_the_chosen_corner():
    for style, (xs, ys) in {"cover-tr": (range(13, 16), range(0, 4)), "cover-tl": (range(0, 3), range(0, 4)),
                            "cover-bl": (range(0, 3), range(12, 16)), "cover-br": (range(13, 16), range(12, 16))}.items():
        drawn = changed_pixels(icons.render_style(style, DARK, 7), DARK)
        assert drawn and all(x in xs and y in ys for x, y in drawn), style


def test_cover_only_and_progress_bar():
    assert decode_png(icons.render_style("cover", DARK, 7))[2] == DARK
    _, _, px = decode_png(icons.render_style("progress", DARK, 3, fraction=0.5))
    bottom = [px[(15 * 16 + x) * 4] for x in range(16)]
    assert bottom[:8] == [250] * 8 and bottom[8:] == [30] * 8          # half way through the book
    assert decode_png(icons.render_style("progress", DARK, 3, fraction=0.5))[2][:15 * 64] == DARK[:15 * 64]


def test_big_number_and_number_only():
    _, _, px = decode_png(icons.render_style("cover-big", DARK, 12))
    white = {(i // 4 % 16, i // 64) for i in range(0, len(px), 4) if px[i] == 240}
    assert min(x for x, _ in white) == 1 and max(x for x, _ in white) == 14    # two doubled digits, centred
    assert px[0] == 40 * 40 // 100                                             # the cover dimmed
    _, _, px = decode_png(icons.render_style("number", bytes([200, 100, 0, 255]) * 256, 5))
    assert tuple(px[:3]) == (150, 75, 0)                                       # the cover's colour, mid-dark
    assert any(px[i] == 245 for i in range(0, len(px), 4))                     # light digits


def test_random_creature_is_the_same_per_chapter_and_differs_between_chapters():
    a = icons.render_style("random", DARK, 1, seed="li1:1")
    assert a == icons.render_style("random", DARK, 1, seed="li1:1")
    assert a != icons.render_style("random", DARK, 2, seed="li1:2")
    _, _, px = decode_png(a)
    for y in range(16):                                                        # mirrored left to right
        assert px[y * 64:y * 64 + 32] == b"".join(px[y * 64 + (15 - x) * 4:y * 64 + (16 - x) * 4] for x in range(8))


def test_theme_matching_is_kid_safe_unless_allowed(tmp_path):
    lib = IconLibrary(None, str(tmp_path))
    lib.icons = [{"mediaId": "a", "title": "Dragon", "publicTags": ["fantasy"], "url": "u"},
                 {"mediaId": "b", "title": "Sword", "publicTags": ["knight"], "url": "u"},
                 {"mediaId": "c", "title": "Castle", "publicTags": ["knight", "dragon"], "url": "u"}]
    lib._index()
    assert words("The Dragons' Caves") == ["dragon", "cave"]
    assert lib.match("Here Be Dragons")["mediaId"] == "a"        # a title word beats a tag word
    assert lib.match("The Knight's Sword")["mediaId"] == "c"     # the sword is never picked...
    assert lib.match("The Knight's Sword", allow_all=True)["mediaId"] == "b"   # ...unless allowed
    assert lib.match("Chapter 7") is None


async def test_card_style_rewrites_its_playlist_and_default_applies_to_the_rest(env):
    br, client, _ = await env.make(chapter_tracks=True)
    r = await client.post("/api/cards", json={"item_id": "li1"})
    a = (await r.json())["slot"]
    r = await client.post("/api/cards", json={"item_id": "li2"})
    b = (await r.json())["slot"]

    def track_icons(slot):
        cid = br.store.get(slot).yoto_card_id
        return [c["tracks"][0].get("display", {}).get("iconUrl16x16") for c in env.yoto.content[cid]["content"]["chapters"]]

    old = track_icons(a)
    assert old[0].startswith(f"http://bridge.test:8080/s/icon/{a}/")         # default style: URLs as before
    assert (await client.put(f"/api/cards/{a}/icon", json={"style": "number"})).status == 200
    new = track_icons(a)
    assert new != old and "/s/icon/" in new[0] and new[0].endswith("/1.png")
    r = await client.get(new[0][len("http://bridge.test:8080"):])
    assert r.status == 200 and decode_png(await r.read())[:2] == (16, 16)
    assert track_icons(b)[0].startswith(f"http://bridge.test:8080/s/icon/{b}/")   # the other card is untouched

    await client.post("/api/icons", json={"style": "none"})
    assert track_icons(b) == [None, None, None]                              # default: no icon at all
    assert track_icons(a) == new                                             # the card's own style wins
    cards = await (await client.get("/api/cards")).json()
    assert next(c for c in cards if c["slot"] == a)["icon_style"] == "number"
    st = await (await client.get("/api/status")).json()
    assert st["icons"]["style"] == "none" and any(s["id"] == "theme" for s in st["icons"]["styles"])

    await client.put(f"/api/cards/{a}/icon", json={"style": ""})            # back to the default
    assert track_icons(a) == [None, None, None]
    assert (await client.put(f"/api/cards/{a}/icon", json={"style": "bogus"})).status == 400
    assert (await client.put(f"/api/cards/{a}/icon", json={"style": "custom"})).status == 400   # no picture yet


async def test_custom_picture(env):
    br, client, _ = await env.make(chapter_tracks=True)
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    jpg = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x33cc66:s=64x64",
                          "-frames:v", "1", "-f", "mjpeg", "pipe:1"], capture_output=True, check=True).stdout
    r = await client.put(f"/api/cards/{s}/icon/image", data=jpg)
    assert r.status == 200 and (await r.json())["icon_style"] == "custom"
    _, _, px = decode_png(await (await client.get(f"/api/cards/{s}/icon.png")).read())
    assert abs(px[0] - 0x33) < 12 and abs(px[1] - 0xcc) < 12
    assert (await client.put(f"/api/cards/{s}/icon/image", data=b"nope")).status == 400
    await client.delete(f"/api/cards/{s}")
    assert not list((br.custom_icon_path(br.store.state.slots.get(s) or type("S", (), {"slot": s})).parent).glob("*"))


async def test_theme_icons_from_the_yoto_library(env):
    br, client, _ = await env.make(chapter_tracks=True)
    item = env.abs.item("li1")
    for ch, title in zip(item["media"]["chapters"], ["Here Be Dragons", "The Knight's Sword", "Chapter 3"]):
        ch["title"] = title
    env.abs.extra["li1"] = item
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    await client.put(f"/api/cards/{s}/icon", json={"style": "theme"})
    cid = br.store.get(s).yoto_card_id
    urls = [c["tracks"][0]["display"]["iconUrl16x16"][len("http://bridge.test:8080"):]
            for c in env.yoto.content[cid]["content"]["chapters"]]
    first = decode_png(await (await client.get(urls[0])).read())[2]
    assert tuple(first[:3]) == (200, 40, 40)                         # the dragon
    second = decode_png(await (await client.get(urls[1])).read())[2]
    assert tuple(second[:3]) == (120, 120, 200)                      # a knight's castle, not the sword
    third = decode_png(await (await client.get(urls[2])).read())[2]
    assert third == decode_png(await icons_default(br, "li1", 3))[2]   # no match: cover and number
    assert env.yoto.library_requests == 1                            # the library is fetched once

    await client.post("/api/icons", json={"allow_all": True})
    urls2 = [c["tracks"][0]["display"]["iconUrl16x16"] for c in env.yoto.content[cid]["content"]["chapters"]]
    assert urls2[0] != "http://bridge.test:8080" + urls[0]           # new URLs, so the Yoto refetches
    second = decode_png(await (await client.get(urls2[1][len("http://bridge.test:8080"):])).read())[2]
    assert tuple(second[:3]) == (180, 180, 190)                      # now the sword


async def icons_default(br, item_id, number):
    return icons.render(await br.cover16(item_id), number)


async def test_offline_copy_uses_the_card_style(env):
    from test_bridge import _wait_offline
    br, client, _ = await env.make(offline_track_seconds=15)
    item = env.abs.item("li1")
    item["media"]["chapters"][1]["title"] = "Here Be Dragons"
    env.abs.extra["li1"] = item
    env.abs.progress["li1"] = {"currentTime": 25.0, "duration": 60.0}
    s = card(br)
    s.icon_style = "theme"
    await client.post(f"/api/cards/{s.slot}/offline")
    await _wait_offline(client, s.slot)
    refs = [c["tracks"][0]["display"]["icon16x16"] for c in env.yoto.posts[-1]["content"]["chapters"]]
    assert refs[:2] == ["yoto:#m-dragon", "yoto:#m-dragon"]           # Yoto's own icon, no upload
    assert refs[2] == "yoto:#icon1" and len(env.yoto.icons) == 1      # chapter 3 has no theme: drawn and uploaded


def _jpg(colour):
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=64x64",
                           "-frames:v", "1", "-f", "mjpeg", "pipe:1"], capture_output=True, check=True).stdout


async def test_custom_picture_per_chapter(env):
    br, client, _ = await env.make(chapter_tracks=True)
    s = (await (await client.post("/api/cards", json={"item_id": "li1"})).json())["slot"]
    chapters = await (await client.get(f"/api/cards/{s}/icon/chapters")).json()
    assert [c["number"] for c in chapters] == [1, 2, 3] and not any(c["picture"] for c in chapters)

    r = await client.put(f"/api/cards/{s}/icon/chapters/li1/2", data=_jpg("0xcc3333"))
    assert r.status == 200 and (await r.json())["icon_style"] == "custom"
    cid = br.store.get(s).yoto_card_id
    urls = [c["tracks"][0]["display"]["iconUrl16x16"][len("http://bridge.test:8080"):]
            for c in env.yoto.content[cid]["content"]["chapters"]]
    px = [decode_png(await (await client.get(u)).read())[2] for u in urls]
    assert abs(px[1][0] - 0xcc) < 12 and px[1][1] < 70                        # chapter 2: its picture
    assert px[0] == decode_png(icons.render(await br.cover16("li1"), 1))[2]   # no picture: the default style

    await client.put(f"/api/cards/{s}/icon/image", data=_jpg("0x3333cc"))   # a card picture fills the gaps
    urls2 = [c["tracks"][0]["display"]["iconUrl16x16"][len("http://bridge.test:8080"):]
             for c in env.yoto.content[cid]["content"]["chapters"]]
    assert urls2 != urls                                                      # new pictures, new URLs
    px = [decode_png(await (await client.get(u)).read())[2] for u in urls2]
    assert px[0][2] > 180 and px[2][2] > 180 and px[1][0] > 180               # blue, red, blue

    assert (await client.get(f"/api/cards/{s}/icon/chapters/li1/2")).status == 200
    assert (await client.get(f"/api/cards/{s}/icon/chapters/other/2")).status == 404
    await client.delete(f"/api/cards/{s}/icon/chapters/li1/2")
    assert (await client.get(f"/api/cards/{s}/icon/chapters/li1/2")).status == 404
    assert not br.store.get(s).icon_chapters
