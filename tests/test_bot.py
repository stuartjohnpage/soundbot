"""Wiring tests for Soundboard cog command handlers.

These exist because the PCM-cache refactor (issue #16) added meaningful
branching to `_play_sound` — error path on decode failure, teardown-race
re-check after `to_thread`, mixer-volume sync — and the agent review on
PR #18 flagged that none of it was unit-tested. The discord.py command
plumbing is mocked rather than stood up; the goal here is to exercise
the cog's own logic, not Discord's dispatcher.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from soundbot.bot import (
    Soundboard,
    deploy_commands,
    duplicate_sound_message,
    sync_guild_commands,
)
from soundbot.mixer import MixerSource
from soundbot.pcm_cache import CachedPCMSource, PCMCache
from soundbot.store import SoundStore


def _make_cog(tmp_path: Path) -> Soundboard:
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    store = SoundStore(
        metadata_path=tmp_path / "sounds.json",
        sounds_dir=sounds_dir,
    )
    bot = MagicMock()
    return Soundboard(bot, store)


def _make_interaction(*, voice_client=None, response_done: bool = False):
    interaction = MagicMock()
    interaction.guild = MagicMock()
    # Plain attribute assignment: MagicMock(name=...) would set the mock's
    # own name, not the guild.name attribute the auto-tag code reads.
    interaction.guild.name = "Test Guild"
    interaction.guild.voice_client = voice_client
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = response_done
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.__str__ = MagicMock(return_value="test-user")
    interaction.user.voice = MagicMock()
    interaction.user.voice.channel = MagicMock()
    return interaction


def _connected_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    return vc


def _add_sound(cog: Soundboard, name: str, file_name: str = "hello.ogg") -> str:
    sounds_dir = Path(cog.store._sounds_dir)
    path = sounds_dir / file_name
    path.write_bytes(b"")
    cog.store.add(name, path)
    return str(path)


class TestPlaySoundHappyPath:
    def test_cached_pcm_source_added_to_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")
        cog.pcm_cache = PCMCache(decoder=lambda p: b"\x00" * 7680)
        cog.mixer = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "alpha"))

        assert len(cog.mixer._sources) == 1
        assert isinstance(cog.mixer._sources[0], CachedPCMSource)
        assert cog.store.get("alpha")["play_count"] == 1
        interaction.response.send_message.assert_called_once()


class TestPlaySoundDecodeFailure:
    def test_decode_failure_replies_ephemeral_and_skips_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "broken")

        def boom(path):
            raise ValueError("unsupported codec: foo")

        cog.pcm_cache = PCMCache(decoder=boom)
        cog.mixer = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "broken"))

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "Failed to decode" in args[0]
        assert "broken" in args[0]
        assert kwargs.get("ephemeral") is True
        # Mixer untouched
        assert cog.mixer._sources == []
        # Play count NOT incremented — the user heard nothing
        assert cog.store.get("broken")["play_count"] == 0


class TestPlaySoundTeardownRace:
    def test_mixer_nulled_during_decode_bails_cleanly(self, tmp_path):
        """If /leave fires while we're awaiting to_thread, the mixer can
        be None when we resume. Old code lazily created a fresh mixer
        that was never wired to the voice client — silent drop + leak."""
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")

        def decoder_that_tears_down(p):
            cog.mixer = None
            return b"\x00" * 3840

        cog.pcm_cache = PCMCache(decoder=decoder_that_tears_down)
        cog.mixer = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "alpha"))

        # No lazy mixer recreated
        assert cog.mixer is None
        # Play count NOT bumped — the press produced no sound
        assert cog.store.get("alpha")["play_count"] == 0

    def test_vc_disconnected_during_decode_bails_cleanly(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")

        vc = _connected_vc()

        def decoder_that_drops_vc(p):
            vc.is_connected.return_value = False
            return b"\x00" * 3840

        cog.pcm_cache = PCMCache(decoder=decoder_that_drops_vc)
        cog.mixer = MixerSource()

        interaction = _make_interaction(voice_client=vc)
        asyncio.run(cog._play_sound(interaction, "alpha"))

        # Mixer is intact but no source was added
        assert len(cog.mixer._sources) == 0
        assert cog.store.get("alpha")["play_count"] == 0


class TestPlaySoundNotInVoice:
    def test_no_voice_client_replies_with_join_hint(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")

        # voice_client=None -> _ensure_voice raises
        interaction = _make_interaction(voice_client=None)
        asyncio.run(cog._play_sound(interaction, "alpha"))

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "join" in args[0].lower()
        assert kwargs.get("ephemeral") is True


class TestPlaySoundUnknownSound:
    def test_unknown_sound_replies_not_found(self, tmp_path):
        cog = _make_cog(tmp_path)
        # No sound added
        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "ghost"))

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "ghost" in args[0]
        assert "not found" in args[0].lower()


class TestVolumeCommand:
    def test_volume_command_syncs_to_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.mixer = MixerSource(volume=1.0)
        interaction = _make_interaction()

        asyncio.run(Soundboard.volume.callback(cog, interaction, 50))

        assert cog.volume == 0.5
        assert cog.mixer.volume == 0.5
        interaction.response.send_message.assert_called_once()

    def test_volume_command_safe_when_no_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        # mixer is None until /join is called
        assert cog.mixer is None
        interaction = _make_interaction()

        asyncio.run(Soundboard.volume.callback(cog, interaction, 75))

        assert cog.volume == 0.75
        # Did not raise, and still confirmed to the user
        interaction.response.send_message.assert_called_once()
        args, _ = interaction.response.send_message.call_args
        assert "75" in args[0]

    def test_volume_command_rejects_out_of_range(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.mixer = MixerSource(volume=0.5)
        interaction = _make_interaction()

        asyncio.run(Soundboard.volume.callback(cog, interaction, 150))

        # State unchanged
        assert cog.volume == 0.5
        assert cog.mixer.volume == 0.5


class TestRemoveSoundCacheInvalidation:
    def test_removesound_invalidates_cache_entry(self, tmp_path):
        cog = _make_cog(tmp_path)
        path = _add_sound(cog, "alpha")

        cog.pcm_cache = PCMCache(decoder=lambda p: b"cached")
        cog.pcm_cache.get(path)
        assert path in cog.pcm_cache

        interaction = _make_interaction()
        asyncio.run(Soundboard.removesound.callback(cog, interaction, "alpha"))

        assert path not in cog.pcm_cache
        assert cog.store.get("alpha") is None

    def test_removesound_unknown_leaves_cache_alone(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.pcm_cache = PCMCache(decoder=lambda p: b"cached")
        cog.pcm_cache.get("some/other/path")
        cog.pcm_cache.get("another/path")
        before = dict(cog.pcm_cache._cache)

        interaction = _make_interaction()
        asyncio.run(Soundboard.removesound.callback(cog, interaction, "ghost"))

        # Stronger than "specific key still present": every entry is
        # still present and nothing new appeared. Would fail if
        # removesound ever started invalidating an arbitrary path.
        assert cog.pcm_cache._cache == before


class TestFindExistingByPath:
    """Direct unit tests for the _find_existing_by_path helper. The
    integration tests in TestAddSoundClobberPrevention exercise it
    through addsound; these cover its contract independently so a future
    refactor can move the iteration without losing coverage."""

    def test_returns_name_when_path_matches(self, tmp_path):
        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        path = sounds_dir / "alpha.mp3"
        path.write_bytes(b"")
        cog.store.add("alpha", path)

        assert cog._find_existing_by_path(path) == "alpha"

    def test_returns_none_for_unowned_path(self, tmp_path):
        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        owned = sounds_dir / "alpha.mp3"
        owned.write_bytes(b"")
        cog.store.add("alpha", owned)

        unowned = sounds_dir / "beta.mp3"
        assert cog._find_existing_by_path(unowned) is None

    def test_returns_none_on_empty_store(self, tmp_path):
        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        assert cog._find_existing_by_path(sounds_dir / "anything.mp3") is None

    def test_finds_match_among_multiple_entries(self, tmp_path):
        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        for name in ("one", "two", "three"):
            p = sounds_dir / f"{name}.mp3"
            p.write_bytes(b"")
            cog.store.add(name, p)

        target = sounds_dir / "two.mp3"
        assert cog._find_existing_by_path(target) == "two"

    def test_resolves_relative_against_absolute(self, tmp_path, monkeypatch):
        """The helper resolves both sides — same logical file via different
        path representations (relative vs absolute) should still match."""
        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)

        # Store an entry with the absolute path
        abs_path = (sounds_dir / "alpha.mp3").resolve()
        abs_path.write_bytes(b"")
        cog.store.add("alpha", abs_path)

        # Look it up by a relative path that resolves to the same place
        monkeypatch.chdir(sounds_dir)
        assert cog._find_existing_by_path(Path("alpha.mp3")) == "alpha"

    def test_does_not_raise_on_unusual_paths(self, tmp_path):
        cog = _make_cog(tmp_path)
        # Empty store: any input path should return None, never raise
        assert cog._find_existing_by_path(Path("")) is None
        assert cog._find_existing_by_path(Path("does/not/exist.mp3")) is None


class TestImportSoundsPathConflict:
    """The same store-entry-path-collision guard that addsound got needs
    to fire in importsounds too: a fresh download of a Discord soundboard
    sound must not silently overwrite a file owned by an entry under a
    different name."""

    def test_path_conflict_blocks_download(self, tmp_path, monkeypatch):
        from soundbot import config

        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        monkeypatch.setattr(config, "SOUNDS_DIR", sounds_dir)
        monkeypatch.setattr(config, "MAX_DURATION", 60)

        # Pre-populate: an entry under the name "owner" points at the
        # path that "victim.ogg" would download to. The file is *not* on
        # disk (so classify_import_sound returns "needs_download"), but
        # the entry still owns it. Without the guard, we'd silently
        # overwrite "owner"'s file with the new download.
        target_path = sounds_dir / "victim.ogg"
        cog.store.add("owner", target_path)

        # Discord soundboard sound mock — sanitize_name("victim") -> "victim"
        sound = MagicMock()
        sound.name = "victim"
        sound.id = 1234

        save_called = []

        async def fake_save(path):
            save_called.append(str(path))
            Path(path).write_bytes(b"new-bytes")

        sound.save = fake_save

        guild = MagicMock()
        guild.name = "test-guild"
        guild.fetch_soundboard_sounds = AsyncMock(return_value=[sound])

        interaction = _make_interaction()
        interaction.guild = guild

        asyncio.run(Soundboard.importsounds.callback(cog, interaction))

        # sound.save was never called — the guard refused before download
        assert save_called == []
        # "owner" entry intact
        assert cog.store.get("owner")["file"] == str(target_path)
        # No "victim" entry was added
        assert cog.store.get("victim") is None
        # The user was told via the followup summary
        interaction.followup.send.assert_called()
        # Find the summary call (the one that mentions "Path conflict")
        summary_calls = [
            c for c in interaction.followup.send.call_args_list
            if "Path conflict" in (c.args[0] if c.args else "")
        ]
        assert len(summary_calls) == 1
        assert "owner" in summary_calls[0].args[0]


class TestAddSoundClobberPrevention:
    """The error-path unlink in addsound used to clobber another entry's
    file. Pre-existing bug, surfaced in the second review of PR #18.
    Both clobber scenarios are covered:

    - Different name, same uploaded filename: silently corrupts the
      existing entry even without raising. Must refuse pre-save.
    - Same name, same filename: `store.add` raises on name collision,
      error path unlinks the file the *existing* entry still needs.
      Also refuse pre-save.
    """

    def _setup(self, tmp_path, monkeypatch):
        return _setup_addsound(tmp_path, monkeypatch)

    def test_different_name_same_filename_is_refused(
        self, tmp_path, monkeypatch
    ):
        cog, sounds_dir = self._setup(tmp_path, monkeypatch)

        existing_path = sounds_dir / "thing.mp3"
        existing_path.write_bytes(b"first-content")
        cog.store.add("first", existing_path)

        attachment = MagicMock(spec=discord.Attachment)
        attachment.filename = "thing.mp3"

        async def fake_save(path):
            Path(path).write_bytes(b"second-content")

        attachment.save = fake_save

        interaction = _make_interaction()
        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "second", attachment
            )
        )

        # File content is intact — file.save never ran
        assert existing_path.read_bytes() == b"first-content"
        # No "second" entry was added
        assert cog.store.get("second") is None
        # Original "first" entry intact
        assert cog.store.get("first")["file"] == str(existing_path)
        # User was told why
        interaction.followup.send.assert_called_once()
        args, kwargs = interaction.followup.send.call_args
        assert "first" in args[0]
        assert kwargs.get("ephemeral") is True

    def test_same_name_same_filename_is_refused(
        self, tmp_path, monkeypatch
    ):
        cog, sounds_dir = self._setup(tmp_path, monkeypatch)

        existing_path = sounds_dir / "thing.mp3"
        existing_path.write_bytes(b"original-content")
        cog.store.add("existing", existing_path)

        attachment = MagicMock(spec=discord.Attachment)
        attachment.filename = "thing.mp3"

        async def fake_save(path):
            Path(path).write_bytes(b"replacement-content")

        attachment.save = fake_save

        interaction = _make_interaction()
        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "existing", attachment
            )
        )

        # Original file content preserved — file.save never ran
        assert existing_path.read_bytes() == b"original-content"
        # Store entry intact
        assert cog.store.get("existing") is not None
        # User told to remove first
        interaction.followup.send.assert_called_once()
        args, _ = interaction.followup.send.call_args
        assert "remove" in args[0].lower() or "already" in args[0].lower()

    def test_different_name_different_filename_succeeds(
        self, tmp_path, monkeypatch
    ):
        """The guard must not false-positive on unrelated uploads."""
        cog, sounds_dir = self._setup(tmp_path, monkeypatch)

        existing_path = sounds_dir / "alpha.mp3"
        existing_path.write_bytes(b"alpha-bytes")
        cog.store.add("alpha", existing_path)

        attachment = MagicMock(spec=discord.Attachment)
        attachment.filename = "beta.mp3"

        async def fake_save(path):
            Path(path).write_bytes(b"beta-bytes")

        attachment.save = fake_save

        interaction = _make_interaction()
        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "beta", attachment
            )
        )

        assert cog.store.get("beta") is not None
        assert cog.store.get("alpha") is not None
        assert (sounds_dir / "beta.mp3").read_bytes() == b"beta-bytes"
        assert existing_path.read_bytes() == b"alpha-bytes"


class TestAddSoundCacheInvalidation:
    def test_addsound_invalidates_cache_for_destination(
        self, tmp_path, monkeypatch
    ):
        """Two different /addsound invocations using the same uploaded
        filename land at the same dest on disk. If the first one was
        played, its PCM is in the cache — and the second add must wipe
        that entry or the new sound serves the old bytes."""
        cog, sounds_dir = _setup_addsound(tmp_path, monkeypatch)

        dest = sounds_dir / "thing.mp3"
        cache_key = str(dest)
        cog.pcm_cache = PCMCache(decoder=lambda p: b"stale")
        cog.pcm_cache.get(cache_key)
        assert cache_key in cog.pcm_cache

        attachment = MagicMock(spec=discord.Attachment)
        attachment.filename = "thing.mp3"

        async def fake_save(path):
            Path(path).write_bytes(b"new-file")

        attachment.save = fake_save

        interaction = _make_interaction()
        asyncio.run(
            Soundboard.addsound.callback(cog, interaction, "thing", attachment)
        )

        assert cache_key not in cog.pcm_cache


def _setup_addsound(tmp_path, monkeypatch, *, normalize=lambda p, t: None):
    """Shared harness for addsound handler tests: real store + tmp sounds
    dir, audio helpers stubbed so no ffmpeg runs."""
    from soundbot import config

    cog = _make_cog(tmp_path)
    sounds_dir = Path(cog.store._sounds_dir)
    monkeypatch.setattr(config, "SOUNDS_DIR", sounds_dir)
    monkeypatch.setattr(config, "MAX_DURATION", 60)
    # The audio helpers live in soundbot.ingest since the pipeline was
    # extracted there (shared with the web panel's upload route).
    monkeypatch.setattr("soundbot.ingest.has_video_stream", lambda p: False)
    monkeypatch.setattr("soundbot.ingest.validate_sound", lambda p, d: None)
    monkeypatch.setattr("soundbot.ingest.normalize_loudness", normalize)
    return cog, sounds_dir


def _make_attachment(filename: str) -> MagicMock:
    attachment = MagicMock(spec=discord.Attachment)
    attachment.filename = filename

    async def fake_save(path):
        Path(path).write_bytes(b"audio-bytes")

    attachment.save = MagicMock(side_effect=fake_save)
    return attachment


class TestDuplicateSoundMessage:
    """Pure-function coverage for the name-collision wording (bug: the old
    bare "already exists" gave no hint that the sound was merely invisible
    on the guild's tag-filtered board)."""

    def test_untagged_entry_explains_board_invisibility(self):
        msg = duplicate_sound_message("What", {"tags": []})
        assert "what" in msg
        assert "no tags" in msg
        assert "/board" in msg

    def test_tagged_entry_lists_tags_sorted(self):
        msg = duplicate_sound_message("boop", {"tags": ["zeta", "alpha"]})
        assert "`alpha`, `zeta`" in msg
        assert "/board" in msg


class TestAddSoundDuplicateNamePrecheck:
    def test_refused_before_file_io_with_tag_hint(self, tmp_path, monkeypatch):
        cog, sounds_dir = _setup_addsound(tmp_path, monkeypatch)
        existing_path = sounds_dir / "orig.mp3"
        existing_path.write_bytes(b"orig")
        cog.store.add("dupe", existing_path)

        attachment = _make_attachment("unrelated.mp3")
        interaction = _make_interaction()
        asyncio.run(
            Soundboard.addsound.callback(cog, interaction, "dupe", attachment)
        )

        # Refused before any file I/O — the upload never touched disk.
        attachment.save.assert_not_called()
        args, kwargs = interaction.followup.send.call_args
        assert "no tags" in args[0]
        assert kwargs.get("ephemeral") is True


class TestAddSoundGuildAutoTag:
    def test_upload_is_tagged_with_guild_and_user_tags(self, tmp_path, monkeypatch):
        cog, _ = _setup_addsound(tmp_path, monkeypatch)
        interaction = _make_interaction()  # guild.name = "Test Guild"

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3"),
                tags="meme,funny",
            )
        )

        assert set(cog.store.get("boop")["tags"]) == {"meme", "funny", "test-guild"}
        args, _ = interaction.followup.send.call_args
        assert "`test-guild`" in args[0]

    def test_guild_tag_not_duplicated_when_user_supplies_it(self, tmp_path, monkeypatch):
        cog, _ = _setup_addsound(tmp_path, monkeypatch)
        interaction = _make_interaction()

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3"),
                tags="test-guild",
            )
        )

        assert cog.store.get("boop")["tags"] == ["test-guild"]

    def test_dm_upload_gets_no_guild_tag(self, tmp_path, monkeypatch):
        cog, _ = _setup_addsound(tmp_path, monkeypatch)
        interaction = _make_interaction()
        interaction.guild = None

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3")
            )
        )

        assert cog.store.get("boop")["tags"] == []

    def test_unsanitizable_guild_name_skips_tag_but_uploads(self, tmp_path, monkeypatch):
        cog, _ = _setup_addsound(tmp_path, monkeypatch)
        interaction = _make_interaction()
        interaction.guild.name = "!!!"

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3")
            )
        )

        assert cog.store.get("boop")["tags"] == []


