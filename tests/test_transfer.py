"""Moving an old MYO card (uploaded audio) onto ABS without losing the listener's place."""
import json

from yotobridge import transfer


def book(item_id, title, files, chapters, series_id, series_name, seq):
    """files: lengths of the audio files; chapters: chapter start times (end = book end)."""
    dur = float(sum(files))
    starts = [sum(files[:i]) for i in range(len(files))]
    ends = chapters[1:] + [dur]
    return {"id": item_id, "libraryId": "lib1", "mediaType": "book", "media": {
        "duration": dur,
        "metadata": {"title": title, "authorName": "A", "series": [{"id": series_id, "name": series_name, "sequence": seq}]},
        "tracks": [{"index": i + 1, "startOffset": float(s), "duration": float(f), "contentUrl": f"/api/items/{item_id}/file/0"}
                   for i, (s, f) in enumerate(zip(starts, files))],
        "chapters": [{"id": i, "start": float(s), "end": float(e), "title": f"Chapter {i + 1}"}
                     for i, (s, e) in enumerate(zip(chapters, ends))]}}


P1 = book("p1", "The Larkspurs", [1000, 1000, 1000], [0, 500, 1000, 1500, 2000, 2500], "sp", "The Larkspurs", "1")
P2 = book("p2", "The Larkspurs on Gardam Street", [2400], [0, 300, 600, 900, 1200, 1500, 1800, 2100], "sp", "The Larkspurs", "2")
V1 = book("v1", "The Hollowbrooks of Elm Street", [3600], [0, 400, 1000, 1700, 2500], "sv", "The Hollowbrooks", "1")
V4 = book("v4", "The Hollowbrooks Lost and Found", [2600, 2400], [0, 350, 900, 1600, 2300, 3100, 3900, 4400], "sv",
          "The Hollowbrooks", "4")


def old_card(durations):
    return {"cardId": "old1", "title": "Larkspurs and Hollowbrooks", "content": {"chapters": [
        {"key": f"{i:02d}", "title": f"Track {i}", "tracks": [{"key": "01", "title": f"Track {i}", "type": "audio",
                                                               "trackUrl": f"yoto:#sha{i}", "duration": d}]}
        for i, d in enumerate(durations, 1)]}}


# P1 as its three files (Yoto rounding), P2 as one track, V1 one track per chapter, then the first
# chapters of V4, one of them a few seconds short (trimmed when it was uploaded)
CARD = [1001, 999, 1000, 2400, 400, 600, 700, 800, 1101, 350, 545, 700, 700]


def setup(env):
    env.abs.extra.update({b["id"]: b for b in (P1, P2, V1, V4)})
    env.yoto.content["old1"] = old_card(CARD)


async def test_old_card_tracks_are_matched_to_books(env):
    br, client, _ = await env.make()
    setup(env)
    r = await client.get("/api/transfer/old1")
    assert r.status == 200
    a = await r.json()
    runs = [(b["item_id"], b["first_track"], b["last_track"], b["whole"]) for b in a["books"]]
    assert runs == [("p1", 1, 3, True), ("p2", 4, 4, True), ("v1", 5, 9, True), ("v4", 10, 13, False)]
    t = {x["n"]: x for x in a["tracks"]}
    assert (t[3]["item_id"], t[3]["start"], t[3]["exact"]) == ("p1", 2000, True)
    assert (t[11]["item_id"], t[11]["start"], t[11]["exact"]) == ("v4", 350, False)   # the short one: estimated
    assert (t[12]["start"], t[12]["exact"]) == (900, True)                              # back on a boundary
    assert a["player"] is None and a["place"] is None
    assert ("search", "larkspur") in env.abs.calls and ("search", "hollowbrook") in env.abs.calls


