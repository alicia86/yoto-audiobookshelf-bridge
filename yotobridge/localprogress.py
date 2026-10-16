"""Cards that keep their own place: listening on them is never written to Audiobookshelf.

A card with `own_place` set gets a LocalKey instead of its plain ABS key (PlaybackManager.key_for). The
key still authenticates with ABS for audio, chapters and covers, but Sources sends the card's progress
reads and writes (progress, all_progress, patch_progress, mark_finished, and the play/sync/close of a
playback session) here, to <data_dir>/card_progress.json, so ABS sees no session and no position from
it. Useful for a bedtime card that is left running all night: the book in ABS stays where it was.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class LocalKey(str):
    """The ABS key of a card that keeps its own place; `slot_id` says which card's place."""

    def __new__(cls, key: str, slot_id: str):
        s = super().__new__(cls, key or "")
        s.slot_id = slot_id
        return s


class LocalProgress:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "card_progress.json"
        self.data: dict[str, dict[str, dict]] = {}        # slot id -> item id -> progress (ABS shape)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                log.exception("could not read %s; cards that keep their own place start over", self.path)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    def get(self, slot_id: str, item_id: str) -> dict | None:
        p = self.data.get(slot_id, {}).get(item_id)
        return dict(p) if p else None

    def all(self, slot_id: str) -> list[dict]:
        return [dict(p) for p in self.data.get(slot_id, {}).values()]

    def set(self, slot_id: str, item_id: str, current: float, duration: float, finished: bool = False):
        self.data.setdefault(slot_id, {})[item_id] = {
            "libraryItemId": item_id, "currentTime": float(current), "duration": float(duration or 0),
            "progress": (current / duration) if duration else 0, "isFinished": bool(finished),
            "lastUpdate": int(time.time() * 1000)}
        self._save()

    def forget(self, slot_id: str):
        if self.data.pop(slot_id, None) is not None:
            self._save()


def local_session(slot_id: str, item_id: str) -> str:
    return f"local:{slot_id}:{item_id}"


def parse_session(session_id: str) -> tuple[str, str] | None:
    if not session_id.startswith("local:"):
        return None
    _, slot_id, item_id = session_id.split(":", 2)
    return slot_id, item_id