class TestAddSoundNormalization:
    def test_applied_gain_reported_in_message(self, tmp_path, monkeypatch):
        calls = []

        def fake_normalize(path, target):
            calls.append((Path(path), target))
            return -4.5

        cog, sounds_dir = _setup_addsound(
            tmp_path, monkeypatch, normalize=fake_normalize
        )
        interaction = _make_interaction()

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3")
            )
        )

        from soundbot import config

        assert calls == [(sounds_dir / "boop.mp3", config.TARGET_LUFS)]
        args, _ = interaction.followup.send.call_args
        assert "Normalized -4.5 dB" in args[0]

    def test_normalization_failure_keeps_upload(self, tmp_path, monkeypatch):
        def broken_normalize(path, target):
            raise ValueError("ffmpeg exploded")

        cog, _ = _setup_addsound(
            tmp_path, monkeypatch, normalize=broken_normalize
        )
        interaction = _make_interaction()

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3")
            )
        )

        # A sound that can't be normalized is still a playable sound.
        assert cog.store.get("boop") is not None
        args, _ = interaction.followup.send.call_args
        assert "Added sound" in args[0]
        assert "Normalized" not in args[0]

    def test_already_at_target_reports_no_gain(self, tmp_path, monkeypatch):
        cog, _ = _setup_addsound(tmp_path, monkeypatch)  # normalize -> None
        interaction = _make_interaction()

        asyncio.run(
            Soundboard.addsound.callback(
                cog, interaction, "boop", _make_attachment("boop.mp3")
            )
        )

        args, _ = interaction.followup.send.call_args
        assert "Added sound" in args[0]
        assert "Normalized" not in args[0]


