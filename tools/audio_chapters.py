#!/usr/bin/env python3
"""Find a book's chapters in its own audio, for books Audible has no useful chapters for (dramatized
editions listed as a few long parts, home recordings, ...). Run it on a fast computer, not a low-power server:

    python3 tools/audio_chapters.py --search "Alcatraz"              # list matching books and their ids
    python3 tools/audio_chapters.py li_abc123 li_def456               # find chapters, send them to the bridge

Two signals are combined:
  1. Speech: Whisper (faster-whisper) transcribes the audio and finds the narrator saying "Chapter N".
  2. Pauses: ffmpeg's silencedetect finds the quiet gaps between scenes. Each announced chapter starts at
     the end of the pause just before it (so a music sting before "Chapter 3" belongs to chapter 3), and
     a chapter number that is never said is placed at the longest pause between its neighbours.
With no announcements at all it falls back to pauses only (--expect N picks the N-1 strongest).

Results go to the bridge's Chapters tab as proposals (nothing changes in ABS until applied there), and
to a JSON file next to the downloaded audio. Reads abs_url, abs_api_key and ui_pin from the bridge's
data/config.yaml unless given on the command line.

Needs: ffmpeg on PATH, and `pip install faster-whisper` (the model downloads on first use, ~150 MB for base.en).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
NUMBERS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                "ninety": 90})
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
ROMAN = re.compile(r"^[ivxlc]+$")


def roman(s: str) -> int | None:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total or None


def parse_number(words: list[str]) -> tuple[int | None, int]:
    """The chapter number at the start of `words` (already lower-cased, punctuation stripped) and how
    many words it used: "21", "twenty-one", "twenty one", "seven", "xii", "the first"."""
    if not words:
        return None, 0
    w = words[0]
    if w == "the" and len(words) > 1 and words[1] in ORDINALS:
        return ORDINALS[words[1]], 2
    if w.isdigit():
        return int(w), 1
    if "-" in w:
        a, _, b = w.partition("-")
        if a in NUMBERS and b in NUMBERS and NUMBERS[a] >= 20 and NUMBERS[b] < 10:
            return NUMBERS[a] + NUMBERS[b], 1
    if w in NUMBERS:
        n = NUMBERS[w]
        if n >= 20 and len(words) > 1 and words[1] in NUMBERS and 0 < NUMBERS[words[1]] < 10:
            return n + NUMBERS[words[1]], 2
        return n, 1
    if w in ORDINALS:
        return ORDINALS[w], 1
    if ROMAN.match(w) and len(w) <= 6:
        return roman(w), 1
    return None, 0


def clean(word: str) -> str:
    return re.sub(r"[^\w-]", "", word.lower()).strip("-")


def find_announcements(words: list[tuple[float, float, str]], gaps: list[tuple[float, float]],
                       max_lead: float = 25.0) -> list[tuple[float, int]]:
    """(time, number) for every "Chapter N" that looks like an announcement rather than dialogue: it
    follows a pause (in the speech, or a detected quiet gap shortly before)."""
    out = []
    for i, (start, _end, text) in enumerate(words):
        if clean(text) != "chapter":
            continue
        n, _ = parse_number([clean(w[2]) for w in words[i + 1:i + 4]])
        if not n:
            continue
        pause_before = start - words[i - 1][1] if i else float("inf")
        near_gap = any(start - max_lead <= g_end <= start + 0.5 for _, g_end in gaps)
        if pause_before >= 0.6 or near_gap:
            out.append((start, n))
    return out


def best_sequence(found: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Longest run of announcements whose numbers rise with time (drops repeats, recaps and stray mentions)."""
    found = sorted(found)
    best: list[list[tuple[float, int]]] = []
    for t, n in found:
        chain = [c for c in best if c[-1][1] < n]
        new = (max(chain, key=len) if chain else []) + [(t, n)]
        best.append(new)
    return max(best, key=len) if best else []


def snap(t: float, gaps: list[tuple[float, float]], max_lead: float = 25.0) -> float:
    """Start a chapter at the end of the last pause before its announcement (music stings stay with it)."""
    ends = [g_end for _, g_end in gaps if t - max_lead <= g_end <= t + 0.5]
    return max(ends) - 0.2 if ends else max(0.0, t - 0.5)


def longest_gap(gaps: list[tuple[float, float]], lo: float, hi: float, margin: float = 60.0) -> tuple[float, float] | None:
    inside = [g for g in gaps if lo + margin < g[1] < hi - margin]
    return max(inside, key=lambda g: g[1] - g[0]) if inside else None


