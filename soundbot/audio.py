import json
import os
import re
import subprocess
from pathlib import Path

# ebur128 prints a summary block on stderr; these pull the integrated
# loudness and true peak out of it. Same patterns as scripts/measure_loudness.py.
_INTEGRATED_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+\.\d+) LUFS")
_TRUE_PEAK_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?\d+\.\d+) dBFS")

# Re-encode settings per container for in-place normalization. Covers every
# extension scan_folder tracks (store._AUDIO_EXTS, pinned by test) — but
# /addsound accepts anything ffprobe can read, so an unknown extension
# degrades to an un-normalized upload rather than a rejected one
# (normalize_upload catches the ValueError and keeps the file).
_ENCODE_ARGS: dict[str, list[str]] = {
    ".ogg": ["-c:a", "libopus", "-b:a", "96k", "-vbr", "on"],
    ".opus": ["-c:a", "libopus", "-b:a", "96k", "-vbr", "on"],
    ".webm": ["-c:a", "libopus", "-b:a", "96k", "-vbr", "on"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    ".m4a": ["-c:a", "aac", "-b:a", "128k"],
    ".flac": ["-c:a", "flac"],
    ".wav": ["-c:a", "pcm_s16le"],
}

# Don't bother re-encoding for a trim smaller than this — the lossy
# re-encode would cost more fidelity than the level correction gains.
_SKIP_THRESHOLD_DB = 0.3


def get_duration(file_path: Path) -> float:
    """Return the duration in seconds of an audio file using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Audio file could not be processed (timed out)") from exc
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError(f"Cannot read audio file: {file_path}") from exc

    data = json.loads(result.stdout)
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot determine duration of: {file_path}") from exc


def has_video_stream(file_path: Path) -> bool:
    """Return True if the file contains a video stream."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v",
                "-show_entries", "stream=codec_type",
                "-of", "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    return len(data.get("streams", [])) > 0


def extract_audio(video_path: Path, output_path: Path) -> None:
    """Extract the audio track from a video file.

    Caller must verify the input is a video (via has_video_stream).
    Raises ValueError if the video has no audio track or extraction fails.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        if not data.get("streams"):
            raise ValueError(f"Video has no audio track: {video_path}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Cannot read file: {video_path}") from exc
    except (json.JSONDecodeError, ValueError):
        raise

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-vn",
                "-q:a", "4",
                str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        output_path.unlink(missing_ok=True)
        raise ValueError(f"Failed to extract audio from: {video_path}") from exc


def validate_sound(file_path: Path, max_duration: float) -> None:
    """Raise ValueError if file is not a valid audio file or exceeds max_duration."""
    duration = get_duration(file_path)
    if duration > max_duration:
        raise ValueError(
            f"Sound duration {duration:.1f}s exceeds maximum {max_duration:.1f}s"
        )


def measure_loudness(file_path: Path) -> tuple[float, float]:
    """Return (integrated LUFS, true peak dBFS) of an audio file.

    Raises ValueError if ffmpeg is unavailable, fails, or produces no
    parseable ebur128 summary (e.g. the file is silent or too short for
    the meter to gate a single block).
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostats", "-hide_banner",
                "-i", str(file_path),
                "-af", "ebur128=peak=true",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Loudness measurement timed out") from exc
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError(f"Cannot read audio file: {file_path}") from exc

    integrated = _INTEGRATED_RE.search(result.stderr)
    true_peak = _TRUE_PEAK_RE.search(result.stderr)
    if not integrated or not true_peak:
        raise ValueError(f"Cannot measure loudness of: {file_path}")
    return float(integrated.group(1)), float(true_peak.group(1))


def normalize_loudness(file_path: Path, target_lufs: float) -> float | None:
    """Attenuate an over-loud file in place down to target_lufs.

    Only ever applies *negative* gain — a file quieter than the target is
    left untouched (boosting would amplify the noise floor and risk
    clipping). Returns the gain applied in dB, or None if the file was
    already at/below target (or within _SKIP_THRESHOLD_DB of it).

    The re-encode goes through a temp file + atomic replace so a crash
    mid-encode can't leave a truncated sound behind. Raises ValueError on
    measurement or encode failure; the original file is intact either way.
    """
    encode_args = _ENCODE_ARGS.get(file_path.suffix.lower())
    if encode_args is None:
        raise ValueError(f"Cannot normalize unsupported format: {file_path.suffix}")

    lufs, _ = measure_loudness(file_path)
    gain = target_lufs - lufs
    if gain >= -_SKIP_THRESHOLD_DB:
        return None

    tmp = file_path.with_name(file_path.stem + ".__norm_tmp__" + file_path.suffix)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-nostats", "-hide_banner",
                "-i", str(file_path),
                "-af", f"volume={gain:.2f}dB",
                *encode_args,
                str(tmp),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        # The replace lives inside the guard: on Windows a transient lock
        # on the destination (antivirus, indexer) raises OSError, and this
        # function's contract is ValueError-or-success — callers like
        # normalize_upload catch exactly that.
        os.replace(tmp, file_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # OSError subsumes FileNotFoundError (ffmpeg not installed).
        tmp.unlink(missing_ok=True)
        stderr = getattr(exc, "stderr", None)
        detail = f": {stderr[-300:]}" if stderr else ""
        raise ValueError(f"Failed to normalize {file_path}{detail}") from exc
    return gain
