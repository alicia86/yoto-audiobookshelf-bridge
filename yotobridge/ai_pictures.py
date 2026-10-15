"""AI chapter pictures: a picture for each chapter of a card, made from what is said in the chapter.

Three steps, each an HTTP service in the OpenAI API format, so any provider or a computer at home can
do them (config "ai_pictures"; see README "AI chapter pictures"):

  transcribe  POST <url>/audio/transcriptions  the first sample_seconds of the chapter -> text
  text        POST <url>/chat/completions      text -> keywords (library mode) or a picture prompt
  image       POST <url>/images/generations    prompt -> picture   (optional; api: ollama uses
                                                Ollama's own POST <url>/api/generate instead)

Without an image service the chapter gets the best-fitting icon from Yoto's own icon library (drawn for
16x16, kid-safe filter as for theme icons), chosen by the text model. With one, a picture is drawn and
shrunk to 16x16. Either way the result is saved as the chapter's own picture (custom style), so it can
be changed by hand like any other.

Steps run one at a time; with unload: true the bridge asks Ollama to drop each model from memory after
its step (a 16 GB computer cannot hold Whisper, a text model and an image model at once). When a
service cannot be reached (a computer asleep), the chapter waits and is tried again.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

import aiohttp

from . import icons
from .icon_themes import UNSAFE, words

log = logging.getLogger(__name__)

KID_SAFE = ("It is for a young child's audiobook player: only friendly, everyday, kid-safe things; never "
            "weapons, violence, blood, scary or grown-up things.")
PICTURE_STYLE = ("simple flat pixel art icon, one bold object centred, bright flat colours, thick shapes, "
                 "plain black background, no text, no border")


@dataclass
class Endpoint:
    url: str = ""
    model: str = ""
    key: str = ""
    api: str = "openai"          # image step only: "openai" or "ollama"
    unload: bool = False         # Ollama: drop the model from memory after the step

    @classmethod
    def parse(cls, raw) -> "Endpoint":
        raw = raw or {}
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise SystemExit(f"Unknown ai_pictures keys: {', '.join(sorted(unknown))}")
        e = cls(**raw)
        e.url = e.url.rstrip("/")
        if e.api not in ("openai", "ollama"):
            raise SystemExit("ai_pictures image api must be 'openai' or 'ollama'")
        return e

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    @property
    def root(self) -> str:
        """The server's root address (an Ollama URL given as .../v1)."""
        return re.sub(r"/v1$", "", self.url)


@dataclass
class AISettings:
    transcribe: Endpoint = field(default_factory=Endpoint)
    text: Endpoint = field(default_factory=Endpoint)
    image: Endpoint = field(default_factory=Endpoint)
    sample_seconds: int = 180       # how much of each chapter is transcribed
    retry_minutes: float = 60       # how long a chapter waits for a service that cannot be reached
    timeout_seconds: float = 600    # one request (a slow computer drawing a picture takes a while)

    @classmethod
    def parse(cls, raw) -> "AISettings":
        raw = dict(raw or {})
        s = cls(**{k: Endpoint.parse(raw.pop(k, None)) for k in ("transcribe", "text", "image")})
        for k in ("sample_seconds", "retry_minutes", "timeout_seconds"):
            if k in raw:
                setattr(s, k, type(getattr(s, k))(raw.pop(k)))
        if raw:
            raise SystemExit(f"Unknown ai_pictures keys: {', '.join(sorted(raw))}")
        return s

    @property
    def enabled(self) -> bool:
        return bool(self.transcribe.url and self.text.url)

    @property
    def mode(self) -> str:
        return "images" if self.image.url else "library"


class Unreachable(Exception):
    """The service could not be reached or is busy: try again later."""


