"""Configuration: a YAML file (default /data/config.yaml) with environment-variable overrides.

Every setting that depends on an unverified Yoto behaviour is a named option here, so a
test result changes one line of config instead of code. See README "Unverified points".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DeviceConfig:
    name: str = ""
    # Optional: a different ABS user (API key) for this player, e.g. one per child.
    abs_api_key: str = ""
    # Optional: the player's LAN IP. Lets a card tap be matched to this player before its first MQTT
    # event arrives. Useless behind a proxy or tunnel (every request then comes from the same address);
    # without it the player is recognised from its events instead.
    ip: str = ""


@dataclass
class Config:
    abs_url: str = "http://audiobookshelf:80"
    abs_api_key: str = ""
    # How the Yoto players reach this bridge. Use an IP address, not a .local/.lan name:
    # the players may not resolve mDNS or your router's local DNS.
    public_url: str = "http://192.168.1.20:8090"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    data_dir: str = "/data"

    yoto_client_id: str = ""
    yoto_api: str = "https://api.yotoplay.com"
    yoto_login: str = "https://login.yotoplay.com"
    # Where Yoto sends the browser after sign-in; must be listed under Allowed Callback URLs in
    # dashboard.yoto.dev. Empty = public_url + /s/yoto-callback (public wherever /s/ is published).
    # Alternative: http://127.0.0.1:8787/callback, then paste the address of the page it lands on.
    yoto_redirect_uri: str = ""

    # Audio the Yoto receives. Always constant-bitrate MP3 so byte offsets map to seconds.
    bitrate_kbps: int = 64
    channels: int = 1
    sample_rate: int = 44100

    # UNVERIFIED POINT (b): how resume works.
    #   "server"     - the bridge starts the audio at the ABS position when the player asks for
    #                  the stream. Does not need secondsIn or seeking to work on stream tracks.
    #   "seconds_in" - the stream is the whole book; resume is done by sending MQTT card/start
    #                  with secondsIn. Use only if test (b) shows the player seeks streams.
    resume_mode: str = "server"
    # UNVERIFIED: whether MQTT `position` for a stream track counts from where this stream
    # started ("relative") or from the start of the book ("absolute"). In server mode it is
    # almost certainly relative; in seconds_in mode, absolute.
    event_position: str = ""
    # UNVERIFIED: format of the `uri` in card/start. yoto-api (used by Home Assistant) sends this.
    card_uri_format: str = "https://yoto.io/{card_id}"

    rewind_seconds: int = 5           # back up a little on every resume, like most audiobook apps
    player_buffer_seconds: int = 20   # used only without MQTT (no position or pause events)
    sync_interval: int = 15           # seconds between progress writes to ABS
    # Faster card start: keep the first seconds of every card's audio, from where ABS has it, encoded
    # in advance, so a tap sends audio at once instead of waiting for ffmpeg. 0 = off.
    warm_start_seconds: int = 30
    warm_refresh_seconds: int = 300   # how often to re-check ABS positions (listening on the phone moves them)
    chapter_tracks: bool = True       # one Yoto track per chapter, so the player's skip buttons move by chapter
    part_seconds: int = 1800          # track length for books without chapters
    chapter_icons: bool = True        # cover + chapter number on the Yoto's pixel display
    offline_hours: float = 5          # how much of the book "Make offline" copies (Yoto allows 5 h per card)
    offline_track_seconds: int = 3300  # longest uploaded track (Yoto allows 60 min); chapters are split to fit
    end_grace_seconds: int = 45       # wait this long after the stream closes before closing the ABS session
    identify_wait_seconds: float = 2  # per-player ABS users: how long a card tap waits for the player's event

    # Push alerts when something breaks (see README "Alerts"). Empty ntfy_url = off.
    ntfy_url: str = ""                # e.g. https://ntfy.sh/<a-long-random-topic-name>
    ntfy_token: str = ""              # only for a protected topic on your own ntfy server
    alert_check_minutes: float = 5
    alert_after_failures: int = 2     # checks in a row before alerting, so a quick ABS restart is ignored
    alert_check_public: bool = True   # also check that public_url reaches the bridge from outside

    # Chapters tab: Audible store to take chapter lists from (us, uk, ca, au, de, fr, it, es, jp, in), and an
    # ABS API key of a user allowed to edit books (Update permission). Empty = abs_api_key, which works
    # only if that user may edit books.
    audible_region: str = "us"
    chapters_api_key: str = ""

    library_ids: list[str] = field(default_factory=list)  # empty = all book libraries the ABS user can see
    ui_pin: str = ""                  # optional PIN for the web page's card-admin actions
    devices: dict[str, DeviceConfig] = field(default_factory=dict)  # Yoto deviceId -> settings

    @property
    def byterate(self) -> int:
        return self.bitrate_kbps * 1000 // 8

    @property
    def position_mode(self) -> str:
        if self.event_position:
            return self.event_position
        return "absolute" if self.resume_mode == "seconds_in" else "relative"

    @property
    def per_player_users(self) -> bool:
        """True if some player uses a different ABS user from the default one."""
        return any(d.abs_api_key and d.abs_api_key != self.abs_api_key for d in self.devices.values())

    def abs_key_for(self, device_id: str | None = None, client_ip: str | None = None) -> str:
        for dev_id, dev in self.devices.items():
            if dev.abs_api_key and ((device_id and dev_id == device_id) or (client_ip and dev.ip == client_ip)):
                return dev.abs_api_key
        return self.abs_api_key

    def device_for_ip(self, client_ip: str | None) -> str | None:
        for dev_id, dev in self.devices.items():
            if client_ip and dev.ip == client_ip:
                return dev_id
        return None


_ENV = {
    "ABS_URL": "abs_url", "ABS_API_KEY": "abs_api_key", "PUBLIC_URL": "public_url",
    "LISTEN_PORT": "listen_port", "DATA_DIR": "data_dir", "YOTO_CLIENT_ID": "yoto_client_id",
    "YOTO_REDIRECT_URI": "yoto_redirect_uri", "RESUME_MODE": "resume_mode", "BITRATE_KBPS": "bitrate_kbps", "UI_PIN": "ui_pin",
    "NTFY_URL": "ntfy_url", "NTFY_TOKEN": "ntfy_token",
}


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("BRIDGE_CONFIG", "/data/config.yaml")
    raw: dict = {}
    if Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    for env, key in _ENV.items():
        if os.environ.get(env):
            raw[key] = os.environ[env]
    devices = {k: DeviceConfig(**(v or {})) for k, v in (raw.pop("devices", None) or {}).items()}
    known = Config.__dataclass_fields__
    unknown = set(raw) - set(known)
    if unknown:
        raise SystemExit(f"Unknown config keys: {', '.join(sorted(unknown))}")
    cfg = Config(**raw, devices=devices)
    for name in ("listen_port", "bitrate_kbps", "channels", "sample_rate", "rewind_seconds",
                 "player_buffer_seconds", "sync_interval", "end_grace_seconds", "offline_track_seconds",
                 "alert_after_failures", "warm_start_seconds", "warm_refresh_seconds", "part_seconds"):
        setattr(cfg, name, int(getattr(cfg, name)))
    cfg.identify_wait_seconds = float(cfg.identify_wait_seconds)
    cfg.alert_check_minutes = float(cfg.alert_check_minutes)
    cfg.abs_url = cfg.abs_url.rstrip("/")
    cfg.public_url = cfg.public_url.rstrip("/")
    if cfg.resume_mode not in ("server", "seconds_in"):
        raise SystemExit("resume_mode must be 'server' or 'seconds_in'")
    return cfg
