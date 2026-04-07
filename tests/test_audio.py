import shutil
import subprocess
from pathlib import Path

import pytest

from soundbot.audio import get_duration, validate_sound

_has_ffmpeg = shutil.which("ffprobe") is not None

pytestmark = pytest.mark.skipif(not _has_ffmpeg, reason="FFmpeg/ffprobe not installed")


@pytest.fixture()
def short_wav(tmp_path):
    """Generate a 2-second 440Hz sine wave."""
    out = tmp_path / "short.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


@pytest.fixture()
def long_wav(tmp_path):
    """Generate a 10-second 440Hz sine wave."""
    out = tmp_path / "long.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=10",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


class TestGetDuration:
    def test_returns_correct_duration(self, short_wav):
        duration = get_duration(short_wav)
        assert 1.9 <= duration <= 2.1


class TestValidateSound:
    def test_valid_sound_passes(self, short_wav):
        # Should not raise
        validate_sound(short_wav, max_duration=6.0)

    def test_rejects_exceeding_max_duration(self, long_wav):
        with pytest.raises(ValueError, match="duration"):
            validate_sound(long_wav, max_duration=6.0)

    def test_rejects_non_audio_file(self, tmp_path):
        bad_file = tmp_path / "not_audio.txt"
        bad_file.write_text("this is not audio")
        with pytest.raises(ValueError):
            validate_sound(bad_file, max_duration=6.0)
