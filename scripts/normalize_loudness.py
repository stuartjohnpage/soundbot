"""Normalize the loud sounds in sounds/ down to a target LUFS.

Only applies *negative* gain — any file quieter than the target is left alone
(avoids amplifying noise floor and removes clipping risk entirely).

Reads the pre-measurement TSV produced by scripts/measure_loudness.py so we
don't re-measure (that was already expensive).

Re-encodes in place via a temp file → atomic replace:
  - .ogg  -> libopus  96 kbps  (source is 48kHz stereo Opus already)
  - .mp3  -> libmp3lame 128 kbps CBR (matches source)

Run from repo root:  python scripts/normalize_loudness.py <measurements.tsv> <target_lufs>
"""
import os
import subprocess
import sys
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
SKIP_THRESHOLD_DB = 0.3  # don't bother re-encoding for sub-threshold trims


def encode_args(ext: str) -> list[str]:
    ext = ext.lower()
    if ext == ".ogg":
        return ["-c:a", "libopus", "-b:a", "96k", "-vbr", "on"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "128k"]
    raise ValueError(f"Unsupported extension: {ext}")


def apply_gain(path: Path, gain_db: float) -> None:
    tmp = path.with_name(path.stem + ".__norm_tmp__" + path.suffix)
    cmd = [
        "ffmpeg", "-y", "-nostats", "-hide_banner",
        "-i", str(path),
        "-af", f"volume={gain_db:.2f}dB",
        *encode_args(path.suffix),
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed on {path.name}:\n{result.stderr}")
    os.replace(tmp, path)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: normalize_loudness.py <measurements.tsv> <target_lufs>", file=sys.stderr)
        return 2

    measurements_path = Path(sys.argv[1])
    target_lufs = float(sys.argv[2])

    rows: list[tuple[str, float, float]] = []
    for line in measurements_path.read_text().splitlines():
        if not line.strip():
            continue
        name, lufs_s, peak_s = line.split("\t")
        rows.append((name, float(lufs_s), float(peak_s)))

    reduced = 0
    skipped_quiet = 0
    skipped_near_target = 0
    errors: list[str] = []

    # Sort by how much we're reducing, loudest first — most dramatic at top
    rows.sort(key=lambda r: r[1], reverse=True)

    print(f"Target: {target_lufs:.1f} LUFS")
    print(f"{'file':<40}  {'before':>8}  {'gain':>7}")
    print("-" * 60)

    for name, lufs, _peak in rows:
        path = SOUNDS_DIR / name
        if not path.exists():
            errors.append(f"{name}: missing file")
            continue

        if lufs <= target_lufs:
            skipped_quiet += 1
            continue

        gain = target_lufs - lufs
        if abs(gain) < SKIP_THRESHOLD_DB:
            skipped_near_target += 1
            print(f"{name:<40}  {lufs:>7.1f}   (skip, within {SKIP_THRESHOLD_DB} dB)")
            continue

        try:
            apply_gain(path, gain)
        except RuntimeError as exc:
            errors.append(str(exc))
            print(f"{name:<40}  ERROR")
            continue

        print(f"{name:<40}  {lufs:>7.1f}  {gain:>+6.1f} dB")
        reduced += 1

    print("-" * 60)
    print(f"Reduced:           {reduced}")
    print(f"Skipped (quieter): {skipped_quiet}")
    print(f"Skipped (at target within {SKIP_THRESHOLD_DB} dB): {skipped_near_target}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