class TestImportSoundsNormalization:
    def test_downloaded_sound_is_normalized(self, tmp_path, monkeypatch):
        """The upload-time normalization must fire for /importsounds
        downloads too — imported soundboard sounds arrive at whatever
        level they were uploaded to Discord at."""
        from soundbot import config

        calls = []

        def fake_normalize(path, target):
            calls.append((Path(path), target))
            return -2.0

        cog, sounds_dir = _setup_addsound(
            tmp_path, monkeypatch, normalize=fake_normalize
        )

        sound = MagicMock()
        sound.name = "fresh"
        sound.id = 99

        async def fake_save(path):
            Path(path).write_bytes(b"ogg-bytes")

        sound.save = fake_save

        guild = MagicMock()
        guild.name = "Test Guild"
        guild.fetch_soundboard_sounds = AsyncMock(return_value=[sound])

        interaction = _make_interaction()
        interaction.guild = guild

        asyncio.run(Soundboard.importsounds.callback(cog, interaction))

        assert calls == [(sounds_dir / "fresh.ogg", config.TARGET_LUFS)]
        assert cog.store.get("fresh") is not None


class TestCommandSync:
    """Per-guild command deployment (replaces the old single-GUILD_ID sync).

    Guild-scoped syncs are instant, so we copy the global command set into
    every connected guild individually and then wipe Discord's *global*
    registrations once — leaving the in-memory tree intact so a guild joined
    later (on_guild_join) can still copy from it.
    """

    def test_sync_guild_commands_copies_then_syncs_that_guild(self):
        tree = MagicMock()
        tree.sync = AsyncMock()
        guild = MagicMock()

        asyncio.run(sync_guild_commands(tree, guild))

        tree.copy_global_to.assert_called_once_with(guild=guild)
        tree.sync.assert_awaited_once_with(guild=guild)

    def test_deploy_syncs_every_guild_then_wipes_global_once(self):
        tree = MagicMock()
        tree.sync = AsyncMock()
        http = MagicMock()
        http.bulk_upsert_global_commands = AsyncMock()
        guilds = [MagicMock(), MagicMock(), MagicMock()]

        asyncio.run(deploy_commands(tree, http, 42, guilds))

        assert tree.copy_global_to.call_count == 3
        synced = [call.kwargs["guild"] for call in tree.sync.await_args_list]
        assert synced == guilds
        # Empty payload = delete all global commands, so they don't double up
        # next to the per-guild copies. Exactly once.
        http.bulk_upsert_global_commands.assert_awaited_once_with(42, [])

    def test_deploy_does_not_clear_in_memory_tree(self):
        """The global wipe must go through HTTP, not tree.clear_commands —
        clearing the in-memory tree would strand later guild joins."""
        tree = MagicMock()
        tree.sync = AsyncMock()
        http = MagicMock()
        http.bulk_upsert_global_commands = AsyncMock()

        asyncio.run(deploy_commands(tree, http, 1, [MagicMock()]))

        tree.clear_commands.assert_not_called()

    def test_deploy_with_no_guilds_still_wipes_stale_global(self):
        tree = MagicMock()
        tree.sync = AsyncMock()
        http = MagicMock()
        http.bulk_upsert_global_commands = AsyncMock()

        asyncio.run(deploy_commands(tree, http, 7, []))

        tree.copy_global_to.assert_not_called()
        tree.sync.assert_not_awaited()
        http.bulk_upsert_global_commands.assert_awaited_once_with(7, [])


