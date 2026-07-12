import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from soundbot.audio import (
    extract_audio,
    get_duration,
    has_video_stream,
    measure_loudness,
    normalize_loudness,
    validate_sound,
)

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


@pytest.fixture()
def loud_wav(tmp_path):
    """Generate a 2-second sine well above the -16 LUFS target (~-7 LUFS).

    lavfi's sine source is NOT full-scale — it lands around -21.8 LUFS,
    i.e. quieter than the target — so tests that need attenuation to
    actually fire must boost it first.
    """
    out = tmp_path / "loud.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2",
            "-af", "volume=15dB",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


@pytest.fixture()
def quiet_wav(tmp_path):
    """Generate a 2-second sine wave well below any sane loudness target."""
    out = tmp_path / "quiet.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2",
            "-af", "volume=-30dB",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


@_skip_no_ffmpeg
class TestMeasureLoudness:
    def test_default_sine_measures_expected_level(self, short_wav):
        lufs, true_peak = measure_loudness(short_wav)
        # lavfi's default sine measures ~-21.8 LUFS / -18.1 dBFS true peak.
        assert -25.0 <= lufs <= -18.0
        assert -21.0 <= true_peak <= -15.0

    def test_quiet_file_measures_quiet(self, quiet_wav, short_wav):
        quiet_lufs, _ = measure_loudness(quiet_wav)
        base_lufs, _ = measure_loudness(short_wav)
        assert quiet_lufs < base_lufs - 25

    def test_non_audio_raises(self, tmp_path):
        bad = tmp_path / "junk.mp3"
        bad.write_bytes(b"not audio")
        with pytest.raises(ValueError):
            measure_loudness(bad)


class TestMeasureLoudnessTimeout:
    """No ffmpeg required."""

    def test_timeout_raises_valueerror(self, tmp_path):
        fake_file = tmp_path / "stuck.mp3"
        fake_file.write_bytes(b"fake")
        with patch("soundbot.audio.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)
            with pytest.raises(ValueError, match="timed out"):
                measure_loudness(fake_file)


@_skip_no_ffmpeg
class TestNormalizeLoudness:
    def test_loud_file_attenuated_to_target(self, loud_wav):
        gain = normalize_loudness(loud_wav, target_lufs=-16.0)
        assert gain is not None
        assert gain < 0  # attenuation only, never boost
        lufs, _ = measure_loudness(loud_wav)
        assert abs(lufs - (-16.0)) <= 1.5
        # Temp file from the atomic-replace dance must not survive.
        assert list(loud_wav.parent.glob("*__norm_tmp__*")) == []

    def test_quiet_file_left_untouched(self, quiet_wav):
        before = quiet_wav.read_bytes()
        assert normalize_loudness(quiet_wav, target_lufs=-16.0) is None
        assert quiet_wav.read_bytes() == before

    def test_file_within_threshold_left_untouched(self, loud_wav):
        # Normalize once, then re-normalizing to the same target must be a
        # no-op — the file is now within the skip threshold of the target.
        assert normalize_loudness(loud_wav, target_lufs=-16.0) is not None
        before = loud_wav.read_bytes()
        assert normalize_loudness(loud_wav, target_lufs=-16.0) is None
        assert loud_wav.read_bytes() == before


class TestNormalizeLoudnessNoFfmpeg:
    """No ffmpeg required."""

    def test_unsupported_extension_raises(self, tmp_path):
        f = tmp_path / "sound.aiff"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="unsupported format"):
            normalize_loudness(f, target_lufs=-16.0)

    def test_encode_args_cover_every_tracked_extension(self):
        """Pin _ENCODE_ARGS ⊇ store._AUDIO_EXTS so a new tracked format
        can't silently become un-normalizable."""
        from soundbot.audio import _ENCODE_ARGS
        from soundbot.store import _AUDIO_EXTS

        assert set(_ENCODE_ARGS) >= _AUDIO_EXTS


@_skip_no_ffmpeg
class TestNormalizeLoudnessReplaceFailure:
    def test_locked_destination_raises_valueerror_and_cleans_up(self, loud_wav):
        """On Windows the destination can be transiently locked (antivirus,
        indexer) — os.replace raising must surface as the documented
        ValueError, leave the original intact, and not leak the temp file."""
        before = loud_wav.read_bytes()
        with patch("soundbot.audio.os.replace", side_effect=PermissionError("locked")):
            with pytest.raises(ValueError, match="Failed to normalize"):
                normalize_loudness(loud_wav, target_lufs=-16.0)
        assert loud_wav.read_bytes() == before
        assert list(loud_wav.parent.glob("*__norm_tmp__*")) == []