async def test_player_reading_gives_the_place_and_apply_moves_the_card(env, tmp_path):
    br, client, _ = await env.make()
    setup(env)
    await br.connect_yoto()
    env.abs.progress["v4"] = {"currentTime": 72.0, "duration": 5000.0, "lastUpdate": 1}   # a stray old position
    # the old card goes into the Mini: track 12 (V4 from 900 s), 200 s in
    br.on_yoto_event("dev-mini", {"cardId": "old1", "chapterKey": "12", "trackKey": "01", "position": 200,
                                  "playbackStatus": "playing"})
    br.on_yoto_event("dev-mini", {"position": 205, "playbackStatus": "paused"})
    a = await (await client.get("/api/transfer/old1")).json()
    assert a["player"]["name"] == "Mini" and a["player"]["track"] == 12 and a["player"]["position"] == 205
    place = a["place"]
    assert (place["item_id"], place["position"], place["chapter"], place["exact"]) == ("v4", 1105, "Chapter 3", True)
    assert [f["item_id"] for f in place["finish"]] == ["p1", "p2", "v1"]
    assert [s["id"] for s in place["series"]] == ["sv"]

    posts_before = len(env.yoto.posts)
    old = json.dumps(env.yoto.content["old1"], sort_keys=True)
    r = await client.post("/api/transfer/old1/apply", json={"item_id": "v4", "position": 1105, "device_id": "dev-mini",
                                                             "finish": ["p1", "p2", "v1"], "series_id": "sv"})
    assert r.status == 200, await r.text()
    patches = [c for c in env.abs.calls if c[0] == "patch"]
    assert patches == [("patch", "p1", 3000.0, "finished"), ("patch", "p2", 2400.0, "finished"),
                       ("patch", "v1", 3600.0, "finished"), ("patch", "v4", 1105.0)]   # current book written last
    res = await r.json()
    slot = br.store.get(res["slot"])
    assert res["existing"] is False and res["yoto_title"] == "The Hollowbrooks"
    assert (slot.series_id, slot.series_name, slot.item_id) == ("sv", "The Hollowbrooks", "v4")
    assert slot.yoto_card_id and slot.yoto_card_id != "old1"          # a new playlist...
    assert json.dumps(env.yoto.content["old1"], sort_keys=True) == old  # ...and the old one untouched
    assert all(p.get("cardId") != "old1" for p in env.yoto.posts[posts_before:])
    # moving again (or another old card of the same series) reuses that card
    r = await client.post("/api/transfer/old1/apply", json={"item_id": "v4", "position": 1105, "series_id": "sv"})
    assert (await r.json())["slot"] == slot.slot and len(br.store.cards()) == 1


async def test_place_chosen_by_hand(env):
    br, client, _ = await env.make()
    setup(env)
    p = await (await client.get("/api/transfer/old1/where?track=5&position=90")).json()
    assert (p["item_id"], p["position"], [f["item_id"] for f in p["finish"]]) == ("v1", 90, ["p1", "p2"])
    assert (await client.get("/api/transfer/old1/where?track=99&position=0")).status == 400


async def test_a_new_card_does_not_inherit_the_previous_cards_track(env):
    br, client, _ = await env.make()
    setup(env)
    br.on_yoto_event("dev-mini", {"cardId": "other", "chapterKey": "05", "trackKey": "01", "position": 30})
    br.on_yoto_event("dev-mini", {"cardId": "old1", "playbackStatus": "playing"})     # keys still the other card's
    br.on_yoto_event("dev-mini", {"position": 31})
    assert "old1" not in br.transfer.seen
    br.on_yoto_event("dev-mini", {"chapterKey": "02", "trackKey": "01", "position": 3})
    assert br.transfer.seen["old1"]["chapterKey"] == "02"


def test_one_track_per_whole_book_and_unknown_tracks():
    books = [transfer.Book.from_item(b) for b in (P1, P2)]
    tracks = [{"n": 1, "title": "x", "duration": 3000.0}, {"n": 2, "title": "song", "duration": 123.0},
              {"n": 3, "title": "y", "duration": 2400.0}]
    path = transfer.match(tracks, books)
    assert [(books[p.book].item_id if p.book >= 0 else None, p.start) for p in path] == [("p1", 0), (None, 0), ("p2", 0)]
