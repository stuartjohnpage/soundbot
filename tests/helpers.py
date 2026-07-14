"""Shared test fixtures/utilities (not a test module).

Lives here so test files don't import helpers from each other —
renaming one test module must not break another.
"""
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

has_ffmpeg = shutil.which("ffprobe") is not None
skip_no_ffmpeg = pytest.mark.skipif(
    not has_ffmpeg, reason="FFmpeg/ffprobe not installed"
)


def make_wav(path: Path, duration: float = 1.0, amplitude: float = 0.9) -> Path:
    """Write a mono 48kHz sine-tone WAV. Loud by default (~-3 LUFS) so
    upload-time normalization has something to attenuate."""
    rate = 48000
    n_frames = int(rate * duration)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n_frames):
            sample = int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))
    return path


def make_mp4(path: Path, duration: float = 1.0) -> Path:
    """Render a tiny real video (test pattern + sine audio) with ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path