# -- Emoji reaction playback (issue #9) --

from soundbot.bot import parse_emoji_key  # noqa: E402


class TestParseEmojiKey:
    def test_unicode_emoji_passes_through(self):
        assert parse_emoji_key("🎺") == "🎺"

    def test_whitespace_stripped(self):
        assert parse_emoji_key(" 🎺 ") == "🎺"

    def test_custom_emoji_canonicalized(self):
        assert parse_emoji_key("<:pog:1122334455667788>") == "<:pog:1122334455667788>"

    def test_animated_custom_emoji(self):
        assert parse_emoji_key("<a:dance:1122334455667789>") == "<a:dance:1122334455667789>"

    def test_keycap_emoji_allowed(self):
        assert parse_emoji_key("1️⃣") == "1️⃣"

    def test_zwj_sequence_allowed(self):
        family = "👨‍👩‍👧‍👦"
        assert parse_emoji_key(family) == family

    def test_plain_word_rejected(self):
        with pytest.raises(ValueError, match="doesn't look like an emoji"):
            parse_emoji_key("airhorn")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            parse_emoji_key("   ")

    def test_overlong_sequence_rejected(self):
        with pytest.raises(ValueError, match="single emoji"):
            parse_emoji_key("🎺" * 17)


GUILD_ID = 555
BOT_USER_ID = 999


