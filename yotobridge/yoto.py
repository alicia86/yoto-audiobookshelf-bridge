"""Yoto developer API: OAuth browser login (PKCE), playlists ("content"), players, and MQTT control.

REST endpoints are from yoto.dev (read 2026-10-10). MQTT uses the yoto-api package's client
(the one Home Assistant's Yoto integration uses): one connection per account, auto-reconnect with
a fresh token, and the 4-minute events/request heartbeat without which players stop reporting.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)
# user:icons:manage: chapter icons on offline copies. The Yoto app must have it ticked (dashboard.yoto.dev).
SCOPES = "offline_access user:content:manage user:icons:manage family:devices:view family:devices:control family:library:view family:library:manage"


class YotoNotLoggedIn(Exception):
    pass


class YotoLoginError(Exception):
    """The browser login could not be completed; the message is shown on the phone page."""


class YotoAuth:
    """Browser login (authorization code + PKCE, what yoto.dev asks new apps to use; device-code
    is deprecated and refused for new clients) and token refresh. Refresh tokens are single-use,
    so every refresh is written to disk before the new access token is used."""

    LOGIN_TTL = 900  # seconds a started login stays valid

    def __init__(self, http: aiohttp.ClientSession, client_id: str, login_url: str, audience: str, token_file: Path,
                 redirect_uri: str = ""):
        self.http = http
        self.client_id = client_id
        self.login_url = login_url
        self.audience = audience
        self.redirect_uri = redirect_uri
        self.file = token_file
        self.tokens: dict = json.loads(token_file.read_text()) if token_file.exists() else {}
        self._lock = asyncio.Lock()
        self.pending: dict | None = None   # in-progress login, shown on the web page (nothing secret in here)
        self._flow: dict | None = None     # state and PKCE verifier of that login
        self._on_done: Callable[[], None] | None = None

    @property
    def logged_in(self) -> bool:
        return bool(self.tokens.get("refresh_token"))

    def _save(self, body: dict):
        body = {**self.tokens, **body, "obtained_at": time.time()}
        self.tokens = body
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(body))
        tmp.chmod(0o600)
        tmp.replace(self.file)

    async def start_login(self, on_done: Callable[[], None] | None = None) -> dict:
        """Make the Yoto sign-in link. Yoto sends the browser back to redirect_uri with a code,
        which finish_login exchanges for tokens."""
        if not self.client_id:
            raise YotoNotLoggedIn("Set yoto_client_id in config.yaml (from dashboard.yoto.dev) first.")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = f"{self.login_url}/authorize?" + urlencode({
            "audience": self.audience, "scope": SCOPES, "response_type": "code", "client_id": self.client_id,
            "code_challenge": challenge, "code_challenge_method": "S256", "redirect_uri": self.redirect_uri,
            "state": state})
        expires = time.time() + self.LOGIN_TTL
        self._flow = {"state": state, "verifier": verifier, "expires_at": expires}
        self._on_done = on_done
        self.pending = {"url": url, "redirect_uri": self.redirect_uri, "expires_at": expires}
        return self.pending

    async def finish_login(self, code: str, state: str):
        flow = self._flow
        if not flow or time.time() > flow["expires_at"]:
            self.pending = self._flow = None
            raise YotoLoginError("This login link has expired. Press Connect Yoto again.")
        if not secrets.compare_digest(state or "", flow["state"]):
            raise YotoLoginError("This sign-in does not match the current login. Press Connect Yoto again.")
        async with self.http.post(f"{self.login_url}/oauth/token", data={
                "grant_type": "authorization_code", "client_id": self.client_id, "code_verifier": flow["verifier"],
                "code": code, "redirect_uri": self.redirect_uri}) as r:
            body = await r.json(content_type=None)
        if r.status != 200 or not body.get("refresh_token"):
            log.warning("Yoto login failed (%s): %s", r.status, body)
            raise YotoLoginError(f"Yoto refused the login: {_describe(body) or r.status}")
        self.tokens = {}                 # a reconnect replaces the old login (and its scopes) entirely
        self._save(body)
        self.pending = self._flow = None
        log.info("Yoto login complete")
        if self._on_done:
            res = self._on_done()
            if asyncio.iscoroutine(res):
                await res

    async def access_token(self) -> str:
        async with self._lock:
            if not self.logged_in:
                raise YotoNotLoggedIn("Not logged in to Yoto. Open the bridge page and press Connect Yoto.")
            exp = self.tokens.get("obtained_at", 0) + self.tokens.get("expires_in", 0) - 120
            if self.tokens.get("access_token") and time.time() < exp:
                return self.tokens["access_token"]
            async with self.http.post(f"{self.login_url}/oauth/token", data={
                    "grant_type": "refresh_token", "client_id": self.client_id,
                    "refresh_token": self.tokens["refresh_token"], "audience": self.audience}) as r:
                body = await r.json(content_type=None)
                if r.status != 200:
                    log.error("Yoto token refresh failed (%s): %s", r.status, body)
                    if body.get("error") in ("invalid_grant", "unauthorized_client"):
                        self.tokens = {}
                        self.file.unlink(missing_ok=True)
                    raise YotoNotLoggedIn("Yoto token refresh failed; log in again from the bridge page.")
            self._save(body)
            return self.tokens["access_token"]


def _describe(body: dict) -> str:
    """Auth0's reason for refusing, e.g. 'unauthorized_client: Grant type ... not allowed for the client.'"""
    if not isinstance(body, dict):
        return ""
    return ": ".join(x for x in (body.get("error"), body.get("error_description")) if x)


