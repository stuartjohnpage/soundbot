import struct

# discord.py PCM: 48kHz, stereo, 16-bit signed LE, 20ms frames
FRAME_SIZE = 3840  # 48000 * 2 * 2 * 0.02
SAMPLES_PER_FRAME = FRAME_SIZE // 2


class MixerSource:
    """Audio source that mixes multiple sources into a single stream."""

    def __init__(self) -> None:
        self._sources: list = []

    def add(self, source) -> None:
        self._sources.append(source)

    def read(self) -> bytes:
        if not self._sources:
            return b""

        mixed = [0] * SAMPLES_PER_FRAME
        active = []

        for source in self._sources:
            data = source.read()
            if not data:
                if hasattr(source, "cleanup"):
                    source.cleanup()
                continue
            active.append(source)
            samples = struct.unpack(f"<{SAMPLES_PER_FRAME}h", data)
            for i in range(SAMPLES_PER_FRAME):
                mixed[i] += samples[i]

        self._sources = active

        if not active:
            return b""

        # Clip to int16 range
        for i in range(SAMPLES_PER_FRAME):
            mixed[i] = max(-32768, min(32767, mixed[i]))

        return struct.pack(f"<{SAMPLES_PER_FRAME}h", *mixed)

    def cleanup(self) -> None:
        for source in self._sources:
            if hasattr(source, "cleanup"):
                source.cleanup()
        self._sources.clear()
