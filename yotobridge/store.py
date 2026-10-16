"""Persistent bridge state in <data_dir>/state.json: which Yoto playlist (card) points at which slot,
and which ABS book each slot currently plays.

A "slot" is a stable stream URL, http://<bridge>/s/<slot>.mp3. The Yoto playlist stores that URL once;
which book it plays is decided here, at request time. So re-pointing a card at another book is a
local change, with no Yoto app or API involved.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Slot:
    slot: str
    kind: str                      # "card" (a physical MYO card) or "remote" (phone-to-Yoto, one per player)
    item_id: str = ""              # ABS library item currently assigned
    title: str = ""                # book title, for display
    yoto_card_id: str = ""         # the Yoto playlist (content) id that streams this slot
    yoto_title: str = ""
    device_id: str = ""            # remote slots: which player this belongs to
    stream_url: str = ""           # the URL last written into the Yoto playlist
    series_id: str = ""            # series/collection cards: the ABS series or collection id (item_id = current book)
    group_kind: str = ""           # "collection", or "" / "series" for a series
    series_name: str = ""
    series_library: str = ""
    icon_url: str = ""             # the chapter-icon URL last written into the Yoto playlist
    layout_id: str = ""            # chapter tracks: the track layout currently on the Yoto playlist
    layouts: dict = field(default_factory=dict)  # layout id -> list of segments (see chapters.py); last few kept
    offline: dict = field(default_factory=dict)  # offline copy on this card (see offline.py); empty = streaming
    speed: float = 0.0             # playback speed for this card; 0 = the player's speed
    icon_style: str = ""           # pixel icon style for this card (icons.STYLES); "" = the default style
    icon_custom: str = ""          # hash of this card's own picture (custom style), kept in <data_dir>/icons/
    icon_chapters: dict = field(default_factory=dict)  # "<item>:<chapter>" -> hash of that chapter's own picture
    own_place: bool = False        # keeps its own place: listening is never written to ABS (localprogress.py)


@dataclass
class State:
    slots: dict[str, Slot] = field(default_factory=dict)
    icons: dict[str, str] = field(default_factory=dict)   # "<item>:<chapter>" -> uploaded Yoto icon ("yoto:#…")
    speeds: dict[str, float] = field(default_factory=dict)  # Yoto deviceId -> playback speed (absent = 1.0)
    icon_style: str = ""           # default pixel icon style of every card ("" = icons.DEFAULT_STYLE)
    icon_allow_all: bool = False   # chapter theme icons: also pick weapons and other not-for-kids icons


class Store:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "state.json"
        self.state = State()
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.state.slots = {k: Slot(**v) for k, v in raw.get("slots", {}).items()}
            self.state.icons = raw.get("icons", {})
            self.state.speeds = raw.get("speeds", {})
            self.state.icon_style = raw.get("icon_style", "")
            self.state.icon_allow_all = bool(raw.get("icon_allow_all", False))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"slots": {k: asdict(v) for k, v in self.state.slots.items()}, "icons": self.state.icons, "speeds": self.state.speeds,
                "icon_style": self.state.icon_style, "icon_allow_all": self.state.icon_allow_all}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, slot: str) -> Slot | None:
        return self.state.slots.get(slot)

    def by_yoto_card(self, card_id: str) -> Slot | None:
        return next((s for s in self.state.slots.values() if s.yoto_card_id and s.yoto_card_id == card_id), None)

    def remote_for(self, device_id: str) -> Slot | None:
        return next((s for s in self.state.slots.values() if s.kind == "remote" and s.device_id == device_id), None)

    def cards(self) -> list[Slot]:
        return [s for s in self.state.slots.values() if s.kind == "card"]

    def new_slot(self, kind: str, **kw) -> Slot:
        # Unguessable, because the stream endpoint has no other auth (the Yoto cannot send headers).
        s = Slot(slot=secrets.token_urlsafe(9), kind=kind, **kw)
        self.state.slots[s.slot] = s
        self.save()
        return s

    def delete(self, slot: str):
        self.state.slots.pop(slot, None)
        self.save()