def _make_reaction_cog(tmp_path, *, in_voice=True):
    """Cog wired for reaction tests: real store, fake decoder, live mixer."""
    cog = _make_cog(tmp_path)
    _add_sound(cog, "airhorn")
    cog.pcm_cache = PCMCache(decoder=lambda p: b"\x00" * 7680)
    cog.mixer = MixerSource()
    cog.bot.user.id = BOT_USER_ID
    guild = MagicMock()
    guild.voice_client = _connected_vc() if in_voice else None
    cog.bot.get_guild.return_value = guild
    return cog


def _make_payload(*, guild_id=GUILD_ID, user_id=1, emoji="🎺"):
    payload = MagicMock()
    payload.guild_id = guild_id
    payload.user_id = user_id
    payload.emoji = emoji  # str() of a plain str is itself, like PartialEmoji
    return payload


class TestReactionPlayback:
    def test_bound_emoji_plays_sound(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert len(cog.mixer._sources) == 1
        assert cog.store.get("airhorn")["play_count"] == 1

    def test_unbound_emoji_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)

        asyncio.run(cog.on_raw_reaction_add(_make_payload(emoji="💀")))

        assert cog.mixer._sources == []
        assert cog.store.get("airhorn")["play_count"] == 0

    def test_binding_in_other_guild_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload(guild_id=777)))

        assert cog.mixer._sources == []

    def test_dm_reaction_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload(guild_id=None)))

        assert cog.mixer._sources == []

    def test_bots_own_reaction_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload(user_id=BOT_USER_ID)))

        assert cog.mixer._sources == []

    def test_bot_not_in_voice_silently_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path, in_voice=False)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert cog.mixer._sources == []
        assert cog.store.get("airhorn")["play_count"] == 0

    def test_no_mixer_silently_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        cog.mixer = None

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert cog.store.get("airhorn")["play_count"] == 0

    def test_stale_binding_missing_sound_is_silent(self, tmp_path):
        """A binding whose sound vanished (e.g. hand-edited JSON) must not
        raise inside the listener — just log and stay quiet."""
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        # Drop the sound while keeping the binding (bypasses remove()'s cascade)
        cog.store.replace_sounds({})

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert cog.mixer._sources == []

    def test_custom_emoji_binding_matches_payload(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "<:pog:1122334455667788>", "airhorn")
        payload = _make_payload(
            emoji=discord.PartialEmoji(name="pog", id=1122334455667788)
        )

        asyncio.run(cog.on_raw_reaction_add(payload))

        assert len(cog.mixer._sources) == 1


