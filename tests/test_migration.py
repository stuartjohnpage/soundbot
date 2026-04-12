"""Smoke tests for the on_ready migration runner.

The runner glues together:
  - guild.fetch_soundboard_sounds() (per connected guild, once)
  - building the sanitized {guild_name: {sound_names}} mapping
  - the pure migrate_v1_to_v2() function
  - atomic save (or noop on error)

These tests use lightweight async fakes — no real discord.py objects.
We avoid pytest-asyncio (not in requirements-dev.txt) and drive coroutines
directly via asyncio.run() inside sync test bodies.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from soundbot.migration import run_migration_if_needed
from soundbot.store import SoundStore


class _FakeSoundboardSound:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeGuild:
    def __init__(self, name: str, sound_names: list[str]) -> None:
        self.name = name
        self._sounds = [_FakeSoundboardSound(n) for n in sound_names]
        self.fetch_calls = 0

    async def fetch_soundboard_sounds(self):
        self.fetch_calls += 1
        return self._sounds


def _seed_v1(tmp_path: Path, sounds: dict) -> Path:
    metadata = tmp_path / "sounds.json"
    metadata.write_text(json.dumps({"version": 1, "sounds": sounds}))
    return metadata


def _make_store(tmp_path: Path) -> SoundStore:
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir(exist_ok=True)
    return SoundStore(metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir)


def _entry(**overrides) -> dict:
    base = {
        "file": "/tmp/x.mp3",
        "category": None,
        "uploaded_by": "u#1",
        "uploaded_at": "2025-01-01T00:00:00+00:00",
        "play_count": 0,
    }
    base.update(overrides)
    return base


def test_migration_runs_when_version_is_v1(tmp_path):
    _seed_v1(
        tmp_path,
        {
            "airhorn": _entry(),
            "rimshot": _entry(),
        },
    )
    store = _make_store(tmp_path)
    assert store.loaded_version == 1

    guilds = [
        _FakeGuild("My Cool Server", ["airhorn"]),
        _FakeGuild("Other Place", ["rimshot"]),
    ]

    asyncio.run(run_migration_if_needed(store, guilds))

    assert store.get("airhorn")["tags"] == ["my-cool-server"]
    assert store.get("rimshot")["tags"] == ["other-place"]
    # Single fetch per guild
    assert all(g.fetch_calls == 1 for g in guilds)
    # Persisted to disk at v2
    data = json.loads((tmp_path / "sounds.json").read_text())
    assert data["version"] == 2
    assert store.loaded_version == 2


def test_migration_skipped_when_already_v2(tmp_path):
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    # First create at v2 by saving with the current store
    store = SoundStore(metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir)
    store._sounds["airhorn"] = _entry(tags=[])
    store.save()
    assert store.loaded_version == 2

    guild = _FakeGuild("server", ["airhorn"])
    asyncio.run(run_migration_if_needed(store, [guild]))

    # No fetches, no tags applied
    assert guild.fetch_calls == 0
    assert store.get("airhorn")["tags"] == []


def test_migration_atomic_on_fetch_failure(tmp_path):
    """If a guild's fetch raises, no save happens and the file stays at v1."""
    _seed_v1(tmp_path, {"airhorn": _entry()})
    store = _make_store(tmp_path)

    failing_guild = _FakeGuild("server", [])
    failing_guild.fetch_soundboard_sounds = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        asyncio.run(run_migration_if_needed(store, [failing_guild]))

    # File on disk is still v1
    data = json.loads((tmp_path / "sounds.json").read_text())
    assert data["version"] == 1


def test_migration_unmatched_sounds_stay_untagged(tmp_path):
    _seed_v1(tmp_path, {"orphan": _entry()})
    store = _make_store(tmp_path)

    guilds = [_FakeGuild("server", ["something-else"])]
    asyncio.run(run_migration_if_needed(store, guilds))

    assert store.get("orphan")["tags"] == []
    # Still saved to v2
    data = json.loads((tmp_path / "sounds.json").read_text())
    assert data["version"] == 2


def test_migration_one_fetch_per_guild_not_per_sound(tmp_path):
    _seed_v1(
        tmp_path,
        {
            "a": _entry(),
            "b": _entry(),
            "c": _entry(),
        },
    )
    store = _make_store(tmp_path)

    guild = _FakeGuild("server", ["a", "b", "c"])
    asyncio.run(run_migration_if_needed(store, [guild]))

    assert guild.fetch_calls == 1


def test_migration_sanitizes_sound_names_from_discord(tmp_path):
    """Discord sound names like 'My Sound!' should be sanitized for matching."""
    _seed_v1(tmp_path, {"my_sound": _entry()})
    store = _make_store(tmp_path)

    # Discord returns the human-readable name
    guild = _FakeGuild("server", ["My Sound!"])
    asyncio.run(run_migration_if_needed(store, [guild]))

    # The sound was matched after sanitization
    assert "server" in store.get("my_sound")["tags"]


def test_migration_skipped_when_no_guilds_connected(tmp_path):
    """Zero guilds at ready time means we have nothing to match against.

    Running the migration anyway would silently mark every sound untagged
    and bump the file to v2 — losing the retry opportunity forever.
    Refuse to migrate and leave the file at v1 so the next startup retries.
    """
    _seed_v1(tmp_path, {"airhorn": _entry()})
    store = _make_store(tmp_path)
    assert store.loaded_version == 1

    # No guilds connected
    asyncio.run(run_migration_if_needed(store, []))

    # File on disk is still v1
    data = json.loads((tmp_path / "sounds.json").read_text())
    assert data["version"] == 1
    # Store still reports v1 so the next on_ready will retry
    assert store.loaded_version == 1
    # Sound is untouched (no spurious tags or empty-list backfill mismatch)
    assert "tags" not in data["sounds"]["airhorn"] or data["sounds"]["airhorn"]["tags"] == []
