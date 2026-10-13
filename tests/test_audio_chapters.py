"""tools/audio_chapters.py: chapters from spoken "Chapter N" plus pauses, and the bridge endpoint that takes them."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import audio_chapters as ac  # noqa: E402
from test_chapter_finder import chapters_env, scan  # noqa: E402,F401  (fixture reused)


def words(*items):
    """(start, text) pairs -> whisper-like (start, end, word) tuples, 0.3 s per word."""
    return [(t, t + 0.3, w) for t, w in items]


def test_parse_number():
    assert ac.parse_number(["21"]) == (21, 1)
    assert ac.parse_number(["twenty-one"]) == (21, 1)
    assert ac.parse_number(["twenty", "one"]) == (21, 2)
    assert ac.parse_number(["seven", "the"]) == (7, 1)
    assert ac.parse_number(["xii"]) == (12, 1)
    assert ac.parse_number(["the", "first"]) == (1, 2)
    assert ac.parse_number(["of"]) == (None, 0)


def test_announcements_need_a_pause_and_rise():
    gaps = [(98.0, 100.0), (598.0, 600.0)]
    w = words((0.0, "Chapter"), (0.4, "One."), (50, "the"), (50.35, "next"), (50.7, "chapter"), (51.0, "three"),
              (101.0, "Chapter"), (101.4, "two"), (601.0, "Chapter"), (601.4, "Three"))
    found = ac.find_announcements(w, gaps)
    assert [n for _, n in found] == [1, 2, 3]          # "the next chapter three" mid-sentence is ignored


def test_build_chapters_snaps_and_fills_missing():
    gaps = [(98.0, 100.0), (300.0, 304.0), (310.0, 311.0), (598.0, 600.0)]
    found = [(0.0, 1), (102.0, 2), (601.0, 4)]          # chapter 3 never said
    r = ac.build_chapters(found, gaps, 900.0)
    assert [(c["title"], c["start"]) for c in r["chapters"]] == [
        ("Chapter 1", 0.0), ("Chapter 2", 99.8), ("Chapter 3", 303.8), ("Chapter 4", 599.8)]
    assert r["quality"] == "check" and "Chapter 3 was not announced" in r["warnings"][0]


def test_build_chapters_good_and_opening():
    gaps = [(40.0, 45.0), (498.0, 500.0)]
    r = ac.build_chapters([(50.0, 1), (505.0, 2)], gaps, 900.0, expect=2)
    assert [c["title"] for c in r["chapters"]] == ["Opening", "Chapter 1", "Chapter 2"]
    assert r["quality"] == "good"


def test_no_announcements_falls_back_to_pauses():
    gaps = [(600.0, 604.0), (700.0, 701.0), (1200.0, 1203.0), (1205.0, 1209.0)]
    r = ac.build_chapters([], gaps, 2000.0, expect=3)
    assert [c["start"] for c in r["chapters"]] == [0.0, 603.8, 1208.8]   # 1203 is too close to 1209
    assert r["quality"] == "check" and "No spoken" in r["warnings"][0]


def test_silences_found_in_real_audio(tmp_path):
    f = tmp_path / "a.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-filter_complex", "[1]atrim=0:2[s];[0][s][2]concat=n=3:v=0:a=1", str(f)], check=True)
    gaps = ac.silences(f, 100.0, -35, 0.5)
    assert len(gaps) == 1 and abs(gaps[0][0] - 103.0) < 0.2 and abs(gaps[0][1] - 105.0) < 0.2


async def test_bridge_takes_audio_proposal(chapters_env):  # noqa: F811
    br, client, finder = await chapters_env.make(chapters_api_key="editor-key")
    chapters_env.abs.FILE_CHAPTERS = {**chapters_env.abs.FILE_CHAPTERS, "b4": [(0, 900)]}
    r = await client.post("/api/chapters/b4/proposal", json={
        "chapters": [{"title": "Chapter 1", "start": 3.0}, {"title": "Chapter 2", "start": 400.0}],
        "warnings": [], "quality": "good", "note": "2 spoken chapter announcements"})
    assert r.status == 200, await r.text()
    b = await r.json()
    assert b["source"] == "audio" and b["status"] == "ready"
    assert b["proposal"]["chapters"] == [{"title": "Chapter 1", "start": 0.0, "end": 400.0},
                                         {"title": "Chapter 2", "start": 400.0, "end": 900.0}]
    await scan(client, finder)                                   # a scan keeps it
    assert finder.books["b4"]["source"] == "audio"
    res = await (await client.post("/api/chapters/apply", json={"item_ids": ["b4"]})).json()
    assert res["applied"] == 1 and chapters_env.abs.writes[0][1] == "b4"
    bad = await client.post("/api/chapters/b4/proposal", json={"chapters": [{"start": 1}]})
    assert bad.status == 400
