"""Claude as a pixel artist: a 16x16 icon for a chapter (or a whole book), drawn as a grid of palette
indices that the bridge turns into pixels itself. Claude has no image model; it writes the grid as
structured JSON, which suits a 16x16 display well (every pixel is chosen, nothing is shrunk).

What Claude gets: the book's title, author, series and description, the chapter's number and title,
and (when a transcribe service is set up) the opening of the chapter. Nothing else leaves the bridge.
The key is the owner's own Anthropic API key, set on the phone page or in config.yaml.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:          # optional: the bridge runs without it
    anthropic = None

MODELS = {                   # id -> label shown on the page (first-party prices per million tokens)
    "claude-opus-5-5": "Claude Opus 5.5 (best pictures; about $4 in / $20 out)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 ($2 in / $10 out)",
    "claude-haiku-5-5": "Claude Haiku 5.5 (cheapest; $0.10 in / $0.50 out)",
}
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
MAX_COLOURS = 8

SYSTEM = """You draw icons for the 16x16 pixel display of a young child's audiobook player.

Draw ONE bold, simple, instantly recognisable picture that shows what the chapter (or book) is about: an
object, animal, place or character, centred, filling most of the 16x16 grid. Think classic 8-bit sprite:
clear silhouette, a 1-pixel darker outline where it helps, flat bright colours, no gradients, no text,
letters or numbers, no border.

The display is black where a pixel is off. Use "." for background (black). Do not rely on dark colours
for important details; they disappear on black.

Answer in the JSON format:
- subject: a few plain words for what you drew
- palette: up to 8 colours as "#rrggbb"
- rows: exactly 16 strings, each exactly 16 characters, top row first; "." is background, a digit 0-7
  is that palette colour."""

KID_SAFE = ("Keep it kid-safe and friendly: never weapons, violence, blood, skulls, scary or grown-up things. "
            "If the chapter is about something like that, draw a gentle object from the story instead.")

SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "palette": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "palette", "rows"],
    "additionalProperties": False,
}


class ClaudeUnavailable(Exception):
    """Overloaded, rate limited or unreachable: try again later."""


@dataclass
class ClaudeSettings:
    key: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "low"           # low thinking effort: a 16x16 drawing needs little reasoning
    url: str = ""                 # another endpoint (tests); empty = Anthropic's API

    @classmethod
    def parse(cls, raw) -> "ClaudeSettings":
        raw = raw or {}
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise SystemExit(f"Unknown ai_pictures.claude keys: {', '.join(sorted(unknown))}")
        return cls(**raw)


def raster(palette: list[str], rows: list[str]) -> bytes:
    """16x16 RGBA from the grid. Anything malformed (short rows, unknown characters, bad colours) is
    background, so a slightly wrong answer still gives a picture."""
    colours = []
    for h in palette[:MAX_COLOURS]:
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(h).strip())
        colours.append(tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else (0, 0, 0))
    out = bytearray()
    for y in range(16):
        row = str(rows[y]) if y < len(rows) else ""
        for x in range(16):
            c = row[x] if x < len(row) else "."
            rgb = colours[int(c)] if c.isdigit() and int(c) < len(colours) else (0, 0, 0)
            out += bytes([*rgb, 255])
    return bytes(out)


def blank(px: bytes) -> bool:
    return not any(px[i] or px[i + 1] or px[i + 2] for i in range(0, len(px), 4))


class ClaudeArtist:
    def __init__(self, settings: ClaudeSettings, timeout: float = 300):
        self.s = settings
        self.timeout = timeout
        self._client = None
        self._client_key = None

    @property
    def available(self) -> bool:
        return anthropic is not None

    def client(self):
        if anthropic is None:
            raise RuntimeError("The anthropic package is not installed in the bridge.")
        if self._client is None or self._client_key != self.s.key:
            kw = {"api_key": self.s.key, "timeout": self.timeout, "max_retries": 2}
            if self.s.url:
                kw["base_url"] = self.s.url
            self._client, self._client_key = anthropic.AsyncAnthropic(**kw), self.s.key
        return self._client

    async def draw(self, about: str, allow_all: bool) -> tuple[str, bytes] | None:
        """(subject, 16x16 RGBA), or None when Claude declined or drew nothing."""
        if not self.s.key:
            raise RuntimeError("No Claude API key set.")
        prompt = f"{about}\n\nDraw the icon.{'' if allow_all else ' ' + KID_SAFE}"
        kw = dict(model=self.s.model, max_tokens=16000, system=SYSTEM,
                  messages=[{"role": "user", "content": prompt}],
                  output_config={"effort": self.s.effort, "format": {"type": "json_schema", "schema": SCHEMA}})
        try:
            if self.s.model in FALLBACK_MODELS:
                # a declined request is retried on a fallback model chosen by Anthropic, in the same call
                resp = await self.client().beta.messages.create(
                    **kw, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                resp = await self.client().messages.create(**kw)
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
            raise ClaudeUnavailable(f"Claude: {type(e).__name__}") from e
        except anthropic.AuthenticationError as e:
            raise RuntimeError("Claude refused the API key; check it on the Cards tab.") from e
        except anthropic.PermissionDeniedError as e:
            raise RuntimeError(f"Claude: this API key may not use {self.s.model}.") from e
        except anthropic.BadRequestError as e:
            raise RuntimeError(f"Claude: {e.message[:200]}") from e
        if resp.stop_reason == "refusal":
            log.info("Claude declined to draw: %s", getattr(resp.stop_details, "category", None))
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("Claude's drawing was not JSON (stop_reason %s)", resp.stop_reason)
            return None
        px = raster(data.get("palette") or [], data.get("rows") or [])
        if blank(px):
            return None
        return str(data.get("subject") or ""), px
