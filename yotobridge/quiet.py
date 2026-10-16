"""Track cuts on a pause, where a cut is not at a chapter boundary.

Chapter boundaries are pauses already. Cuts made inside a chapter (a chapter over an hour split in
parts, a book without chapters in 30-minute parts, an offline track split to fit) used to land at
evenly spaced times, often mid-word: a stutter between streamed tracks, a clipped syllable between
uploaded ones. Each such cut now moves to the middle of the nearest pause within WINDOW seconds.

The pauses found are kept per book in <data_dir>/quiet.json, and a cut is worked out from that file
alone, so the same book always gets the same cuts: a streaming card's track numbers stay put, and an
offline copy picked up after a restart recognises the tracks it already uploaded.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

WINDOW = 20.0              # seconds either side of a planned cut
GOOD_PAUSE = 0.5           # a pause at least this long beats a shorter one nearer the planned time

Snap = Callable[[float], float]


class QuietSpots:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "quiet.json"
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                self.data = {}

    def snapper(self, item_id: str) -> Snap | None:
        spots = (self.data.get(item_id) or {}).get("quiet") or []
        if not spots:
            return None

        def snap(t: float) -> float:
            near = [(s, e) for s, e in spots if abs((s + e) / 2 - t) <= WINDOW]
            if not near:
                return t
            s, e = min(near, key=lambda p: (p[1] - p[0] < GOOD_PAUSE, abs((p[0] + p[1]) / 2 - t)))
            return round((s + e) / 2, 2)
        return snap

    def missing(self, item_id: str, cuts: list[float]) -> list[float]:
        """The cuts whose window has not been listened to yet."""
        done = (self.data.get(item_id) or {}).get("scanned") or []
        return sorted({round(t, 1) for t in cuts
                       if not any(a <= t - WINDOW + 0.5 and t + WINDOW - 0.5 <= b for a, b in done)})

    def add(self, item_id: str, window: tuple[float, float], spots: list[tuple[float, float]]):
        d = self.data.setdefault(item_id, {"scanned": [], "quiet": []})
        d["scanned"].append([round(window[0], 2), round(window[1], 2)])
        known = {(round(a, 2), round(b, 2)) for a, b in d["quiet"]}
        d["quiet"] += [[round(a, 2), round(b, 2)] for a, b in spots if (round(a, 2), round(b, 2)) not in known]
        d["quiet"].sort()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".quiet")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


def cut(snap: Snap | None, t: float, lo: float, hi: float) -> float:
    """Planned cut t, moved to a pause by snap if it stays clear of the neighbouring cuts lo and hi."""
    if snap is None:
        return t
    v = snap(t)
    return v if lo + 1 < v < hi - 1 else t
