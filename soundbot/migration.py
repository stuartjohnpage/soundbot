"""v1 -> v2 store migration: pure function and on_ready runner.

This module exists so that the migration logic — both the pure
transformation and the Discord-aware runner — lives in one place
instead of leaking duck-typed guild fetches into bot.py. Tests live
in tests/test_store.py (TestMigrationPureFunction) and
tests/test_migration.py (runner integration with fake guilds).
"""

import copy
import logging

from .store import CURRENT_SCHEMA_VERSION, SoundStore

logger = logging.getLogger("soundbot")


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
        # entry.get with a default is correct here: this function accepts
        # raw v1 input where the tags key has not yet been backfilled.
        accumulated_tags = list(entry.get("tags", []))
        for guild_tag, sound_set in guild_sound_map.items():
            if sound_name in sound_set and guild_tag not in accumulated_tags:
                accumulated_tags.append(guild_tag)
        entry["tags"] = sorted(accumulated_tags)
    return v2


async def run_migration_if_needed(store: SoundStore, guilds) -> None:
    """Backfill v1 sounds.json with sanitized-guild-name tags from each
    connected guild's Discord soundboard, then save at v2.

    No-op when the store's on-disk version at startup was already v2.
    No-op when the guild list is empty (we have nothing to match against,
    and committing v2 anyway would burn the retry opportunity forever).

    Why the gate reads ``store.startup_version`` and not the in-memory
    schema version: ``bot.setup_hook`` writes the store to disk via
    ``store.save()`` BEFORE ``on_ready`` fires. That save legitimately
    flushes the file to v2. If the gate looked at a "current version"
    field that ``save()`` mutates, the migration would early-return here
    on every real startup and every v1 user would silently end up with
    empty tags. ``startup_version`` is a frozen snapshot of what was on
    disk when the store was constructed, set once in ``load()``, so the
    gate reflects the pre-setup-hook state.

    Atomic on fetch failures: if any guild's fetch raises, no save
    happens and the file stays at v1 so the next startup retries.

    The ``guilds`` parameter is duck-typed: it must yield objects with
    ``.name`` and an awaitable ``fetch_soundboard_sounds()`` method.
    Tested against fake guild objects in tests/test_migration.py.
    """
    if store.startup_version >= CURRENT_SCHEMA_VERSION:
        return

    if not guilds:
        # Zero guilds means we have no soundboards to match against.
        # Refuse to migrate so the next on_ready retries with a populated
        # bot.guilds list. Marking the file as v2 here would silently
        # leave every sound untagged forever.
        logger.warning(
            "tag migration skipped: bot has no connected guilds yet; "
            "leaving sounds.json at v%d (startup) for retry on next startup",
            store.startup_version,
        )
        return

    # One fetch per guild — not per sound — to stay under Discord's rate limits.
    guild_map: dict[str, set[str]] = {}
    for guild in guilds:
        try:
            guild_tag = SoundStore.sanitize_tag(guild.name)
        except ValueError:
            logger.warning(
                "skipping guild %r during migration: name cannot be sanitized",
                getattr(guild, "name", "?"),
            )
            continue
        sounds = await guild.fetch_soundboard_sounds()
        sanitized_sound_names: set[str] = set()
        for s in sounds:
            try:
                sanitized_sound_names.add(SoundStore.sanitize_name(s.name))
            except ValueError:
                continue
        guild_map[guild_tag] = sanitized_sound_names

    v1_data = {"version": store.startup_version, "sounds": store.raw_sounds()}
    v2_data = migrate_v1_to_v2(v1_data, guild_map)
    # Swap in the migrated dict via the public hook and persist atomically.
    store.replace_sounds(v2_data["sounds"])
    store.save()

    sounds_view = store.raw_sounds()
    # Direct subscript: migrate_v1_to_v2 guarantees every entry has a tags key.
    tagged = sum(1 for e in sounds_view.values() if e["tags"])
    untagged = len(sounds_view) - tagged
    logger.info(
        "tag migration complete: processed=%d tagged=%d untagged=%d",
        len(sounds_view),
        tagged,
        untagged,
    )
