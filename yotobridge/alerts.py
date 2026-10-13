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
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

import aiohttp

from .abs_client import ABSError
from .yoto import YotoNotLoggedIn

log = logging.getLogger(__name__)


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

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.ntfy_url)

    @property
    def problems(self) -> list[str]:
        """Current problems that have failed often enough to count, for the phone page."""
        return [self.messages[k] for k in self.messages if self.fails.get(k, 0) >= self.cfg.alert_after_failures]

    # ---------------- sending ----------------

    async def send(self, title: str, message: str, priority: str = "high", tags: str = "warning") -> bool:
        if not self.enabled:
            log.info("alert (ntfy_url not set, not sent): %s: %s", title, message)
            return False
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
        log.info("alert sent: %s: %s", title, message)
        return True

    def play_failed(self, card: str, why: str):
        """A card was tapped and the bridge could not play it. Sent at once, not repeated for 10 minutes."""
        msg = f"{card}: {why}"
        if time.time() - self._recent.get(msg, 0) < self.PLAY_FAILURE_REPEAT:
            return
        self._recent[msg] = time.time()
        asyncio.get_running_loop().create_task(self.send("Yoto card could not play", msg))

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
                    if await self.send("Yoto bridge problem", problem):
                        self.alerted.add(name)
            else:
                if name in self.alerted:
                    await self.send("Yoto bridge OK again", f"Fixed: {self.messages.get(name, name)}",
                                    priority="default", tags="white_check_mark")
                    self.alerted.discard(name)
                self.fails.pop(name, None)
                self.messages.pop(name, None)

    async def run(self):
        await asyncio.sleep(60)                     # let Yoto and MQTT connect after a restart
        while True:
            await self.check_all()
            await asyncio.sleep(max(1.0, self.cfg.alert_check_minutes * 60))
