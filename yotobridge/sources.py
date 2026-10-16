"""Where a card's progress goes: Audiobookshelf, or the bridge for a card that keeps its own place.

Stands in for the ABS client everywhere: every call passes through to ABS, except that a card with a
LocalKey (localprogress.py) has its progress and playback sessions kept by the bridge. ABS still
serves that card's audio, chapters and covers, but never hears where it got to.
"""
from __future__ import annotations

from .abs_client import ABSClient
from .localprogress import LocalKey, LocalProgress, local_session, parse_session


class Sources:
    def __init__(self, abs_client: ABSClient, local: LocalProgress | None = None):
        self.abs = abs_client
        self.local = local

    def __getattr__(self, name):
        return getattr(self.abs, name)

    def __setattr__(self, name, value):
        if name not in ("abs", "local") and hasattr(self.abs, name):
            setattr(self.abs, name, value)        # e.g. the ABS address
        else:
            object.__setattr__(self, name, value)

    def _own(self, key) -> str | None:
        """The card whose own place this key keeps, if it is a LocalKey."""
        return key.slot_id if isinstance(key, LocalKey) and self.local is not None else None

    async def progress(self, item_id: str, key=None) -> dict | None:
        if (own := self._own(key)) is not None:
            return self.local.get(own, item_id)
        return await self.abs.progress(item_id, key)

    async def all_progress(self, key=None) -> list[dict]:
        if (own := self._own(key)) is not None:
            return self.local.all(own)
        return await self.abs.all_progress(key)

    async def mark_finished(self, item_id: str, duration: float, key=None):
        if (own := self._own(key)) is not None:
            return self.local.set(own, item_id, duration, duration, finished=True)
        await self.abs.mark_finished(item_id, duration, key)

    async def patch_progress(self, item_id: str, current: float, duration: float, key=None):
        if (own := self._own(key)) is not None:
            return self.local.set(own, item_id, current, duration)
        await self.abs.patch_progress(item_id, current, duration, key)

    async def play(self, item_id: str, device_id: str, device_name: str, key=None) -> dict:
        if (own := self._own(key)) is not None:
            # a session of the bridge's own: the book's tracks from ABS, the place from the card
            media = (await self.abs.item(item_id, key)).get("media", {})
            tracks = media.get("tracks", [])
            duration = float(media.get("duration") or 0) or sum(float(t.get("duration") or 0) for t in tracks)
            prog = self.local.get(own, item_id) or {}
            pos = duration if prog.get("isFinished") else float(prog.get("currentTime") or 0)
            return {"id": local_session(own, item_id), "duration": duration, "startTime": pos, "currentTime": pos,
                    "audioTracks": tracks}
        return await self.abs.play(item_id, device_id, device_name, key)

    async def sync(self, session_id: str, current: float, listened: float, duration: float, key=None):
        if (own := parse_session(session_id)) is not None:
            return self.local.set(*own, current, duration) if self.local is not None else None
        await self.abs.sync(session_id, current, listened, duration, key)

    async def close(self, session_id: str, current: float, listened: float, duration: float, key=None):
        if (own := parse_session(session_id)) is not None:
            return self.local.set(*own, current, duration) if self.local is not None else None
        await self.abs.close(session_id, current, listened, duration, key)
