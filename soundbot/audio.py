import json
import subprocess
from pathlib import Path


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
