"""One-shot helper: measure integrated loudness + true peak of every file in sounds/.

Run from repo root:  python scripts/measure_loudness.py
Prints one line per file: `filename<TAB>LUFS<TAB>true_peak_dbfs`
"""
import re
import subprocess
import sys
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
I_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+\.\d+) LUFS")
TP_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?\d+\.\d+) dBFS")


def measure(path: Path) -> tuple[float, float] | None:
    result = subprocess.run(
        [
            "ffmpeg", "-nostats", "-hide_banner",
            "-i", str(path),
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    out = result.stderr
    i_match = I_RE.search(out)
    tp_match = TP_RE.search(out)
    if not i_match or not tp_match:
        return None
    return float(i_match.group(1)), float(tp_match.group(1))


def main() -> int:
    files = sorted(
        p for p in SOUNDS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".opus"}
    )
    for path in files:
        m = measure(path)
        if m is None:
            print(f"{path.name}\tERROR\tERROR", file=sys.stderr)
            continue
        lufs, peak = m
        print(f"{path.name}\t{lufs}\t{peak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