class YotoAPI:
    TRANSCODE_POLL = 2.0        # seconds between checks while Yoto transcodes an upload
    TRANSCODE_TIMEOUT = 900     # a job not done in this long is given up and the track uploaded again
    TRANSCODE_TRIES = 3         # uploads of one track before the copy fails

    def __init__(self, http: aiohttp.ClientSession, auth: YotoAuth, base: str):
        self.http = http
        self.auth = auth
        self.base = base.rstrip("/")

    async def _req(self, method: str, path: str, **kw):
        token = await self.auth.access_token()
        headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
        async with self.http.request(method, self.base + path, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=30), **kw) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Yoto API {method} {path} -> {r.status}: {text[:300]}")
            return json.loads(text) if text else {}

    async def devices(self) -> list[dict]:
        return (await self._req("GET", "/device-v2/devices/mine")).get("devices", [])

    async def my_playlists(self) -> list[dict]:
        return (await self._req("GET", "/content/mine")).get("cards", [])

    async def groups(self) -> list[dict]:
        """User-defined card groups / collections in the Yoto family library."""
        try:
            r = await self._req("GET", "/card/family/library/groups")
            if isinstance(r, list):
                return r
            return r.get("groups", r.get("items", []))
        except Exception as e:
            log.warning("Yoto groups query failed: %s", e)
            return []

    async def family_library(self) -> list[dict]:
        """Complete Yoto family library cards (physical retail cards + MYO cards)."""
        try:
            r = await self._req("GET", "/card/family/library")
            if isinstance(r, list):
                return r
            return r.get("cards", r.get("items", []))
        except Exception as e:
            log.warning("Yoto family library query failed: %s", e)
            return []

    async def playlist(self, card_id: str) -> dict:
        return (await self._req("GET", f"/content/{card_id}")).get("card", {})

    async def card_detail(self, card_id: str) -> dict:
        """Card detail and metadata for any card in the family library."""
        try:
            return (await self._req("GET", f"/content/{card_id}")).get("card", {})
        except Exception:
            try:
                return (await self._req("GET", f"/card/{card_id}")).get("card", {})
            except Exception as e:
                log.warning("Yoto card detail query failed for %s: %s", card_id, e)
                return {}

    async def update_playlist(self, card_id: str, title: str | None = None, chapters: list[dict] | None = None, cover_url: str | None = None) -> dict:
        """Updates an existing MYO playlist on Yoto Cloud (titles, chapters/tracks, display icons)."""
        card = await self.card_detail(card_id)
        if not card:
            raise RuntimeError(f"Card {card_id} not found in Yoto Cloud")

        body: dict = {
            "cardId": card_id,
            "title": title or card.get("title", ""),
            "content": card.get("content", {})
        }
        if "metadata" in card:
            body["metadata"] = card["metadata"]
        else:
            body["metadata"] = {}

        if cover_url:
            body["metadata"]["cover"] = {"imageL": cover_url}

        if chapters is not None:
            body["content"]["chapters"] = chapters

        res = await self._req("POST", "/content", json=body)
        return res.get("card", res)

    async def device_status(self, device_id: str) -> dict:
        """Device status and configuration from Yoto API."""
        try:
            return await self._req("GET", f"/device-v2/devices/{device_id}")
        except Exception as e:
            log.warning("Yoto device status query failed for %s: %s", device_id, e)
            return {}

    async def upload_cover(self, image: bytes, content_type: str = "image/jpeg") -> str | None:
        try:
            r = await self._req("POST", "/media/coverImage/user/me/upload", data=image,
                                params={"autoconvert": "true", "coverType": "default"},
                                headers={"Content-Type": content_type})
            return (r.get("coverImage") or {}).get("mediaUrl")
        except Exception as e:  # a missing cover never blocks making a card
            log.warning("cover upload failed: %s", e)
            return None

    async def save_stream_playlist(self, title: str, stream_url: str, card_id: str | None = None,
                                   cover_url: str | None = None, description: str = "",
                                   icon_url: str | None = None) -> str:
        """Create (or, with card_id, overwrite) a playlist whose single track is a stream from the bridge.
        Overwriting replaces the playlist's audio, which is how an existing MYO card is repurposed
        without re-linking it in the Yoto app. icon_url: a 16x16 PNG the player shows on its display
        (yoto.dev "dynamic icons", display.iconUrl16x16)."""
        track = {"key": "01", "title": title, "overlayLabel": "1",
                 "type": "stream", "format": "mp3", "trackUrl": stream_url}
        chapter = {"key": "01", "title": title, "overlayLabel": "1", "tracks": [track]}
        if icon_url:
            track["display"] = {"iconUrl16x16": icon_url}
            chapter["display"] = {"iconUrl16x16": icon_url}
        body: dict = {
            "title": title,
            "content": {"chapters": [chapter]},
            "metadata": {"description": description or "Streams from Audiobookshelf via the Yoto bridge"},
        }
        if cover_url:
            body["metadata"]["cover"] = {"imageL": cover_url}
        if card_id:
            body["cardId"] = card_id
        try:
            r = await self._req("POST", "/content", json=body)
        except RuntimeError as e:
            if not icon_url:
                raise
            # never let the icon cost the card its audio
            log.warning("Yoto refused the playlist with a chapter icon (%s); saving it without", e)
            track.pop("display", None)
            chapter.pop("display", None)
            r = await self._req("POST", "/content", json=body)
        card = r.get("card", r)
        return card.get("cardId") or card_id

    async def save_stream_tracks_playlist(self, title: str, tracks: list[dict], card_id: str | None = None,
                                          cover_url: str | None = None, description: str = "") -> str:
        """Create or overwrite a playlist of stream tracks, one chapter each (chapters as tracks):
        tracks are {"key", "title", "url", "icon_url"}. The player's next/previous move between them."""
        def body(with_icons: bool) -> dict:
            chapters = []
            for t in tracks:
                track = {"key": t["key"], "title": t["title"], "overlayLabel": str(int(t["key"])),
                         "type": "stream", "format": "mp3", "trackUrl": t["url"]}
                chapter = {"key": t["key"], "title": t["title"], "overlayLabel": str(int(t["key"])), "tracks": [track]}
                if with_icons and t.get("icon_url"):
                    track["display"] = chapter["display"] = {"iconUrl16x16": t["icon_url"]}
                chapters.append(chapter)
            b: dict = {"title": title, "content": {"chapters": chapters},
                       "metadata": {"description": description or "Streams from Audiobookshelf via the Yoto bridge"}}
            if cover_url:
                b["metadata"]["cover"] = {"imageL": cover_url}
            if card_id:
                b["cardId"] = card_id
            return b
        try:
            r = await self._req("POST", "/content", json=body(True))
        except RuntimeError as e:
            if not any(t.get("icon_url") for t in tracks):
                raise
            log.warning("Yoto refused the playlist with chapter icons (%s); saving it without", e)
            r = await self._req("POST", "/content", json=body(False))
        card = r.get("card", r)
        return card.get("cardId") or card_id

    async def upload_icon(self, png_bytes: bytes, filename: str) -> str:
        """Upload a 16x16 PNG as a display icon (yoto.dev "uploading icons"; scope user:icons:manage).
        Returns the reference to put in display.icon16x16."""
        r = await self._req("POST", "/media/displayIcons/user/me/upload", data=png_bytes,
                            params={"autoConvert": "false", "filename": filename},
                            headers={"Content-Type": "image/png"})
        return f"yoto:#{r['displayIcon']['mediaId']}"


    async def upload_audio(self, audio: bytes, title: str, on_phase=None) -> dict:
        """Upload one MP3 as MYO audio (yoto.dev "uploading to cards"): get a signed URL, PUT the file,
        wait for Yoto's transcode. Returns the track fields for a playlist. Yoto sometimes never finishes
        a job (seen: a 2 h track still processing after 15 min, when others that long took 1-2 min), so
        a job that takes longer than TRANSCODE_TIMEOUT is given up and the track uploaded again.
        on_phase("uploading" / "processing") is told which stage it is at."""
        for attempt in range(1, self.TRANSCODE_TRIES + 1):
            if on_phase:
                on_phase("uploading")
            up = (await self._req("GET", "/media/transcode/audio/uploadUrl")).get("upload", {})
            async with self.http.put(up["uploadUrl"], data=audio, headers={"Content-Type": "audio/mpeg"},
                                     timeout=aiohttp.ClientTimeout(total=1800)) as r:
                if r.status >= 400:
                    raise RuntimeError(f"Yoto upload of {title} -> {r.status}: {(await r.text())[:200]}")
            began = time.monotonic()
            if on_phase:
                on_phase("processing")
            while time.monotonic() - began < self.TRANSCODE_TIMEOUT:
                t = (await self._req("GET", f"/media/upload/{up['uploadId']}/transcoded",
                                     params={"loudnorm": "false"})).get("transcode", {})
                if t.get("transcodedSha256"):
                    log.info("Yoto processed %s in %.0fs", title, time.monotonic() - began)
                    info = t.get("transcodedInfo") or {}
                    return {"sha": t["transcodedSha256"], "duration": info.get("duration"),
                            "fileSize": info.get("fileSize"), "channels": info.get("channels") or "mono",
                            "format": info.get("format") or "mp3"}
                await asyncio.sleep(self.TRANSCODE_POLL)
            if attempt < self.TRANSCODE_TRIES:
                log.warning("Yoto has not finished processing %s after %.0f min; uploading it again (try %d of %d)",
                            title, self.TRANSCODE_TIMEOUT / 60, attempt + 1, self.TRANSCODE_TRIES)
        raise RuntimeError(f"Yoto did not finish processing {title} in {self.TRANSCODE_TRIES} tries of "
                           f"{self.TRANSCODE_TIMEOUT / 60:.0f} min")

    async def save_track_playlist(self, title: str, tracks: list[dict], card_id: str,
                                  cover_url: str | None = None, description: str = "") -> str:
        """Overwrite a playlist with uploaded tracks, one chapter per track, keys "01", "02", ...
        The player downloads these, so they play offline."""
        chapters = []
        for t in tracks:
            track = {"key": t["key"], "title": t["title"], "overlayLabel": str(int(t["key"])),
                     "type": "audio", "trackUrl": f"yoto:#{t['sha']}", "duration": t.get("duration"),
                     "fileSize": t.get("fileSize"), "channels": t.get("channels"), "format": t.get("format"),
                     "display": {"icon16x16": t["icon"]} if t.get("icon") else None}
            chapter = {"key": t["key"], "title": t["title"], "overlayLabel": str(int(t["key"])),
                       "tracks": [{k: v for k, v in track.items() if v is not None}]}
            if t.get("icon"):
                chapter["display"] = {"icon16x16": t["icon"]}
            chapters.append(chapter)
        size = sum(t.get("fileSize") or 0 for t in tracks)
        body: dict = {"title": title, "cardId": card_id, "content": {"chapters": chapters},
                      "metadata": {"description": description,
                                   "media": {"duration": sum(t.get("duration") or 0 for t in tracks),
                                             "fileSize": size, "readableFileSize": round(size / 1e6, 1)}}}
        if cover_url:
            body["metadata"]["cover"] = {"imageL": cover_url}
        r = await self._req("POST", "/content", json=body)
        card = r.get("card", r)
        return card.get("cardId") or card_id


    async def save_mixed_playlist(self, title: str, tracks: list[dict], card_id: str,
                                  cover_url: str | None = None, description: str = "") -> str:
        """An offline copy still uploading: tracks already uploaded ({"sha", ...}, as for
        save_track_playlist) play from the card, the rest ({"url", "icon_url"}) stream from the bridge."""
        chapters = []
        for t in tracks:
            label = str(int(t["key"]))
            if t.get("sha"):
                track = {"key": t["key"], "title": t["title"], "overlayLabel": label, "type": "audio",
                         "trackUrl": f"yoto:#{t['sha']}", "duration": t.get("duration"), "fileSize": t.get("fileSize"),
                         "channels": t.get("channels"), "format": t.get("format"),
                         "display": {"icon16x16": t["icon"]} if t.get("icon") else None}
                display = {"icon16x16": t["icon"]} if t.get("icon") else None
            else:
                track = {"key": t["key"], "title": t["title"], "overlayLabel": label, "type": "stream",
                         "format": "mp3", "trackUrl": t["url"],
                         "display": {"iconUrl16x16": t["icon_url"]} if t.get("icon_url") else None}
                display = {"iconUrl16x16": t["icon_url"]} if t.get("icon_url") else None
            chapter = {"key": t["key"], "title": t["title"], "overlayLabel": label,
                       "tracks": [{k: v for k, v in track.items() if v is not None}]}
            if display:
                chapter["display"] = display
            chapters.append(chapter)
        body: dict = {"title": title, "cardId": card_id, "content": {"chapters": chapters},
                      "metadata": {"description": description}}
        if cover_url:
            body["metadata"]["cover"] = {"imageL": cover_url}
        r = await self._req("POST", "/content", json=body)
        card = r.get("card", r)
        return card.get("cardId") or card_id


