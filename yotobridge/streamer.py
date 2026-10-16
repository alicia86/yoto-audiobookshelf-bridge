"""Turn an ABS book (one or many files, any format ffmpeg reads) into a constant-bitrate MP3 stream
starting at any second.

Constant bitrate is the point: N bytes of output are exactly N / byterate seconds, so an HTTP
Range request maps to a time offset, and "bytes delivered" bounds how far the player can have got.
Each ABS track is encoded by its own ffmpeg process and the MP3 frames are concatenated; MP3 frames
are self-contained, so the joins are seamless enough for speech.

Playback speed is done here, with ffmpeg's atempo (faster or slower, same pitch). At speed s, one second
of book is 1/s seconds of MP3, so one book second is byterate / s bytes: callers map bytes to book time
with that rate instead of the plain byterate.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator

log = logging.getLogger(__name__)
CHUNK = 16 * 1024


class SourceError(Exception):
    """The audio source stopped sending part-way through (Audiobookshelf went away, say)."""


@dataclass
class Track:
    url: str
    start: float      # offset of this file within the book, seconds
    duration: float


class Encoder:
    def __init__(self, bitrate_kbps: int = 64, channels: int = 1, sample_rate: int = 44100, ffmpeg: str = "ffmpeg"):
        self.bitrate = bitrate_kbps
        self.channels = channels
        self.rate = sample_rate
        self.ffmpeg = ffmpeg
        self.byterate = bitrate_kbps * 1000 // 8
        self._silence: bytes | None = None

    def _out_args(self) -> list[str]:
        return ["-vn", "-ac", str(self.channels), "-ar", str(self.rate), "-c:a", "libmp3lame",
                "-b:a", f"{self.bitrate}k", "-write_xing", "0", "-id3v2_version", "0",
                "-map_metadata", "-1", "-f", "mp3", "pipe:1"]

    async def silence(self) -> bytes:
        """One second of silent MP3 at the stream's settings, used to pad to the promised length."""
        if self._silence is None:
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-t", "1",
                "-i", f"anullsrc=r={self.rate}:cl={'mono' if self.channels == 1 else 'stereo'}",
                *self._out_args(), stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            self._silence = out or b"\x00" * self.byterate
        return self._silence

    async def stream(self, tracks: list[Track], start: float, end: float, auth_header: str,
                     speed: float = 1.0, strict: bool = False) -> AsyncIterator[bytes]:
        """Yield MP3 bytes covering book time [start, end), played at `speed`. With `strict`, a file
        ffmpeg could not read to the end raises SourceError instead of leaving a gap (an offline copy
        would otherwise upload silence where the audio should be)."""
        for t in tracks:
            t_end = t.start + t.duration
            if t_end <= start or t.start >= end:
                continue
            seek = max(0.0, start - t.start)
            length = min(t_end, end) - max(t.start, start)
            args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
            if t.url.startswith("http"):
                args += ["-headers", f"Authorization: {auth_header}\r\n",
                         "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            if seek > 0.05:
                args += ["-ss", f"{seek:.3f}"]
            args += ["-i", t.url, "-t", f"{length / speed:.3f}"]
            if abs(speed - 1.0) > 0.001:
                args += ["-af", f"atempo={speed:g}"]
            args += self._out_args()
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            got = 0
            try:
                while True:
                    chunk = await proc.stdout.read(CHUNK)
                    if not chunk:
                        break
                    got += len(chunk)
                    yield chunk
                await proc.wait()
                if proc.returncode:
                    err = (await proc.stderr.read()).decode(errors="replace")[-400:]
                    log.warning("ffmpeg exited %s on %s: %s", proc.returncode, t.url, err)
                    want = length / speed * self.byterate
                    if strict and got < want - 2 * self.byterate:
                        raise SourceError(f"the audio stopped after {got / self.byterate / 60:.0f} of "
                                          f"{want / self.byterate / 60:.0f} min ({' '.join(err.split())[-120:] or 'ffmpeg failed'})")
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    async def quiet_spots(self, tracks: list[Track], start: float, end: float, auth_header: str,
                          noise_db: int = -35, min_len: float = 0.25) -> list[tuple[float, float]]:
        """Pauses (start, end in book time) in book time [start, end), found by ffmpeg's silencedetect."""
        spots: list[tuple[float, float]] = []
        for t in tracks:
            t_end = t.start + t.duration
            if t_end <= start or t.start >= end:
                continue
            seek = max(0.0, start - t.start)
            length = min(t_end, end) - max(t.start, start)
            args = [self.ffmpeg, "-hide_banner", "-nostdin", "-nostats"]
            if t.url.startswith("http") and auth_header:
                args += ["-headers", f"Authorization: {auth_header}\r\n"]
            if seek > 0.05:
                args += ["-ss", f"{seek:.3f}"]
            args += ["-i", t.url, "-t", f"{length:.3f}", "-vn",
                     "-af", f"silencedetect=noise={noise_db}dB:d={min_len:g}", "-f", "null", "-"]
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            err = (await proc.communicate())[1].decode(errors="replace")
            if proc.returncode:
                raise SourceError(f"could not read the audio around {start:.0f}s: {' '.join(err.split())[-120:]}")
            base = t.start + seek            # the timestamps silencedetect prints start at the seek point
            s0 = None
            for line in err.splitlines():
                if "silence_start:" in line:
                    s0 = float(line.rsplit("silence_start:", 1)[1].split()[0])
                elif "silence_end:" in line and s0 is not None:
                    e0 = float(line.rsplit("silence_end:", 1)[1].split()[0])
                    spots.append((base + max(0.0, s0), base + e0))
                    s0 = None
            if s0 is not None:               # still quiet at the end of the window
                spots.append((base + max(0.0, s0), base + length))
        return spots

    async def exact(self, source: AsyncIterator[bytes], n: int) -> AsyncIterator[bytes]:
        """Deliver exactly n bytes: truncate the encoder's output or pad it with silence.
        The player was promised a Content-Length; a short body looks like a network error."""
        sent = 0
        try:
            async for chunk in source:
                if sent + len(chunk) >= n:
                    yield chunk[: n - sent]
                    sent = n
                    break
                sent += len(chunk)
                yield chunk
        finally:
            await source.aclose()
        if sent < n:
            pad = await self.silence()
            while sent < n:
                piece = pad[: n - sent]
                sent += len(piece)
                yield piece
