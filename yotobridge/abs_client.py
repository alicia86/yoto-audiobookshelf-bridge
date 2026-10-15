"""Minimal Audiobookshelf API client.

Endpoints as in the ABS server source (server/routers/ApiRouter.js, PlaybackSessionManager.js).
Every call takes the API key to use, so different Yoto players can map to different ABS users.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)


class ABSError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"ABS {status}: {msg}")
        self.status = status


class ABSClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, default_key: str):
        self.http = session
        self.base = base_url.rstrip("/")
        self.default_key = default_key

    def _headers(self, key: str | None) -> dict:
        return {"Authorization": f"Bearer {key or self.default_key}"}

    async def _req(self, method: str, path: str, key: str | None = None, timeout: float = 30, **kw):
        try:
            async with self.http.request(method, self.base + path, headers=self._headers(key),
                                         timeout=aiohttp.ClientTimeout(total=timeout), **kw) as r:
                if r.status >= 400:
                    raise ABSError(r.status, (await r.text())[:200])
                if r.content_type == "application/json":
                    return await r.json()
                return await r.read()
        except asyncio.TimeoutError:
            # A bare TimeoutError has no message, so the phone page showed an empty error.
            raise ABSError(504, f"Audiobookshelf did not answer within {timeout:g} s, try again") from None

    def full_url(self, content_url: str) -> str:
        """ABS returns track URLs relative to the server. If ABS runs under a sub-path and the
        URL already includes it, join on the origin instead of doubling the prefix."""
        if content_url.startswith("http"):
            return content_url
        parts = urlsplit(self.base)
        if parts.path and content_url.startswith(parts.path + "/"):
            return f"{parts.scheme}://{parts.netloc}{content_url}"
        return self.base + content_url

    def auth_header(self, key: str | None = None) -> str:
        return self._headers(key)["Authorization"]

    # ---- library ----
    async def libraries(self, key=None) -> list[dict]:
        data = await self._req("GET", "/api/libraries", key)
        return [lib for lib in data.get("libraries", []) if lib.get("mediaType", "book") == "book"]

    async def library_items(self, library_id: str, page=0, limit=60, key=None) -> dict:
        """The library, most recently added first (the phone page's Library tab)."""
        return await self._req("GET", f"/api/libraries/{library_id}/items", key, params={
            "limit": limit, "page": page, "sort": "addedAt", "desc": 1, "minified": 1})

    async def search(self, library_id: str, q: str, key=None) -> list[dict]:
        data = await self._req("GET", f"/api/libraries/{library_id}/search", key, timeout=90,
                               params={"q": q, "limit": 30})
        return [b["libraryItem"] for b in data.get("book", [])]

    async def item(self, item_id: str, key=None) -> dict:
        return await self._req("GET", f"/api/items/{item_id}", key, params={"expanded": 1})

    async def cover(self, item_id: str) -> bytes:
        return await self._req("GET", f"/api/items/{item_id}/cover")

    async def series_items(self, library_id: str, series_id: str, key=None) -> list[dict]:
        """All books of a series (full item JSON, so media.metadata.series carries each book's sequence).
        ABS decodes the filter value as base64 (server/utils/queries/libraryFilters.js decode())."""
        f = "series." + base64.b64encode(series_id.encode()).decode()
        data = await self._req("GET", f"/api/libraries/{library_id}/items", key, params={"filter": f, "limit": 1000})
        return data.get("results", [])

    async def collections(self, key=None) -> list[dict]:
        """All collections the key's user can see, each with its books in the collection's own order."""
        return (await self._req("GET", "/api/collections", key)).get("collections", [])

    async def collection(self, collection_id: str, key=None) -> dict:
        return await self._req("GET", f"/api/collections/{collection_id}", key)

    async def item_tracks(self, item_id: str, key=None) -> list[dict]:
        """Audio tracks of a book without opening a playback session (expanded item JSON)."""
        return (await self.item(item_id, key)).get("media", {}).get("tracks", [])

    # ---- progress ----
    async def progress(self, item_id: str, key=None) -> dict | None:
        try:
            return await self._req("GET", f"/api/me/progress/{item_id}", key)
        except ABSError as e:
            if e.status == 404:
                return None
            raise

    async def all_progress(self, key=None) -> list[dict]:
        """Every media progress entry of the key's user, in one call."""
        return (await self._req("GET", "/api/me", key)).get("mediaProgress", [])

    async def mark_finished(self, item_id: str, duration: float, key=None):
        await self._req("PATCH", f"/api/me/progress/{item_id}", key, json={
            "currentTime": duration, "duration": duration, "progress": 1, "isFinished": True})

    async def items_in_progress(self, key=None) -> list[dict]:
        data = await self._req("GET", "/api/me/items-in-progress", key)
        return data.get("libraryItems", [])

    async def patch_progress(self, item_id: str, current: float, duration: float, key=None):
        await self._req("PATCH", f"/api/me/progress/{item_id}", key, json={
            "currentTime": current, "duration": duration,
            "progress": (current / duration) if duration else 0})

    # ---- playback sessions (these drive ABS listening stats and "continue listening") ----
    async def play(self, item_id: str, device_id: str, device_name: str, key=None) -> dict:
        return await self._req("POST", f"/api/items/{item_id}/play", key, json={
            "deviceInfo": {"deviceId": device_id, "clientName": "Yoto bridge", "deviceName": device_name},
            "forceDirectPlay": True, "forceTranscode": False, "mediaPlayer": "yoto-bridge",
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4", "audio/flac", "audio/ogg", "audio/aac"]})

    async def sync(self, session_id: str, current: float, listened: float, duration: float, key=None):
        await self._req("POST", f"/api/session/{session_id}/sync", key, json={
            "currentTime": round(current, 1), "timeListened": round(listened, 1), "duration": duration})

    async def close(self, session_id: str, current: float, listened: float, duration: float, key=None):
        await self._req("POST", f"/api/session/{session_id}/close", key, json={
            "currentTime": round(current, 1), "timeListened": round(listened, 1), "duration": duration})