class AIClient:
    def __init__(self, http: aiohttp.ClientSession, s: AISettings):
        self.http, self.s = http, s

    async def _post(self, url: str, ep: Endpoint, **kw) -> dict:
        try:
            async with self.http.post(url, headers=ep.headers, timeout=aiohttp.ClientTimeout(total=self.s.timeout_seconds),
                                      **kw) as r:
                text = await r.text()
                if r.status in (429, 502, 503, 504):
                    raise Unreachable(f"{url} -> {r.status}")
                if r.status >= 400:
                    raise RuntimeError(f"{url} -> {r.status}: {text[:200]}")
                return json.loads(text) if text.strip().startswith(("{", "[")) else {"text": text}
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            raise Unreachable(f"{url}: {e or type(e).__name__}") from e

    async def _unload(self, ep: Endpoint):
        if ep.unload and ep.model:
            try:
                async with self.http.post(f"{ep.root}/api/generate", json={"model": ep.model, "keep_alive": 0},
                                          timeout=aiohttp.ClientTimeout(total=60)):
                    pass
            except Exception as e:                       # never fail a chapter over this
                log.debug("could not unload %s: %s", ep.model, e)

    async def transcribe(self, wav: bytes) -> str:
        ep = self.s.transcribe
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="chapter.wav", content_type="audio/wav")
        form.add_field("model", ep.model or "whisper-1")
        form.add_field("response_format", "json")
        r = await self._post(f"{ep.url}/audio/transcriptions", ep, data=form)
        await self._unload(ep)
        return str(r.get("text") or "").strip()

    async def chat(self, prompt: str, temperature: float = 0.3) -> str:
        ep = self.s.text
        r = await self._post(f"{ep.url}/chat/completions", ep, json={
            "model": ep.model, "temperature": temperature, "stream": False,
            "messages": [{"role": "user", "content": prompt}]})
        await self._unload(ep)
        try:
            return str(r["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected answer from the text model: {str(r)[:200]}")

    async def image(self, prompt: str) -> bytes:
        ep = self.s.image
        if ep.api == "ollama":
            r = await self._post(f"{ep.root}/api/generate", ep, json={
                "model": ep.model, "prompt": prompt, "stream": False, "width": 512, "height": 512})
            b64 = r.get("image") or (r.get("images") or [None])[0]
        else:
            r = await self._post(f"{ep.url}/images/generations", ep, json={
                "model": ep.model, "prompt": prompt, "n": 1, "size": "512x512", "response_format": "b64_json"})
            item = (r.get("data") or [{}])[0]
            b64 = item.get("b64_json")
            if not b64 and item.get("url"):
                async with self.http.get(item["url"], timeout=aiohttp.ClientTimeout(total=120)) as g:
                    data = await g.read()
                await self._unload(ep)
                return data
        await self._unload(ep)
        if not b64:
            raise RuntimeError(f"no picture in the image service's answer: {str(r)[:200]}")
        return base64.b64decode(b64)


async def to_wav(mp3: bytes, ffmpeg: str = "ffmpeg") -> bytes:
    """16 kHz mono WAV: what Whisper works in, and the one format every Whisper server reads."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(mp3)
    if proc.returncode or len(out) < 100:
        raise RuntimeError(f"could not convert the chapter audio: {err.decode(errors='replace')[-200:]}")
    return out


def first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().strip('"').strip()
        if line:
            return re.sub(r"^(prompt|keywords?)\s*:\s*", "", line, flags=re.I)
    return ""


def unsafe(text: str) -> bool:
    return bool(set(words(text)) & UNSAFE)


class AIPictures:
    """One background job per card: a picture for each chapter of the book it is on."""
    RETRY_SLEEP = 60.0

    def __init__(self, br, settings: AISettings):
        self.br, self.s = br, settings
        self.client = AIClient(br.http, settings)
        self.jobs: dict[str, dict] = {}        # slot -> progress, shown on the page
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()            # one chapter at a time across all cards (one computer)

    def status(self, slot_id: str) -> dict | None:
        return self.jobs.get(slot_id)

    def start(self, slot, overwrite: bool = False) -> dict:
        if not self.s.enabled:
            raise RuntimeError("Set ai_pictures (transcribe and text) in config.yaml and restart the bridge first.")
        if slot.slot in self._tasks and not self._tasks[slot.slot].done():
            return self.jobs[slot.slot]
        self.jobs[slot.slot] = {"status": "running", "done": 0, "total": 0, "skipped": 0, "step": "starting",
                                "error": "", "mode": self.s.mode, "item_id": slot.item_id}
        self._tasks[slot.slot] = asyncio.create_task(self._run(slot, overwrite))
        return self.jobs[slot.slot]

    def cancel(self, slot_id: str):
        t = self._tasks.pop(slot_id, None)
        if t:
            t.cancel()
        if slot_id in self.jobs and self.jobs[slot_id]["status"] == "running":
            self.jobs[slot_id].update(status="cancelled", step="")

    async def _run(self, slot, overwrite: bool):
        job = self.jobs[slot.slot]
        try:
            item_id = slot.item_id
            chapters = await self.br.chapters(item_id)
            todo = [(n, c) for n, c in enumerate(chapters, 1)
                    if len(chapters) >= 2 and (overwrite or f"{item_id}:{n}" not in slot.icon_chapters)]
            job["total"] = len(todo)
            if not todo:
                job.update(status="done", step="")
                return
            key = self.br.cfg.abs_key_for(slot.device_id)
            tracks = self.br.pm._tracks(await self.br.abs.item_tracks(item_id, key))
            for n, c in todo:
                job["step"] = f"chapter {n}: {c.get('title') or ''}".strip(": ")
                px = await self._with_retry(job, lambda: self._chapter(item_id, tracks, key, c), n)
                if px:
                    self._save(slot, f"{item_id}:{n}", px)
                else:
                    job["skipped"] += 1
                job["done"] += 1
            job.update(status="done", step="")
            log.info("AI pictures for %s: %d chapters, %d skipped", slot.title, job["done"], job["skipped"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            job.update(status="failed", error=str(e)[:300], step="")
            log.warning("AI pictures for %s failed: %s", slot.title, e)
        finally:
            if job["done"] - job["skipped"] > 0:
                try:
                    await self.br.apply_icon_style([slot])
                except Exception as e:
                    log.warning("could not put the AI pictures on %s: %s", slot.title, e)

    async def _with_retry(self, job: dict, fn, n: int):
        waited = 0.0
        while True:
            try:
                async with self._lock:
                    return await fn()
            except Unreachable as e:
                if waited >= self.s.retry_minutes * 60:
                    raise RuntimeError(f"the AI service could not be reached for {self.s.retry_minutes:g} minutes ({e})")
                job["step"] = f"chapter {n}: waiting for the AI service ({e})"
                await asyncio.sleep(self.RETRY_SLEEP)
                waited += self.RETRY_SLEEP

    def _save(self, slot, key: str, px: bytes):
        path = self.br.custom_icon_path(slot, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(px)
        slot.icon_chapters[key] = hashlib.sha1(px).hexdigest()[:8]
        slot.icon_style = "custom"
        self.br.store.save()

    async def _chapter(self, item_id: str, tracks, key: str, chapter: dict) -> bytes | None:
        """16x16 RGBA for one chapter, or None when nothing kid-safe fits."""
        start = float(chapter.get("start", 0))
        end = float(chapter.get("end") or start + self.s.sample_seconds)
        end = min(end, start + self.s.sample_seconds)
        audio = bytearray()
        async for chunk in self.br.encoder.stream(tracks, start, end, self.br.abs.auth_header(key)):
            audio += chunk
        said = await self.client.transcribe(await to_wav(bytes(audio)))
        title = chapter.get("title") or ""
        if not said and not title:
            return None
        about = f'Chapter title: "{title}"\nThe chapter begins: "{said[:3000]}"'
        allow_all = self.br.store.state.icon_allow_all
        if self.s.mode == "images":
            return await self._draw(about, allow_all)
        return await self._pick(about, allow_all)

    async def _draw(self, about: str, allow_all: bool) -> bytes | None:
        ask = (f"{about}\n\nDescribe ONE simple object or animal that best shows what this chapter is about, "
               f"in at most 8 words, for a tiny icon. {'' if allow_all else KID_SAFE} Answer with the words only.")
        subject = first_line(await self.client.chat(ask))
        if not allow_all and unsafe(subject):
            subject = first_line(await self.client.chat(ask + " Pick something gentler."))
            if unsafe(subject):
                return None
        if not subject:
            return None
        raw = await self.client.image(f"{subject}, {PICTURE_STYLE}")
        return await icons.shrink_cover(raw)

    async def _pick(self, about: str, allow_all: bool) -> bytes | None:
        lib = self.br.icon_library
        if not await lib.load():
            raise RuntimeError("Yoto's icon library could not be loaded (is Yoto connected?)")
        ask = (f"{about}\n\nList 6 single English words (nouns) for simple pictures that show what this chapter "
               f"is about, most fitting first, comma-separated. {'' if allow_all else KID_SAFE} Answer with the words only.")
        keywords = [w.strip() for w in re.split(r"[,\n]", first_line(await self.client.chat(ask))) if w.strip()]
        candidates: list[dict] = []
        for kw in keywords[:6]:
            for ic in lib.matches(kw, allow_all, 3):
                if ic not in candidates:
                    candidates.append(ic)
        candidates = candidates[:12]
        if not candidates:
            return None
        chosen = candidates[0]
        if len(candidates) > 1:
            menu = "\n".join(f"{i}. {ic.get('title')}" for i, ic in enumerate(candidates, 1))
            answer = await self.client.chat(f"{about}\n\nWhich of these icons fits this chapter best?\n{menu}\n\n"
                                            "Answer with the number only.", temperature=0)
            m = re.search(r"\d+", answer)
            if m and 1 <= int(m.group()) <= len(candidates):
                chosen = candidates[int(m.group()) - 1]
        raw = await lib.png(chosen)
        return await icons.shrink_cover(raw) if raw else None