def _make_guild_interaction(**kwargs):
    interaction = _make_interaction(**kwargs)
    interaction.guild.id = GUILD_ID
    return interaction


class TestBindEmojiCommand:
    def test_bind_happy_path_persists(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "airhorn")
        interaction = _make_guild_interaction()

        asyncio.run(
            Soundboard.bindemoji.callback(cog, interaction, "airhorn", "🎺")
        )

        assert cog.store.get_emoji_binding(GUILD_ID, "🎺") == "airhorn"
        args, _ = interaction.response.send_message.call_args
        assert "Bound" in args[0]
        # Persisted to disk, not just in memory
        reloaded = SoundStore(
            metadata_path=cog.store._metadata_path,
            sounds_dir=cog.store._sounds_dir,
        )
        assert reloaded.get_emoji_binding(GUILD_ID, "🎺") == "airhorn"

    def test_bind_invalid_emoji_rejected(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "airhorn")
        interaction = _make_guild_interaction()

        asyncio.run(
            Soundboard.bindemoji.callback(cog, interaction, "airhorn", "oops")
        )

        assert cog.store.get_emoji_binding(GUILD_ID, "oops") is None
        args, kwargs = interaction.response.send_message.call_args
        assert "doesn't look like an emoji" in args[0]
        assert kwargs.get("ephemeral") is True

    def test_bind_unknown_sound_rejected(self, tmp_path):
        cog = _make_cog(tmp_path)
        interaction = _make_guild_interaction()

        asyncio.run(
            Soundboard.bindemoji.callback(cog, interaction, "ghost", "🎺")
        )

        args, kwargs = interaction.response.send_message.call_args
        assert "not found" in args[0]
        assert kwargs.get("ephemeral") is True

    def test_rebind_reports_previous_sound(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "airhorn")
        _add_sound(cog, "bruh", "bruh.ogg")
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        interaction = _make_guild_interaction()

        asyncio.run(
            Soundboard.bindemoji.callback(cog, interaction, "bruh", "🎺")
        )

        assert cog.store.get_emoji_binding(GUILD_ID, "🎺") == "bruh"
        args, _ = interaction.response.send_message.call_args
        assert "Rebound" in args[0]
        assert "airhorn" in args[0]