def build_chapters(announced: list[tuple[float, int]], gaps: list[tuple[float, float]], duration: float,
                   expect: int | None = None) -> dict:
    """Chapter list plus warnings and a quality verdict, from announcements and pauses."""
    warnings: list[str] = []
    seq = best_sequence(announced)
    if len(seq) >= 2:
        chapters = [{"title": f"Chapter {n}", "start": snap(t, gaps), "n": n} for t, n in seq]
        placed = []
        filled = []
        for a, b in zip(chapters, chapters[1:]):
            filled.append(a)
            lo = a["start"]
            for missing in range(a["n"] + 1, b["n"]):
                g = longest_gap(gaps, lo, b["start"])
                if not g:
                    continue
                filled.append({"title": f"Chapter {missing}", "start": g[1] - 0.2, "n": missing})
                placed.append(missing)
                lo = g[1]
        filled.append(chapters[-1])
        chapters = filled
        first = chapters[0]
        if first["start"] > 20:
            chapters.insert(0, {"title": "Opening", "start": 0.0, "n": 0})
        missing = sorted(set(range(1, seq[-1][1] + 1)) - {c["n"] for c in chapters})
        if placed:
            warnings.append(f"Chapter {', '.join(map(str, placed))} {'was' if len(placed) == 1 else 'were'} not announced; "
                            "placed at the longest pause between the neighbouring chapters. Check those.")
        if missing:
            warnings.append(f"No chapter {', '.join(map(str, missing))} found.")
        if expect and seq[-1][1] < expect:
            warnings.append(f"Found chapters up to {seq[-1][1]}, expected {expect}.")
        quality = "good" if not placed and not missing and (not expect or seq[-1][1] >= expect) else "check"
        note = f"{len(seq)} spoken chapter announcements" + (f", {len(placed)} placed at pauses" if placed else "")
    else:
        # nothing said aloud: strongest pauses, at least 3 minutes apart
        want = (expect - 1) if expect else None
        ranked = sorted((g for g in gaps if 60 < g[1] < duration - 60), key=lambda g: g[0] - g[1])
        picked: list[tuple[float, float]] = []
        for g in ranked:
            if want is not None and len(picked) >= want:
                break
            if want is None and g[1] - g[0] < 2.0:
                break
            if all(abs(g[1] - p[1]) > 180 for p in picked):
                picked.append(g)
        picked.sort(key=lambda g: g[1])
        chapters = [{"title": "Part 1", "start": 0.0, "n": 1}] + [
            {"title": f"Part {i + 2}", "start": g[1] - 0.2, "n": i + 2} for i, g in enumerate(picked)]
        warnings.append("No spoken chapter announcements found; these are the strongest pauses in the audio, "
                        "so some may fall mid-scene.")
        quality = "check" if len(chapters) > 1 else "bad"
        note = f"{len(chapters)} parts split at pauses"
    for c in chapters:
        c["start"] = round(max(0.0, c["start"]), 2)
    return {"chapters": [{"title": c["title"], "start": c["start"]} for c in chapters],
            "warnings": warnings, "quality": quality, "note": note}


# ---------------------------------------------------------------- audio work

def silences(path: Path, offset: float, noise_db: int, min_len: float) -> list[tuple[float, float]]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"],
                       capture_output=True, text=True)
    out, start = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            out.append((offset + start, offset + float(m.group(1))))
            start = None
    return out