class YotoControl:
    """MQTT: send play/pause commands and receive playback events for every player on the account."""

    def __init__(self, auth: YotoAuth, uri_format: str, on_event: Callable[[str, dict], None]):
        self.auth = auth
        self.uri_format = uri_format
        self.on_event = on_event
        self.client = None
        self.device_ids: list[str] = []

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def connect(self, device_ids: list[str]):
        from yoto_api.mqtt.client import YotoMqttClient
        from yoto_api.models.event import EventPatch

        if self.client:
            await self.client.disconnect()
        self.device_ids = device_ids
        self.client = YotoMqttClient()

        def cb(msg):
            # Logged at INFO for now: which card ids and positions real players send is still being
            # worked out (README "Unverified points").
            log.info("MQTT %s %s: %s", type(msg).__name__, getattr(msg, "player_id", "?"),
                     {k: getattr(v, "value", v) for k, v in (getattr(msg, "fields", None) or vars(msg)).items()
                      if k not in ("player_id",)})
            if isinstance(msg, EventPatch):
                # yoto-api renames keys to snake_case; map back to the wire names the rest of the bridge uses.
                f = msg.fields
                ev = {"cardId": f.get("card_id"), "position": f.get("position"), "trackLength": f.get("track_length"),
                      "playbackStatus": getattr(f.get("playback_status"), "value", f.get("playback_status")),
                      "source": f.get("source"), "streaming": f.get("streaming"), "trackKey": f.get("track_key"),
                      "chapterKey": f.get("chapter_key"), "chapterTitle": f.get("chapter_title"), "trackTitle": f.get("track_title")}
                self.on_event(msg.player_id, {k: v for k, v in ev.items() if v is not None})

        await self.client.connect(None, device_ids, cb, token_getter=self.auth.access_token)
        log.info("MQTT connected for %d player(s)", len(device_ids))

    def uri(self, card_id: str) -> str:
        return self.uri_format.format(card_id=card_id)

    async def play(self, device_id: str, card_id: str, seconds_in: int | None = None, track: int = 1):
        # Same payload as yoto-api's card_play, but with the uri format configurable (unverified point).
        key = f"{max(1, int(track)):02d}"
        payload: dict = {"uri": self.uri(card_id), "chapterKey": key, "trackKey": key}
        if seconds_in is not None:
            payload["secondsIn"] = int(seconds_in)
        await self.client._publish(f"device/{device_id}/command/card/start", json.dumps(payload))

    async def pause(self, device_id: str):
        await self.client.card_pause(device_id)

    async def resume(self, device_id: str):
        await self.client.card_resume(device_id)

    async def stop(self, device_id: str):
        await self.client.card_stop(device_id)

    async def set_volume(self, device_id: str, volume: int):
        """Sets the player volume level (0-100)."""
        vol = max(0, min(100, int(volume)))
        await self.client._publish(f"device/{device_id}/command/volume", json.dumps({"volume": vol}))
