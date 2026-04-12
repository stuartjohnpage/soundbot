"""Show the before/after loudness diff for each file.

Flags any reduced file that didn't land within 0.5 dB of target.

Usage:  python scripts/diff_loudness.py <before.tsv> <after.tsv> <target_lufs>
"""
import sys
from pathlib import Path


def load(path: Path) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        name, lufs_s, peak_s = line.split("\t")
        out[name] = (float(lufs_s), float(peak_s))
    return out


def main() -> int:
    before = load(Path(sys.argv[1]))
    after = load(Path(sys.argv[2]))
    target = float(sys.argv[3])

    print(f"{'file':<40} {'before':>8} {'after':>8} {'diff':>7} {'flag':>6}")
    print("-" * 75)

    off_target: list[tuple[str, float]] = []
    untouched_above: list[tuple[str, float]] = []

    # Sort by before-loudness descending
    for name in sorted(before.keys(), key=lambda n: before[n][0], reverse=True):
        b_lufs, _ = before[name]
        if name not in after:
            print(f"{name:<40} {b_lufs:>8.1f}     MISSING")
            continue
        a_lufs, _ = after[name]
        delta = a_lufs - b_lufs
        flag = ""
        if b_lufs > target:  # should have been reduced
            if abs(a_lufs - target) > 0.5:
                flag = "OFF"
                off_target.append((name, a_lufs))
        else:  # should be untouched
            if abs(delta) > 0.1:
                flag = "MOVED"
        print(f"{name:<40} {b_lufs:>8.1f} {a_lufs:>8.1f} {delta:>+7.1f} {flag:>6}")

    print("-" * 75)
    if off_target:
        print(f"\n{len(off_target)} files didn't land within 0.5 dB of target:")
        for n, l in off_target:
            print(f"  {n}: {l:.1f} LUFS (target {target})")
        return 1
    print("\nAll reduced files landed within 0.5 dB of target.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