class TestUnbindEmojiCommand:
    def test_unbind_happy_path(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "airhorn")
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        interaction = _make_guild_interaction()

        asyncio.run(Soundboard.unbindemoji.callback(cog, interaction, "🎺"))

        assert cog.store.get_emoji_binding(GUILD_ID, "🎺") is None
        args, _ = interaction.response.send_message.call_args
        assert "Unbound" in args[0]
        assert "airhorn" in args[0]

    def test_unbind_not_bound_reports_cleanly(self, tmp_path):
        cog = _make_cog(tmp_path)
        interaction = _make_guild_interaction()

        asyncio.run(Soundboard.unbindemoji.callback(cog, interaction, "🎺"))

        args, kwargs = interaction.response.send_message.call_args
        assert "not bound" in args[0]
        # No KeyError repr quotes leaking into the user-facing message
        assert not args[0].startswith('"')
        assert kwargs.get("ephemeral") is True


class TestListBindingsCommand:
    def test_empty_bindings_message(self, tmp_path):
        cog = _make_cog(tmp_path)
        interaction = _make_guild_interaction()

        asyncio.run(Soundboard.listbindings.callback(cog, interaction))

        args, kwargs = interaction.response.send_message.call_args
        assert "No emoji bindings" in args[0]
        assert kwargs.get("ephemeral") is True

    def test_lists_bindings_in_embed(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "airhorn")
        _add_sound(cog, "bruh", "bruh.ogg")
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        cog.store.bind_emoji(GUILD_ID, "💀", "bruh")
        interaction = _make_guild_interaction()

        asyncio.run(Soundboard.listbindings.callback(cog, interaction))

        _, kwargs = interaction.response.send_message.call_args
        embed = kwargs["embed"]
        assert "airhorn" in embed.description
        assert "💀" in embed.description
