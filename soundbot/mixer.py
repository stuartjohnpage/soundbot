import struct

import discord

# discord.py PCM: 48kHz, stereo, 16-bit signed LE, 20ms frames
FRAME_SIZE = 3840  # 48000 * 2 * 2 * 0.02
SAMPLES_PER_FRAME = FRAME_SIZE // 2
SILENCE = b"\x00" * FRAME_SIZE


class MixerSource(discord.AudioSource):
    """Audio source that mixes multiple sources into a single stream.

    Returns silence when no sources are active (keeps the player alive).
    Returns empty bytes only when explicitly stopped.
    """

    def __init__(self, volume: float = 1.0) -> None:
        self._sources: list = []
        self._stopped: bool = False
        # Applied sample-wise in read() before clipping. Moved out of the
        # per-press `ffmpeg -filter:a volume=...` path so playback can skip
        # ffmpeg entirely — see soundbot/pcm_cache.py.
        self.volume: float = volume

    def add(self, source) -> None:
        self._sources.append(source)

    def stop(self) -> None:
        """Signal end-of-stream. read() will return b'' after this."""
        self._stopped = True

    def reset(self) -> None:
        """Clear stopped state so the mixer can be reused."""
        self._stopped = False

    def read(self) -> bytes:
        if self._stopped:
            return b""

        if not self._sources:
            return SILENCE

        mixed = [0] * SAMPLES_PER_FRAME
        active = []

        for source in self._sources:
            data = source.read()
            if not data:
                if hasattr(source, "cleanup"):
                    source.cleanup()
                continue
            # Pad short frames with zeros
            if len(data) < FRAME_SIZE:
                data = data + b"\x00" * (FRAME_SIZE - len(data))
            active.append(source)
            samples = struct.unpack(f"<{SAMPLES_PER_FRAME}h", data)
            for i in range(SAMPLES_PER_FRAME):
                mixed[i] += samples[i]

        self._sources = active

        if not active:
            return SILENCE

        # Apply volume then clip to int16 range. int(x * 1.0) == x for int
        # x, so volume=1.0 is effectively a pass-through.
        volume = self.volume
        for i in range(SAMPLES_PER_FRAME):
            mixed[i] = max(-32768, min(32767, int(mixed[i] * volume)))

        return struct.pack(f"<{SAMPLES_PER_FRAME}h", *mixed)

    def cleanup(self) -> None:
        for source in self._sources:
            if hasattr(source, "cleanup"):
                source.cleanup()
        self._sources.clear()
        self._stopped = True
