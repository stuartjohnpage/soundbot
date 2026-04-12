import json
import re
from datetime import datetime, timezone
from pathlib import Path

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_NAME_LENGTH = 32
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".webm"}


class SoundStore:
    def __init__(self, metadata_path: Path, sounds_dir: Path) -> None:
        self._metadata_path = metadata_path
        self._sounds_dir = sounds_dir
        self._sounds: dict[str, dict] = {}
        self.load()

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or len(name) > _MAX_NAME_LENGTH or not _NAME_RE.match(name):
            raise ValueError(f"Sound name '{name}' is invalid: must be 1-{_MAX_NAME_LENGTH} alphanumeric/hyphen/underscore characters")

    @staticmethod
    def sanitize_name(raw: str) -> str:
        """Sanitize a raw string into a valid sound name, or raise ValueError."""
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw).strip("_")[:_MAX_NAME_LENGTH]
        if not sanitized:
            raise ValueError(f"Cannot derive a valid name from '{raw}'")
        return sanitized.lower()

    def add(
        self,
        name: str,
        file_path: Path,
        category: str | None = None,
        uploaded_by: str | None = None,
    ) -> None:
        self._validate_name(name)
        key = name.lower()
        if key in self._sounds:
            raise ValueError(f"Sound '{name}' already exists")
        self._sounds[key] = {
            "file": str(file_path),
            "category": category,
            "uploaded_by": uploaded_by,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "play_count": 0,
        }

    def rename(self, old_name: str, new_name: str) -> None:
        self._validate_name(new_name)
        old_key = old_name.lower()
        new_key = new_name.lower()
        if old_key not in self._sounds:
            raise KeyError(f"Sound '{old_name}' not found")
        if new_key in self._sounds:
            raise ValueError(f"Sound '{new_name}' already exists")
        self._sounds[new_key] = self._sounds.pop(old_key)

    def remove(self, name: str) -> None:
        key = name.lower()
        if key not in self._sounds:
            raise KeyError(f"Sound '{name}' not found")
        entry = self._sounds.pop(key)
        file_path = Path(entry["file"])
        if file_path.exists():
            file_path.unlink()

    def get(self, name: str) -> dict | None:
        return self._sounds.get(name.lower())

    def list_sounds(self, category: str | None = None) -> list[tuple[str, dict]]:
        results = []
        for name, entry in self._sounds.items():
            if category is None or entry.get("category") == category:
                results.append((name, entry))
        return sorted(results, key=lambda x: x[0])

    def search(self, query: str) -> list[tuple[str, dict]]:
        query_lower = query.lower()
        if not query_lower:
            return self.list_sounds()
        prefix_matches = []
        substring_matches = []
        for name, entry in self._sounds.items():
            if name.startswith(query_lower):
                prefix_matches.append((name, entry))
            elif query_lower in name:
                substring_matches.append((name, entry))
        prefix_matches.sort(key=lambda x: x[0])
        substring_matches.sort(key=lambda x: x[0])
        return prefix_matches + substring_matches

    def increment_play_count(self, name: str) -> None:
        key = name.lower()
        if key not in self._sounds:
            raise KeyError(f"Sound '{name}' not found")
        self._sounds[key]["play_count"] += 1

    def save(self) -> None:
        data = {"sounds": self._sounds, "version": 1}
        tmp = self._metadata_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._metadata_path)

    def load(self) -> None:
        if self._metadata_path.exists():
            data = json.loads(self._metadata_path.read_text())
            self._sounds = data.get("sounds", {})
        else:
            self._sounds = {}

    def scan_folder(self) -> None:
        tracked_files = {Path(entry["file"]).resolve() for entry in self._sounds.values()}
        for path in sorted(self._sounds_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
                continue
            if path.resolve() in tracked_files:
                continue
            name = path.stem.lower()
            if name in self._sounds:
                continue
            # Infer category from subfolder (one level deep)
            relative = path.relative_to(self._sounds_dir)
            category = relative.parent.name if relative.parent != Path(".") else None
            try:
                self.add(name, path, category=category)
            except ValueError:
                # Invalid name from filename - skip silently
                continue

    def categories(self) -> list[str]:
        cats = {entry["category"] for entry in self._sounds.values() if entry.get("category")}
        return sorted(cats)