def transcribe(model, path: Path, offset: float) -> list[tuple[float, float, str]]:
    segments, _ = model.transcribe(str(path), language="en", beam_size=1, word_timestamps=True, vad_filter=True,
                                   condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append((offset + w.start, offset + w.end, w.word))
        print(f"\r  heard up to {int((offset + seg.end) // 60)} min", end="", flush=True)
    print()
    return words


class ABS:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def get(self, path: str, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(self.url + path + q, headers={"Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def download(self, content_url: str, dest: Path):
        if dest.exists() and dest.stat().st_size > 0:
            return
        url = content_url if content_url.startswith("http") else self.url + content_url
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}"})
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(dest)


def read_config(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^(abs_url|abs_api_key|chapters_api_key|ui_pin)\s*:\s*[\"']?([^\"'#\s]+)", line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def main():
    here = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("items", nargs="*", help="ABS library item ids")
    ap.add_argument("--search", help="list books matching this text, with their ids, and stop")
    ap.add_argument("--config", default=str(here / "data" / "config.yaml"))
    ap.add_argument("--abs-url")
    ap.add_argument("--abs-key")
    ap.add_argument("--bridge", default=os.environ.get("YOTO_BRIDGE_URL", "http://localhost:8090"),
                    help="bridge address, e.g. http://<host-ip>:8090 (or set YOTO_BRIDGE_URL); '' to only write JSON")
    ap.add_argument("--pin")
    ap.add_argument("--model", default="base.en", help="faster-whisper model: tiny.en (fast), base.en, small.en (best)")
    ap.add_argument("--expect", type=int, help="number of chapters the book should have")
    ap.add_argument("--noise", type=int, default=-35, help="silence threshold in dB")
    ap.add_argument("--min-pause", type=float, default=0.6, help="shortest pause in seconds")
    ap.add_argument("--resend", action="store_true", help="send results found earlier to the bridge again, without listening")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "yoto-abs-chapters"))
    a = ap.parse_args()

    cfg = read_config(Path(a.config))
    abs_ = ABS(a.abs_url or cfg.get("abs_url", ""), a.abs_key or cfg.get("abs_api_key", ""))
    if not abs_.url or not abs_.key:
        sys.exit("Need --abs-url and --abs-key (or a data/config.yaml with abs_url and abs_api_key).")

    if a.search:
        for lib in abs_.get("/api/libraries").get("libraries", []):
            for hit in abs_.get(f"/api/libraries/{lib['id']}/search", q=a.search, limit=50).get("book", []):
                it = hit["libraryItem"]
                md = it["media"]["metadata"]
                print(f"{it['id']}  {md.get('title')}  ({md.get('authorName', '')}, "
                      f"{len(it['media'].get('chapters') or [])} chapters, {int((it['media'].get('duration') or 0) // 60)} min)")
        return
    if not a.items:
        sys.exit("Give item ids, or --search to find them.")

    if a.resend:
        for item_id in a.items:
            saved = json.loads((Path(a.cache) / item_id / "chapters.json").read_text())
            send(a, cfg, item_id, {k: saved[k] for k in ("chapters", "warnings", "quality", "note")},
                 Path(a.cache) / item_id)
        return

    from faster_whisper import WhisperModel   # imported late so --search works without it
    model = WhisperModel(a.model, device="auto", compute_type="int8")
    for item_id in a.items:
        item = abs_.get(f"/api/items/{item_id}", expanded=1)
        media = item["media"]
        title, duration = media["metadata"].get("title", item_id), float(media.get("duration") or 0)
        print(f"{title}: {len(media['tracks'])} file(s), {int(duration // 60)} min")
        folder = Path(a.cache) / item_id
        folder.mkdir(parents=True, exist_ok=True)
        gaps: list[tuple[float, float]] = []
        words: list[tuple[float, float, str]] = []
        for i, tr in enumerate(media["tracks"]):
            ext = Path(urllib.parse.urlsplit(tr["contentUrl"]).path).suffix or ".audio"
            path = folder / f"{i:03d}{ext if len(ext) <= 6 else '.audio'}"
            print(f"  file {i + 1}: downloading")
            abs_.download(tr["contentUrl"], path)
            off = float(tr.get("startOffset") or 0)
            if i:
                gaps.append((off - 0.1, off))              # a file boundary counts as a pause
            gaps += silences(path, off, a.noise, a.min_pause)
            print(f"  file {i + 1}: {len(gaps)} pauses so far; listening")
            words += transcribe(model, path, off)
        found = find_announcements(words, gaps)
        result = build_chapters(found, gaps, duration, a.expect)
        (folder / "chapters.json").write_text(json.dumps({"item_id": item_id, "title": title, **result,
                                                          "announcements": found}, indent=1))
        print(f"  {result['note']}; {len(result['chapters'])} chapters ({result['quality']})")
        for w in result["warnings"]:
            print("  !", w)
        send(a, cfg, item_id, result, folder)


def send(a, cfg: dict, item_id: str, result: dict, folder: Path):
    if not a.bridge:
        return
    headers = {"Content-Type": "application/json"}
    if a.pin or cfg.get("ui_pin"):
        headers["X-Pin"] = a.pin or cfg["ui_pin"]
    req = urllib.request.Request(f"{a.bridge.rstrip('/')}/api/chapters/{item_id}/proposal",
                                 data=json.dumps(result).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60):
            print("  sent to the bridge's Chapters tab for review")
    except Exception as e:
        print(f"  could not send to the bridge ({e}); the result is in {folder / 'chapters.json'}; "
              "send it later with --resend")


if __name__ == "__main__":
    main()
