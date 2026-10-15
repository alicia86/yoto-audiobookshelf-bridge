"""Chapter theme icons: a 16x16 icon from Yoto's own public icon library (yoto.dev "get public icons",
GET /media/displayIcons/user/yoto) whose title or tags match words of the chapter title. Chapters
called "Chapter 7" match nothing; the bridge then draws its default icon instead.

Kid-safe by default: icons whose title or tags name a weapon or something gory or grown-up are never
picked unless the owner allows every icon (setting "icon_allow_all").
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

REFRESH = 7 * 24 * 3600          # the library changes rarely

STOP = {
    "the", "and", "for", "with", "from", "into", "onto", "chapter", "part", "book", "volume", "track",
    "intro", "introduction", "prologue", "epilogue", "afterword", "foreword", "end", "credits", "opening",
    "closing", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "twenty", "thirty", "first", "second", "third", "last", "next", "new", "old", "big", "little",
    "who", "what", "when", "where", "why", "how", "this", "that", "there", "here", "their", "they", "them",
    "his", "her", "its", "our", "your", "you", "are", "was", "were", "has", "have", "had", "not", "but",
    "all", "out", "off", "over", "under", "about", "again", "back", "down", "just", "very", "too", "time",
    "day", "way", "get", "got", "goes", "going", "come", "comes", "came", "make", "made", "some", "more",
}

# Never picked unless every icon is allowed: weapons, gore, scary or grown-up things.
UNSAFE = {
    "weapon", "gun", "pistol", "rifle", "shotgun", "revolver", "bullet", "ammo", "sword", "knife", "dagger",
    "blade", "axe", "spear", "bomb", "grenade", "dynamite", "explosive", "cannon", "missile", "tank",
    "crossbow", "skull", "blood", "bloody", "gore", "zombie", "corpse", "coffin", "grave", "gravestone",
    "tombstone", "devil", "demon", "poison", "cigarette", "smoking", "beer", "wine", "alcohol", "cocktail",
    "drunk", "kill", "death", "dead", "war", "army", "soldier", "battle", "fight", "punch",
}


def words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if len(w) < 3 or w in STOP:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith(("ches", "shes", "xes", "sses")):
            w = w[:-2]
        elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
            w = w[:-1]
        if w not in STOP:
            out.append(w)
    return out


class IconLibrary:
    """Yoto's public icons, fetched once a week and kept in <data_dir>/yoto_icons.json."""

    def __init__(self, yoto, data_dir: str):
        self.yoto = yoto
        self.file = Path(data_dir) / "yoto_icons.json"
        self.icons: list[dict] = []
        self.fetched = 0.0
        self._png: dict[str, bytes] = {}
        if self.file.exists():
            try:
                raw = json.loads(self.file.read_text())
                self.icons, self.fetched = raw.get("icons", []), float(raw.get("fetched", 0))
            except (OSError, ValueError):
                pass
        self._index()

    def _index(self):
        self._entries = []
        for ic in self.icons:
            title, tags = words(ic.get("title", "")), set()
            for t in ic.get("publicTags") or []:
                tags.update(words(t))
            unsafe = bool((set(title) | tags) & UNSAFE)
            self._entries.append((ic, set(title), tags, unsafe))

    async def load(self) -> bool:
        if self.icons and time.time() - self.fetched < REFRESH:
            return True
        try:
            r = await self.yoto._req("GET", "/media/displayIcons/user/yoto")
        except Exception as e:
            log.warning("Yoto icon library unavailable: %s", e)
            return bool(self.icons)
        icons = [{k: ic.get(k) for k in ("mediaId", "title", "publicTags", "url")}
                 for ic in r.get("displayIcons", []) if ic.get("mediaId") and ic.get("url")]
        if icons:
            self.icons, self.fetched = icons, time.time()
            self._index()
            try:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                self.file.write_text(json.dumps({"fetched": self.fetched, "icons": icons}))
            except OSError as e:
                log.warning("could not keep the Yoto icon library: %s", e)
            log.info("Yoto icon library: %d icons", len(icons))
        return bool(self.icons)

    def match(self, title: str, allow_all: bool = False) -> dict | None:
        """The icon that best fits a chapter title, or None. A word in the icon's title counts double a
        word in its tags; ties go to the icon with the shorter title (the plainer picture)."""
        found = self.matches(title, allow_all, 1)
        return found[0] if found else None

    def matches(self, text: str, allow_all: bool = False, n: int = 3) -> list[dict]:
        """The n icons that fit text best, best first (see match)."""
        want = set(words(text))
        if not want:
            return []
        scored = []
        for ic, tw, tags, unsafe in self._entries:
            if unsafe and not allow_all:
                continue
            score = sum(2 if w in tw else 1 if w in tags else 0 for w in want)
            if score:
                scored.append(((-score, len(ic.get("title") or ""), ic["mediaId"]), ic))
        return [ic for _, ic in sorted(scored, key=lambda x: x[0])[:n]]

    async def png(self, icon: dict) -> bytes | None:
        mid = icon["mediaId"]
        if mid not in self._png:
            try:
                async with self.yoto.http.get(icon["url"]) as r:
                    if r.status >= 400:
                        return None
                    self._png[mid] = await r.read()
            except Exception as e:
                log.warning("Yoto icon %s unavailable: %s", mid, e)
                return None
        return self._png[mid]
