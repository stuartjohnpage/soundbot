"""In-memory PCM cache to keep ffmpeg off the playback hot path.

The old `_play_sound` path spawned an `ffmpeg` subprocess per press to
decode + apply `-filter:a volume=...`. On Windows that's 100–350ms of
avoidable latency for files that never change on disk. This module
decodes each file exactly once (lazily, on first access) into
48kHz/stereo/s16le bytes and serves those straight to the mixer on
subsequent presses.

Volume lives on the mixer now (see MixerSource.volume) so the cache
doesn't need to be invalidated when `/volume` changes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import discord

from .mixer import FRAME_SIZE


def decode_to_pcm(file_path: str | Path) -> bytes:
    """Decode an audio file to 48kHz stereo s16le PCM bytes via ffmpeg.

    Format matches discord.py's voice pipeline exactly so the output can
    be fed to the mixer with no further conversion. Raises ValueError on
    decode failure (bad file, ffmpeg missing, timeout). The ffmpeg stderr
    tail is included in the exception message so the caller's log line
    names the actual cause (codec missing, unsupported format, etc.)
    rather than an opaque "Failed to decode".
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v", "error",
                "-i", str(file_path),
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        detail = f": {stderr[:200]}" if stderr else ""
        raise ValueError(f"Failed to decode audio: {file_path}{detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Failed to decode audio: {file_path} (timed out)") from exc
    except FileNotFoundError as exc:
        raise ValueError(
            f"Failed to decode audio: {file_path} (ffmpeg not found)"
        ) from exc
    return result.stdout


class PCMCache:
    """Lazy in-memory cache of pre-decoded PCM bytes keyed by file path.

    Threading: `get` calls the decoder synchronously; callers that run
    inside asyncio should wrap with `asyncio.to_thread` so the event
    loop stays responsive while a cold miss is decoding.

    Memory: no eviction. At 48kHz/stereo/s16le the wire format is
    ~192KB/sec, so a typical 2s soundboard clip is ~380KB. A library of
    100 clips fits comfortably in <50MB, which is fine for this bot's
    expected scale. If the library grows past the low thousands, or if
    average clip length jumps (music, long samples), swap this for an
    LRU keyed by byte budget. There is no runtime signal that tells you
    you've hit the wall — watch RSS.
    """

    def __init__(
        self,
        decoder: Callable[[str | Path], bytes] = decode_to_pcm,
    ) -> None:
        self._cache: dict[str, bytes] = {}
        self._decoder = decoder

    def get(self, file_path: str | Path) -> bytes:
        key = str(file_path)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._decoder(file_path)
        self._cache[key] = data
        return data

    def invalidate(self, file_path: str | Path) -> None:
        self._cache.pop(str(file_path), None)

    def clear(self) -> None:
        self._cache.clear()

    def __contains__(self, file_path: str | Path) -> bool:
        return str(file_path) in self._cache


class CachedPCMSource(discord.AudioSource):
    """Memory-backed PCM source. Zero subprocess cost on the hot path.

    Slices the shared bytes into 20ms frames on demand. A partial final
    frame is returned as-is; the mixer pads it to FRAME_SIZE with zeros
    so we don't lose the tail of short sounds.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = self._pos + FRAME_SIZE
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def cleanup(self) -> None:
        pass
