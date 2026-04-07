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
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError(f"Cannot read audio file: {file_path}") from exc

    data = json.loads(result.stdout)
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot determine duration of: {file_path}") from exc


def validate_sound(file_path: Path, max_duration: float) -> None:
    """Raise ValueError if file is not a valid audio file or exceeds max_duration."""
    duration = get_duration(file_path)
    if duration > max_duration:
        raise ValueError(
            f"Sound duration {duration:.1f}s exceeds maximum {max_duration:.1f}s"
        )
