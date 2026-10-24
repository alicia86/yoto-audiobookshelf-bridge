"""Push alerts (ntfy) when something the cards depend on breaks, so a failure shows up on your phone
instead of as "the card doesn't play" at bedtime.

Every few minutes the bridge checks:
  - Audiobookshelf answers, and accepts the bridge's key;
  - the Yoto login still works (an expired or revoked login stops every card);
  - the player control connection (MQTT) is up;
  - the public address the players use reaches this bridge (the proxy, tunnel or port forward, or the LAN address).
A problem is announced once, after it has failed `alert_after_failures` checks in a row (so an ABS
restart doesn't page you), and again when it is fixed. A card that fails to play is announced at once.

ntfy (https://ntfy.sh, or your own ntfy server) is a plain HTTP POST: the message is the body, the
title and priority are headers. Install the ntfy app and subscribe to the same topic.

Discord (discord_webhook_url) sends a rich embed. Each post uses ?wait=true so Discord returns the message id; ids are kept in <data_dir>/discord_message_history.json
and messages older than 48 hours are deleted on the next dispatch, keeping the channel a rolling log.
ntfy and Discord are independent: either, both or neither may be configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .abs_client import ABSError
from .yoto import YotoNotLoggedIn

log = logging.getLogger(__name__)

# Discord embed style per event kind -> (emoji, decimal colour, severity, status)
DISCORD_STYLES: dict[str, tuple[str, int, str, str]] = {
    "degraded": ("\u26a0\ufe0f", 16753920, "WARNING", "Degraded"),
    "recovered": ("\u2705", 65280, "RESOLVED", "Operational"),
    "playback": ("\U0001f6a8", 15158332, "CRITICAL", "Playback Failed"),
    "test": ("\U0001f535", 3447003, "INFO", "Test"),
}
EVENT_TITLES = {"degraded": "Service Degraded", "recovered": "Service Recovered",
                "playback": "Playback Error", "test": "System Test"}
SUBSYSTEMS = {"abs": "Audiobookshelf", "yoto": "Yoto Cloud", "mqtt": "MQTT", "public": "Public Gateway"}
FOOTER_TEXT = "Yoto Bridge"
DISCORD_RETENTION = 172800    # 48 hours


class Alerts:
    PLAY_FAILURE_REPEAT = 600     # seconds before the same "card could not play" is sent again

    def __init__(self, br):
        self.br = br
        self.cfg = br.cfg
        self.token = secrets.token_urlsafe(12)   # /s/health answers this, so the check knows it reached *this* bridge
        self.fails: dict[str, int] = {}
        self.messages: dict[str, str] = {}
        self.alerted: set[str] = set()
        self._recent: dict[str, float] = {}
        self._history_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.ntfy_url or self.cfg.discord_webhook_url)

    @property
    def problems(self) -> list[str]:
        """Current problems that have failed often enough to count, for the phone page."""
        return [self.messages[k] for k in self.messages if self.fails.get(k, 0) >= self.cfg.alert_after_failures]

    @property
    def history_path(self) -> Path:
        return Path(self.cfg.data_dir) / "discord_message_history.json"

    # ---------------- sending ----------------

    async def send(self, title: str, message: str, priority: str = "high", tags: str = "warning", *,
                   event_type: str = "degraded", subsystem: str = "Yoto Bridge",
                   trigger_count: int | None = None) -> bool:
        """Send to every configured destination. True if at least one accepted the alert."""
        if not self.enabled:
            log.info("alert (no ntfy_url or discord_webhook_url set, not sent): %s: %s", title, message)
            return False
        jobs = []
        if self.cfg.discord_webhook_url:
            jobs.append(self._send_discord(title, message, priority, tags, event_type, subsystem, trigger_count))
        if self.cfg.ntfy_url:
            jobs.append(self._send_ntfy(title, message, priority, tags))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        ok = any(r is True for r in results)
        if ok:
            log.info("alert sent: %s: %s", title, message)
        return ok

    async def _send_ntfy(self, title: str, message: str, priority: str, tags: str) -> bool:
        headers = {"Title": title, "Priority": priority, "Tags": tags}
        if self.cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {self.cfg.ntfy_token}"
        try:
            async with self.br.http.post(self.cfg.ntfy_url, data=message.encode(), headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status >= 400:
                    log.warning("ntfy refused the alert (%s): %s", r.status, (await r.text())[:200])
                    return False
        except Exception as e:
            log.warning("could not send alert to ntfy: %s", e)
            return False
        return True

    def _discord_payload(self, title: str, message: str, event_type: str, subsystem: str,
                         trigger_count: int | None) -> dict:
        emoji, colour, severity, status = DISCORD_STYLES.get(event_type, DISCORD_STYLES["degraded"])
        event = EVENT_TITLES.get(event_type, title)
        fields = [
            {"name": "Subsystem", "value": subsystem, "inline": True},
            {"name": "Severity", "value": severity, "inline": True},
            {"name": "Status", "value": status, "inline": True},
        ]
        if trigger_count is not None:
            fields.append({"name": "Trigger Count", "value": str(trigger_count), "inline": True})
        payload: dict = {
            "username": self.cfg.discord_username or "Yoto Bridge",
            "embeds": [{
                "title": f"{emoji} Yoto Bridge Alert: {event}",
                "description": message[:4000],      # Discord limit is 4096
                "color": colour,
                "fields": fields,
                "footer": {"text": FOOTER_TEXT},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
        }
        if self.cfg.discord_avatar_url:
            payload["avatar_url"] = self.cfg.discord_avatar_url
        return payload

    async def _send_discord(self, title: str, message: str, priority: str = "high", tags: str = "warning",
                            event_type: str = "degraded", subsystem: str = "Yoto Bridge",
                            trigger_count: int | None = None) -> bool:
        await self._prune_discord_messages()
        payload = self._discord_payload(title, message, event_type, subsystem, trigger_count)
        try:
            async with self.br.http.post(self.cfg.discord_webhook_url, params={"wait": "true"}, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status >= 400:
                    log.warning("Discord refused the alert (%s): %s", r.status, (await r.text())[:200])
                    return False
                try:
                    msg_id = (await r.json(content_type=None) or {}).get("id")
                except Exception:
                    msg_id = None
        except Exception as e:
            log.warning("could not send alert to Discord: %s", e)
            return False
        if msg_id:
            async with self._history_lock:
                history = self._load_history()
                history.append({"id": str(msg_id), "timestamp": time.time()})
                self._save_history(history)
        return True

    def _load_history(self) -> list[dict]:
        try:
            data = json.loads(self.history_path.read_text())
            return [e for e in data if isinstance(e, dict) and e.get("id")] if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning("Discord history %s unreadable, starting fresh: %s", self.history_path, e)
            return []

    def _save_history(self, history: list[dict]):
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(history, indent=2))
            tmp.replace(self.history_path)              # atomic, so a crash never leaves half a file
        except Exception as e:
            log.warning("could not write Discord history %s: %s", self.history_path, e)

    async def _prune_discord_messages(self):
        """Delete webhook messages older than 48 hours. Failed deletes stay in the log and are retried."""
        if not self.cfg.discord_webhook_url:
            return
        async with self._history_lock:
            history = self._load_history()
            now = time.time()
            expired = [e for e in history if now - float(e.get("timestamp", 0)) > DISCORD_RETENTION]
            if not expired:
                return

            async def delete(entry: dict) -> bool:
                url = f"{self.cfg.discord_webhook_url}/messages/{entry['id']}"
                try:
                    async with self.br.http.delete(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status in (200, 204, 404):     # 404: already deleted by hand
                            return True
                        log.warning("Discord refused to delete message %s (%s)", entry["id"], r.status)
                except Exception as e:
                    log.warning("could not delete Discord message %s: %s", entry["id"], e)
                return False

            results = await asyncio.gather(*(delete(e) for e in expired))
            gone = {e["id"] for e, ok in zip(expired, results) if ok}
            if gone:
                self._save_history([e for e in history if e["id"] not in gone])
                log.info("pruned %d Discord alert(s) older than 48 h", len(gone))

    def play_failed(self, card: str, why: str):
        """A card was tapped and the bridge could not play it. Sent at once, not repeated for 10 minutes."""
        msg = f"{card}: {why}"
        if time.time() - self._recent.get(msg, 0) < self.PLAY_FAILURE_REPEAT:
            return
        self._recent[msg] = time.time()
        asyncio.get_running_loop().create_task(self.send("Yoto card could not play", msg, priority="urgent",
                                                         tags="rotating_light", event_type="playback",
                                                         subsystem="Card Playback"))

    # ---------------- checks ----------------

    async def _check_abs(self) -> str | None:
        try:
            await self.br.abs.libraries()
        except ABSError as e:
            if e.status in (401, 403):
                return f"Audiobookshelf refused the bridge's API key ({e.status}). Make a new key and put it in config.yaml."
            return f"Audiobookshelf answered with an error: {e}"
        except Exception as e:
            return f"Audiobookshelf is not answering at {self.cfg.abs_url} ({type(e).__name__}: {e})."
        return None

    async def _check_yoto(self) -> str | None:
        if not self.cfg.yoto_client_id:
            return None
        if not self.br.auth.logged_in:
            return "The Yoto login has expired or was never made. Open the bridge page and press Connect Yoto."
        try:
            await self.br.yoto.devices()           # also catches a login Yoto revoked before it expired
        except YotoNotLoggedIn:
            return "The Yoto login has expired. Open the bridge page and press Connect Yoto."
        except Exception as e:
            if " 401" in str(e) or " 403" in str(e):
                return "Yoto no longer accepts the bridge's login. Open the bridge page and press Connect Yoto."
            return f"Cannot reach Yoto's servers ({e})."
        return None

    async def _check_mqtt(self) -> str | None:
        if not self.br.auth.logged_in or not self.br.yoto_devices or self.br.control.connected:
            return None
        return "Lost the connection to the Yoto players (MQTT). Play-on from the phone and position tracking are down."

    async def _check_public(self) -> str | None:
        if not self.cfg.alert_check_public:
            return None
        url = f"{self.cfg.public_url}/s/health"
        try:
            async with self.br.http.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                body = (await r.text()).strip()
                if r.status != 200:
                    return f"The players' address {self.cfg.public_url} answers {r.status} instead of reaching the bridge."
                if body != self.token:
                    return f"The players' address {self.cfg.public_url} reaches something other than this bridge."
        except Exception as e:
            return (f"The players can't reach the bridge at {self.cfg.public_url} ({type(e).__name__}: {e}). "
                    "Cards will not play. Check the proxy, tunnel or port forward that publishes /s/.")
        return None

    async def check_all(self):
        for name, check in (("abs", self._check_abs), ("yoto", self._check_yoto), ("mqtt", self._check_mqtt),
                            ("public", self._check_public)):
            try:
                problem = await check()
            except Exception as e:                  # a broken check must never stop the others
                log.exception("alert check %s failed", name)
                problem = f"check {name} failed: {e}"
            if problem:
                self.fails[name] = self.fails.get(name, 0) + 1
                self.messages[name] = problem
                if self.fails[name] >= self.cfg.alert_after_failures and name not in self.alerted:
                    log.warning("problem: %s", problem)
                    if await self.send("Yoto bridge problem", problem, event_type="degraded",
                                       subsystem=SUBSYSTEMS.get(name, name), trigger_count=self.fails[name]):
                        self.alerted.add(name)
            else:
                if name in self.alerted:
                    await self.send("Yoto bridge OK again", f"Fixed: {self.messages.get(name, name)}",
                                    priority="default", tags="white_check_mark", event_type="recovered",
                                    subsystem=SUBSYSTEMS.get(name, name))
                    self.alerted.discard(name)
                self.fails.pop(name, None)
                self.messages.pop(name, None)

    async def run(self):
        await asyncio.sleep(60)                     # let Yoto and MQTT connect after a restart
        while True:
            await self.check_all()
            await asyncio.sleep(max(1.0, self.cfg.alert_check_minutes * 60))
