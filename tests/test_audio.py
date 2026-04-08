import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from soundbot.audio import extract_audio, get_duration, has_video_stream, validate_sound

_has_ffmpeg = shutil.which("ffprobe") is not None
_skip_no_ffmpeg = pytest.mark.skipif(not _has_ffmpeg, reason="FFmpeg/ffprobe not installed")


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


@_skip_no_ffmpeg
class TestGetDuration:
    def test_returns_correct_duration(self, short_wav):
        duration = get_duration(short_wav)
        assert 1.9 <= duration <= 2.1


@_skip_no_ffmpeg
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


@pytest.fixture()
def short_mp4(tmp_path):
    """Generate a 2-second MP4 video with audio."""
    out = tmp_path / "short.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
            "-shortest",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


@pytest.fixture()
def silent_mp4(tmp_path):
    """Generate a 2-second MP4 video with no audio track."""
    out = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2",
            "-an",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


@_skip_no_ffmpeg
class TestHasVideoStream:
    def test_audio_file_has_no_video(self, short_wav):
        assert has_video_stream(short_wav) is False

    def test_video_file_has_video(self, short_mp4):
        assert has_video_stream(short_mp4) is True

    def test_invalid_file_returns_false(self, tmp_path):
        bad = tmp_path / "garbage.bin"
        bad.write_bytes(b"not a media file")
        assert has_video_stream(bad) is False


@_skip_no_ffmpeg
class TestExtractAudio:
    def test_extracts_audio_from_video(self, short_mp4, tmp_path):
        out = tmp_path / "extracted.mp3"
        extract_audio(short_mp4, out)
        assert out.exists()
        duration = get_duration(out)
        assert 1.5 <= duration <= 2.5

    def test_raises_on_video_without_audio(self, silent_mp4, tmp_path):
        out = tmp_path / "fail.mp3"
        with pytest.raises(ValueError, match="no audio"):
            extract_audio(silent_mp4, out)

    def test_raises_on_non_media_file(self, tmp_path):
        bad = tmp_path / "junk.txt"
        bad.write_text("not media")
        out = tmp_path / "fail.mp3"
        with pytest.raises(ValueError):
            extract_audio(bad, out)


class TestFfprobeTimeout:
    """Tests that don't require ffmpeg installed."""

    def test_timeout_expired_raises_valueerror(self, tmp_path):
        fake_file = tmp_path / "stuck.mp3"
        fake_file.write_bytes(b"fake")
        with patch("soundbot.audio.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=10)
            with pytest.raises(ValueError, match="timed out"):
                get_duration(fake_file)
