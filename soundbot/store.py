import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_TAG_RE = re.compile(r"^[a-z0-9-]+$")
_MAX_NAME_LENGTH = 32
_MAX_TAG_LENGTH = 32
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".webm"}

CURRENT_SCHEMA_VERSION = 2


def _validate_tag(tag: str) -> None:
    if not tag or len(tag) > _MAX_TAG_LENGTH or not _TAG_RE.match(tag):
        raise ValueError(
            f"Tag '{tag}' is invalid: must be 1-{_MAX_TAG_LENGTH} characters of [a-z0-9-]"
        )


def parse_tags(raw: str | None) -> list[str]:
    """Parse a user-supplied comma-separated tag string into a sorted list.

    - None or empty -> [].
    - Each element is lowercased and validated against the same rules
      as add_tag (1-32 chars of [a-z0-9-]).
    - Empty elements (e.g. trailing commas) are skipped.
    - Duplicates are removed.
    - Raises ValueError for any invalid element — the caller can show
      the message to the user before any side effects.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw_element in raw.split(","):
        element = raw_element.strip().lower()
        if not element:
            continue
        _validate_tag(element)
        if element not in seen:
            seen.add(element)
            out.append(element)
    return sorted(out)


def migrate_v1_to_v2(
    v1_data: dict, guild_sound_map: dict[str, set[str]]
) -> dict:
    """Pure migration from a v1 store dict to a v2 store dict.

    Inputs:
      v1_data: the on-disk dict, e.g. {"version": 1, "sounds": {...}}.
      guild_sound_map: mapping of sanitized_guild_name -> set of sanitized
        sound names that exist in that guild's Discord soundboard. Caller is
        responsible for sanitizing both keys and values.

    For every local sound that matches a guild's soundboard, the sanitized
    guild name is appended as a tag. Tags are stored sorted and deduped.
    Sounds with no matches keep their existing tags (or [] if absent).

    Does not mutate v1_data.
    """
    v2 = copy.deepcopy(v1_data)
    v2["version"] = CURRENT_SCHEMA_VERSION
    sounds = v2.get("sounds", {})
    for sound_name, entry in sounds.items():
        existing = list(entry.get("tags", []))
        for guild_tag, sound_set in guild_sound_map.items():
            if sound_name in sound_set and guild_tag not in existing:
                existing.append(guild_tag)
        entry["tags"] = sorted(existing)
    return v2


class SoundStore:
    def __init__(self, metadata_path: Path, sounds_dir: Path) -> None:
        self._metadata_path = metadata_path
        self._sounds_dir = sounds_dir
        self._sounds: dict[str, dict] = {}
        self.loaded_version: int = CURRENT_SCHEMA_VERSION
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

    @staticmethod
    def sanitize_tag(raw: str) -> str:
        """Sanitize a raw string into a valid tag, or raise ValueError."""
        sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", raw).strip("-")[:_MAX_TAG_LENGTH].lower()
        if not sanitized:
            raise ValueError(f"Cannot derive a valid tag from '{raw}'")
        return sanitized

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
            "tags": [],
        }

    def add_tag(self, name: str, tag: str) -> None:
        _validate_tag(tag)
        key = name.lower()
        if key not in self._sounds:
            raise KeyError(f"Sound '{name}' not found")
        entry = self._sounds[key]
        tags = entry.setdefault("tags", [])
        if tag not in tags:
            tags.append(tag)
            tags.sort()

    def remove_tag(self, name: str, tag: str) -> None:
        key = name.lower()
        if key not in self._sounds:
            raise KeyError(f"Sound '{name}' not found")
        tags = self._sounds[key].setdefault("tags", [])
        if tag not in tags:
            raise KeyError(f"Tag '{tag}' not present on sound '{name}'")
        tags.remove(tag)

    def list_tags(self, name: str) -> list[str]:
        key = name.lower()
        if key not in self._sounds:
            raise KeyError(f"Sound '{name}' not found")
        return list(self._sounds[key].get("tags", []))

    def global_tags(self) -> list[tuple[str, int]]:
        """Return all tags with usage counts, sorted by count desc then name asc."""
        counts: dict[str, int] = {}
        for entry in self._sounds.values():
            for tag in entry.get("tags", []):
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

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

    def list_sounds(
        self, category: str | None = None, tag: str | None = None
    ) -> list[tuple[str, dict]]:
        results = []
        for name, entry in self._sounds.items():
            if category is not None and entry.get("category") != category:
                continue
            if tag is not None and tag not in entry.get("tags", []):
                continue
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
        data = {"sounds": self._sounds, "version": CURRENT_SCHEMA_VERSION}
        tmp = self._metadata_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._metadata_path)
        self.loaded_version = CURRENT_SCHEMA_VERSION

    def load(self) -> None:
        if self._metadata_path.exists():
            data = json.loads(self._metadata_path.read_text())
            self._sounds = data.get("sounds", {})
            self.loaded_version = data.get("version", 1)
        else:
            self._sounds = {}
            self.loaded_version = CURRENT_SCHEMA_VERSION
        # Ensure every entry has a tags field (backfill on load for v1)
        for entry in self._sounds.values():
            if "tags" not in entry:
                entry["tags"] = []

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
