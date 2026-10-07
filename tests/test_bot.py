"""Wiring tests for Soundboard cog command handlers.

These exist because the PCM-cache refactor (issue #16) added meaningful
branching to `_play_sound` — error path on decode failure, teardown-race
re-check after `to_thread`, mixer-volume sync — and the agent review on
PR #18 flagged that none of it was unit-tested. The discord.py command
plumbing is mocked rather than stood up; the goal here is to exercise
the cog's own logic, not Discord's dispatcher.
"""
import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from soundbot.bot import (
    Soundboard,
    _matches_ref,
    deploy_commands,
    sync_guild_commands,
)
from soundbot.ingest import duplicate_sound_message
from soundbot.mixer import MixerSource
from soundbot.pcm_cache import CachedPCMSource, PCMCache
from soundbot.store import SoundStore

GUILD_ID = 555


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
    interaction.guild.id = GUILD_ID
    interaction.guild_id = GUILD_ID
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
    interaction.user.voice.channel.guild = interaction.guild
    # Default to the happy path for the same-VC gate (issue #17): the
    # user sits in the bot's channel. Gate tests override one side.
    if voice_client is not None:
        voice_client.channel = interaction.user.voice.channel
    return interaction


def _connected_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.guild.id = GUILD_ID
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
        cog.mixers[GUILD_ID] = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "alpha"))

        assert len(cog.mixers[GUILD_ID]._sources) == 1
        assert isinstance(cog.mixers[GUILD_ID]._sources[0], CachedPCMSource)
        assert cog.store.get("alpha")["play_count"] == 1
        interaction.response.send_message.assert_called_once()


class TestPlaySoundDecodeFailure:
    def test_decode_failure_replies_ephemeral_and_skips_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "broken")

        def boom(path):
            raise ValueError("unsupported codec: foo")

        cog.pcm_cache = PCMCache(decoder=boom)
        cog.mixers[GUILD_ID] = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "broken"))

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "Failed to decode" in args[0]
        assert "broken" in args[0]
        assert kwargs.get("ephemeral") is True
        # Mixer untouched
        assert cog.mixers[GUILD_ID]._sources == []
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
            cog.mixers.pop(GUILD_ID, None)
            return b"\x00" * 3840

        cog.pcm_cache = PCMCache(decoder=decoder_that_tears_down)
        cog.mixers[GUILD_ID] = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "alpha"))

        # No lazy mixer recreated
        assert GUILD_ID not in cog.mixers
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
        cog.mixers[GUILD_ID] = MixerSource()

        interaction = _make_interaction(voice_client=vc)
        asyncio.run(cog._play_sound(interaction, "alpha"))

        # Mixer is intact but no source was added
        assert len(cog.mixers[GUILD_ID]._sources) == 0
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



class TestMixerPerGuild:
    """Each guild's voice client gets its own mixer. A single cog-wide
    mixer routed guild A's sounds into guild B's channel once the bot
    joined a second server, and A's /leave then silenced B."""

    OTHER_GUILD = 777

    def _other_guild_vc(self):
        vc = _connected_vc()
        vc.guild.id = self.OTHER_GUILD
        vc.disconnect = AsyncMock()
        return vc

    def test_play_goes_to_the_invoking_guilds_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")
        cog.pcm_cache = PCMCache(decoder=lambda p: b"\x00" * 3840)
        cog.mixers[GUILD_ID] = MixerSource()
        cog.mixers[self.OTHER_GUILD] = MixerSource()

        interaction = _make_interaction(voice_client=_connected_vc())
        asyncio.run(cog._play_sound(interaction, "alpha"))

        assert len(cog.mixers[GUILD_ID]._sources) == 1
        assert cog.mixers[self.OTHER_GUILD]._sources == []

    def test_leaving_one_guild_keeps_the_others_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.mixers[GUILD_ID] = MixerSource()
        other = MixerSource()
        cog.mixers[self.OTHER_GUILD] = other

        asyncio.run(cog._teardown_voice(self._other_guild_vc()))

        assert GUILD_ID in cog.mixers
        assert self.OTHER_GUILD not in cog.mixers
        assert other._sources == []  # stopped/cleaned, not reused

    def test_join_creates_a_mixer_for_that_guild_only(self, tmp_path):
        cog = _make_cog(tmp_path)
        existing = MixerSource()
        cog.mixers[self.OTHER_GUILD] = existing
        interaction = _make_interaction()
        new_vc = MagicMock()
        interaction.user.voice.channel.connect = AsyncMock(return_value=new_vc)

        asyncio.run(Soundboard.join.callback(cog, interaction))

        assert cog.mixers[self.OTHER_GUILD] is existing
        new_vc.play.assert_called_once_with(cog.mixers[GUILD_ID])

    def test_volume_applies_to_every_guilds_mixer(self, tmp_path):
        """Volume stays global (one knob, per the spec) — it just has to
        reach every live mixer now that there can be several."""
        cog = _make_cog(tmp_path)
        cog.mixers[GUILD_ID] = MixerSource(volume=1.0)
        cog.mixers[self.OTHER_GUILD] = MixerSource(volume=1.0)

        asyncio.run(Soundboard.volume.callback(cog, _make_interaction(), 30))

        assert cog.mixers[GUILD_ID].volume == 0.3
        assert cog.mixers[self.OTHER_GUILD].volume == 0.3


class TestVolumeCommand:
    def test_volume_command_syncs_to_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.mixers[GUILD_ID] = MixerSource(volume=1.0)
        interaction = _make_interaction()

        asyncio.run(Soundboard.volume.callback(cog, interaction, 50))

        assert cog.volume == 0.5
        assert cog.mixers[GUILD_ID].volume == 0.5
        interaction.response.send_message.assert_called_once()

    def test_volume_command_safe_when_no_mixer(self, tmp_path):
        cog = _make_cog(tmp_path)
        # no mixer until /join is called
        assert GUILD_ID not in cog.mixers
        interaction = _make_interaction()

        asyncio.run(Soundboard.volume.callback(cog, interaction, 75))

        assert cog.volume == 0.75
        # Did not raise, and still confirmed to the user
        interaction.response.send_message.assert_called_once()
        args, _ = interaction.response.send_message.call_args
        assert "75" in args[0]

    def test_volume_command_rejects_out_of_range(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.mixers[GUILD_ID] = MixerSource(volume=0.5)
        interaction = _make_interaction()

        asyncio.run(Soundboard.volume.callback(cog, interaction, 150))

        # State unchanged
        assert cog.volume == 0.5
        assert cog.mixers[GUILD_ID].volume == 0.5


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


class TestImportSoundsReservation:
    def test_file_appearing_after_classification_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        """Classification checks the disk, then the download awaits — a
        web upload with the same filename can land in between. The import
        must claim the file atomically and back off if it lost the race."""
        from soundbot import config

        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        monkeypatch.setattr(config, "SOUNDS_DIR", sounds_dir)
        # Simulate losing the race: classification saw no file, but by the
        # time the import claims the path, someone else has written it.
        monkeypatch.setattr(
            "soundbot.bot.classify_import_sound", lambda *a: "needs_download"
        )
        winner = sounds_dir / "victim.ogg"
        winner.write_bytes(b"web upload")
        sound = MagicMock()
        sound.name = "victim"
        sound.id = 1234
        sound.save = AsyncMock()
        guild = MagicMock()
        guild.name = "test-guild"
        guild.fetch_soundboard_sounds = AsyncMock(return_value=[sound])
        interaction = _make_interaction()
        interaction.guild = guild

        asyncio.run(Soundboard.importsounds.callback(cog, interaction))

        sound.save.assert_not_awaited()
        assert winner.read_bytes() == b"web upload"
        summary = interaction.followup.send.call_args.args[0]
        assert "File conflict 1" in summary

    def test_cancelled_download_removes_the_placeholder(self, tmp_path, monkeypatch):
        """The import reserves an empty placeholder before downloading.
        Anything escaping the per-sound error handling (cancellation, an
        unexpected exception) must not leave it behind, where it would
        block the filename and register as a broken sound on restart."""
        from soundbot import config

        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        monkeypatch.setattr(config, "SOUNDS_DIR", sounds_dir)
        sound = MagicMock()
        sound.name = "victim"
        sound.id = 1234
        sound.save = AsyncMock(side_effect=asyncio.CancelledError)
        guild = MagicMock()
        guild.name = "test-guild"
        guild.fetch_soundboard_sounds = AsyncMock(return_value=[sound])
        interaction = _make_interaction()
        interaction.guild = guild

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(Soundboard.importsounds.callback(cog, interaction))

        assert not (sounds_dir / "victim.ogg").exists()


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


class TestAddSoundUnregisteredFile:
    def test_stray_file_on_disk_is_refused_and_untouched(self, tmp_path, monkeypatch):
        cog, sounds_dir = _setup_addsound(tmp_path, monkeypatch)
        stray = sounds_dir / "stray.mp3"
        stray.write_bytes(b"not ours")
        attachment = _make_attachment("stray.mp3")
        interaction = _make_interaction()

        asyncio.run(Soundboard.addsound.callback(cog, interaction, "stray", attachment))

        attachment.save.assert_not_called()
        assert stray.read_bytes() == b"not ours"
        assert cog.store.get("stray") is None
        args, kwargs = interaction.followup.send.call_args
        assert "stray.mp3" in args[0]
        assert kwargs.get("ephemeral") is True


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
    # 1.0s stub duration: always under the (also-stubbed) 60s cap, so the
    # trim branch never triggers in these handler tests.
    monkeypatch.setattr("soundbot.ingest.get_duration", lambda p: 1.0)
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

    def test_keycap_emoji_allowed_and_vs16_stripped(self):
        # "1️⃣" is "1" + VS16 + combining keycap; the key drops the VS16
        assert parse_emoji_key("1️⃣") == "1⃣"

    def test_vs16_variants_collapse_to_same_key(self):
        # ❤ (U+2764) and ❤️ (U+2764 U+FE0F) are the same emoji; clients
        # disagree about sending the variation selector.
        assert parse_emoji_key("❤") == parse_emoji_key("❤️") == "❤"

    def test_bare_keycap_base_rejected(self):
        # A lone "5" can never match a reaction (payloads carry 5️⃣),
        # so binding it would create a dead binding with a success message.
        with pytest.raises(ValueError, match="doesn't look like an emoji"):
            parse_emoji_key("5")

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


BOT_USER_ID = 999


def _make_reaction_cog(tmp_path, *, in_voice=True, reactor_in_vc=True):
    """Cog wired for reaction tests: real store, fake decoder, live mixer.

    By default the reacting user (id=1, _make_payload's default) is a member
    of the bot's voice channel, satisfying the same-VC gate (issue #17).
    """
    cog = _make_cog(tmp_path)
    _add_sound(cog, "airhorn")
    cog.pcm_cache = PCMCache(decoder=lambda p: b"\x00" * 7680)
    cog.mixers[GUILD_ID] = MixerSource()
    cog.bot.user.id = BOT_USER_ID
    guild = MagicMock()
    if in_voice:
        vc = _connected_vc()
        vc.channel.members = [MagicMock(id=1)] if reactor_in_vc else []
        guild.voice_client = vc
    else:
        guild.voice_client = None
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

        assert len(cog.mixers[GUILD_ID]._sources) == 1
        assert cog.store.get("airhorn")["play_count"] == 1

    def test_unbound_emoji_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)

        asyncio.run(cog.on_raw_reaction_add(_make_payload(emoji="💀")))

        assert cog.mixers[GUILD_ID]._sources == []
        assert cog.store.get("airhorn")["play_count"] == 0

    def test_binding_in_other_guild_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload(guild_id=777)))

        assert cog.mixers[GUILD_ID]._sources == []

    def test_dm_reaction_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload(guild_id=None)))

        assert cog.mixers[GUILD_ID]._sources == []

    def test_bots_own_reaction_is_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload(user_id=BOT_USER_ID)))

        assert cog.mixers[GUILD_ID]._sources == []

    def test_bot_not_in_voice_silently_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path, in_voice=False)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert cog.mixers[GUILD_ID]._sources == []
        assert cog.store.get("airhorn")["play_count"] == 0

    def test_no_mixer_silently_ignored(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        cog.mixers.pop(GUILD_ID, None)

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

        assert cog.mixers[GUILD_ID]._sources == []

    def test_vs16_in_payload_still_matches_binding(self, tmp_path):
        """Binding stored without VS16 (parse_emoji_key strips it) must match
        a reaction payload that carries the selector, and vice versa."""
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "❤", "airhorn")  # bare heart

        asyncio.run(
            cog.on_raw_reaction_add(_make_payload(emoji="❤️"))
        )

        assert len(cog.mixers[GUILD_ID]._sources) == 1

    def test_bot_user_none_fails_closed(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")
        cog.bot.user = None

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert cog.mixers[GUILD_ID]._sources == []

    def test_custom_emoji_binding_matches_payload(self, tmp_path):
        cog = _make_reaction_cog(tmp_path)
        cog.store.bind_emoji(GUILD_ID, "<:pog:1122334455667788>", "airhorn")
        payload = _make_payload(
            emoji=discord.PartialEmoji(name="pog", id=1122334455667788)
        )

        asyncio.run(cog.on_raw_reaction_add(payload))

        assert len(cog.mixers[GUILD_ID]._sources) == 1

    def test_reactor_outside_bot_vc_silently_ignored(self, tmp_path):
        """Issue #17: the reacting user must be in the bot's voice channel."""
        cog = _make_reaction_cog(tmp_path, reactor_in_vc=False)
        cog.store.bind_emoji(GUILD_ID, "🎺", "airhorn")

        asyncio.run(cog.on_raw_reaction_add(_make_payload()))

        assert cog.mixers[GUILD_ID]._sources == []
        assert cog.store.get("airhorn")["play_count"] == 0


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

    def test_unbind_works_even_if_key_fails_shape_heuristic(self, tmp_path):
        """A stored binding must always be removable: if the shape heuristic
        tightens and rejects an old key, /unbindemoji falls back to the raw
        string instead of erroring before the store is consulted."""
        cog = _make_cog(tmp_path)
        _add_sound(cog, "airhorn")
        # Simulate a legacy key the current heuristic would reject
        cog.store.bind_emoji(GUILD_ID, "oldkey", "airhorn")
        interaction = _make_guild_interaction()

        asyncio.run(Soundboard.unbindemoji.callback(cog, interaction, "oldkey"))

        assert cog.store.get_emoji_binding(GUILD_ID, "oldkey") is None
        args, _ = interaction.response.send_message.call_args
        assert "Unbound" in args[0]

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


class TestSameVoiceChannelGate:
    """Issue #17: playback requires the invoking user in the bot's VC."""

    def _gated_cog(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")
        cog.pcm_cache = PCMCache(decoder=lambda p: b"\x00" * 7680)
        cog.mixers[GUILD_ID] = MixerSource()
        return cog

    def test_user_not_in_voice_blocked(self, tmp_path):
        cog = self._gated_cog(tmp_path)
        interaction = _make_interaction(voice_client=_connected_vc())
        interaction.user.voice = None

        asyncio.run(cog._play_sound(interaction, "alpha"))

        args, kwargs = interaction.response.send_message.call_args
        assert "need to be in a voice channel" in args[0]
        assert kwargs.get("ephemeral") is True
        assert cog.mixers[GUILD_ID]._sources == []
        assert cog.store.get("alpha")["play_count"] == 0

    def test_user_in_different_channel_blocked_with_both_names(self, tmp_path):
        cog = self._gated_cog(tmp_path)
        vc = _connected_vc()
        interaction = _make_interaction(voice_client=vc)
        vc.channel = MagicMock()
        vc.channel.name = "General"
        interaction.user.voice.channel.name = "AFK"

        asyncio.run(cog._play_sound(interaction, "alpha"))

        args, kwargs = interaction.response.send_message.call_args
        assert "#General" in args[0]
        assert "#AFK" in args[0]
        assert kwargs.get("ephemeral") is True
        assert cog.mixers[GUILD_ID]._sources == []

    def test_user_in_same_channel_allowed(self, tmp_path):
        cog = self._gated_cog(tmp_path)
        interaction = _make_interaction(voice_client=_connected_vc())

        asyncio.run(cog._play_sound(interaction, "alpha"))

        assert len(cog.mixers[GUILD_ID]._sources) == 1
        assert cog.store.get("alpha")["play_count"] == 1

    def test_bot_not_in_voice_message_unchanged(self, tmp_path):
        cog = self._gated_cog(tmp_path)
        interaction = _make_interaction(voice_client=None)

        asyncio.run(cog._play_sound(interaction, "alpha"))

        args, kwargs = interaction.response.send_message.call_args
        assert "Use `/join` first" in args[0]
        assert kwargs.get("ephemeral") is True


class TestAutoLeaveWhenAlone:
    """The bot disconnects after sitting alone in voice for
    config.IDLE_TIMEOUT seconds. _disconnect_if_idle is driven by
    _idle_check_loop in production; tests call it directly with an
    explicit clock so no sleeping is involved."""

    TIMEOUT = 600.0

    @staticmethod
    def _human():
        m = MagicMock()
        m.bot = False
        return m

    @staticmethod
    def _bot_member():
        m = MagicMock()
        m.bot = True
        return m

    def _make_idle_cog(self, tmp_path, monkeypatch, *, members=()):
        from soundbot import config

        monkeypatch.setattr(config, "IDLE_TIMEOUT", self.TIMEOUT)
        cog = _make_cog(tmp_path)
        cog.mixers[GUILD_ID] = MixerSource()
        vc = _connected_vc()
        vc.disconnect = AsyncMock()
        vc.guild.id = GUILD_ID
        vc.channel.name = "General"
        # The bot's own membership is what a real voice-state cache shows
        # for an "empty" channel.
        vc.channel.members = [self._bot_member(), *members]
        cog.bot.voice_clients = [vc]
        return cog, vc

    def test_alone_past_timeout_disconnects_and_cleans_mixer(
        self, tmp_path, monkeypatch
    ):
        cog, vc = self._make_idle_cog(tmp_path, monkeypatch)

        asyncio.run(cog._disconnect_if_idle(1000.0))
        vc.disconnect.assert_not_awaited()

        asyncio.run(cog._disconnect_if_idle(1000.0 + self.TIMEOUT))

        vc.disconnect.assert_awaited_once()
        assert GUILD_ID not in cog.mixers
        assert cog._alone_since == {}

    def test_alone_below_timeout_stays_connected(self, tmp_path, monkeypatch):
        cog, vc = self._make_idle_cog(tmp_path, monkeypatch)

        asyncio.run(cog._disconnect_if_idle(1000.0))
        asyncio.run(cog._disconnect_if_idle(1000.0 + self.TIMEOUT - 1))

        vc.disconnect.assert_not_awaited()
        assert GUILD_ID in cog.mixers
        assert GUILD_ID in cog._alone_since

    def test_human_present_never_starts_timer(self, tmp_path, monkeypatch):
        cog, vc = self._make_idle_cog(
            tmp_path, monkeypatch, members=[self._human()]
        )

        asyncio.run(cog._disconnect_if_idle(1000.0))
        asyncio.run(cog._disconnect_if_idle(1000.0 + self.TIMEOUT * 10))

        vc.disconnect.assert_not_awaited()
        assert cog._alone_since == {}

    def test_human_returning_resets_the_countdown(self, tmp_path, monkeypatch):
        cog, vc = self._make_idle_cog(tmp_path, monkeypatch)

        asyncio.run(cog._disconnect_if_idle(1000.0))  # alone: timer starts
        visitor = self._human()
        vc.channel.members.append(visitor)
        asyncio.run(cog._disconnect_if_idle(1300.0))  # company: timer resets
        vc.channel.members.remove(visitor)
        asyncio.run(cog._disconnect_if_idle(1400.0))  # alone again: restart

        # 600s after the *original* alone-start, but only 200s into the
        # fresh countdown — must still be connected.
        asyncio.run(cog._disconnect_if_idle(1000.0 + self.TIMEOUT))
        vc.disconnect.assert_not_awaited()

        asyncio.run(cog._disconnect_if_idle(1400.0 + self.TIMEOUT))
        vc.disconnect.assert_awaited_once()

    def test_other_bots_do_not_count_as_company(self, tmp_path, monkeypatch):
        cog, vc = self._make_idle_cog(
            tmp_path, monkeypatch, members=[self._bot_member()]
        )

        asyncio.run(cog._disconnect_if_idle(1000.0))
        asyncio.run(cog._disconnect_if_idle(1000.0 + self.TIMEOUT))

        vc.disconnect.assert_awaited_once()

    def test_zero_timeout_disables_auto_leave(self, tmp_path, monkeypatch):
        from soundbot import config

        cog, vc = self._make_idle_cog(tmp_path, monkeypatch)
        monkeypatch.setattr(config, "IDLE_TIMEOUT", 0)

        asyncio.run(cog._disconnect_if_idle(1000.0))
        asyncio.run(cog._disconnect_if_idle(1_000_000.0))

        vc.disconnect.assert_not_awaited()
        assert cog._alone_since == {}

    def test_disconnected_vc_is_skipped_and_timer_pruned(
        self, tmp_path, monkeypatch
    ):
        """A vc that dropped (network blip, /leave mid-pass) must not keep
        a stale timer that would insta-kick the next connection."""
        cog, vc = self._make_idle_cog(tmp_path, monkeypatch)

        asyncio.run(cog._disconnect_if_idle(1000.0))
        assert GUILD_ID in cog._alone_since

        vc.is_connected.return_value = False
        asyncio.run(cog._disconnect_if_idle(1100.0))

        vc.disconnect.assert_not_awaited()
        assert cog._alone_since == {}

    def test_leave_command_uses_shared_teardown(self, tmp_path):
        cog = _make_cog(tmp_path)
        cog.mixers[GUILD_ID] = MixerSource()
        vc = _connected_vc()
        vc.disconnect = AsyncMock()
        interaction = _make_interaction(voice_client=vc)

        asyncio.run(Soundboard.leave.callback(cog, interaction))

        vc.disconnect.assert_awaited_once()
        assert GUILD_ID not in cog.mixers
        args, _ = interaction.followup.send.call_args
        assert "Left" in args[0]


class TestStatsCommand:
    def test_no_plays_yet_message(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")
        interaction = _make_interaction()

        asyncio.run(Soundboard.stats.callback(cog, interaction))

        args, kwargs = interaction.response.send_message.call_args
        assert "No plays" in args[0]
        assert kwargs.get("ephemeral") is True

    def test_embed_lists_top_sounds_and_totals(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")
        _add_sound(cog, "bravo", "bravo.ogg")
        _add_sound(cog, "silent", "silent.ogg")
        for _ in range(3):
            cog.store.increment_play_count("bravo")
        cog.store.increment_play_count("alpha")
        interaction = _make_interaction()

        asyncio.run(Soundboard.stats.callback(cog, interaction))

        _, kwargs = interaction.response.send_message.call_args
        embed = kwargs["embed"]
        assert "1. `bravo` — 3 plays" in embed.description
        assert "2. `alpha` — 1 play" in embed.description
        assert "silent" not in embed.description
        assert "3 sounds" in embed.footer.text
        assert "4 total plays" in embed.footer.text


class TestBoardCleanup:
    """Boards posted by /board are deleted once the bot leaves voice
    (whether by /leave, idle auto-leave, or someone disconnecting it) —
    their buttons are useless with no bot in the channel. Every leave
    route surfaces as the bot's own voice-state update, so that listener
    is the single trigger."""

    BOT_ID = 999

    def _make_board_cog(self, tmp_path):
        from soundbot.boards import BoardTracker

        cog = _make_cog(tmp_path)
        cog.boards = BoardTracker(tmp_path / "boards.json")
        cog.bot.user.id = self.BOT_ID
        deleted = []
        errors = {}

        def partial_messageable(channel_id):
            channel = MagicMock()

            def partial_message(message_id):
                message = MagicMock()

                async def delete():
                    exc = errors.get(message_id)
                    if exc is not None:
                        raise exc
                    deleted.append((channel_id, message_id))

                message.delete = delete
                return message

            channel.get_partial_message = partial_message
            return channel

        cog.bot.get_partial_messageable = partial_messageable
        return cog, deleted, errors

    def _voice_update(self, cog, *, member_id, before, after, guild_id=GUILD_ID):
        member = MagicMock()
        member.id = member_id
        member.guild.id = guild_id
        before_state = MagicMock()
        before_state.channel = before
        after_state = MagicMock()
        after_state.channel = after
        asyncio.run(cog.on_voice_state_update(member, before_state, after_state))

    @staticmethod
    def _http_error(cls, status):
        response = MagicMock()
        response.status = status
        response.reason = "nope"
        return cls(response, "nope")

    def test_board_command_records_every_posted_message(self, tmp_path):
        cog, _, _ = self._make_board_cog(tmp_path)
        for i in range(30):  # 30 sounds -> two board messages
            _add_sound(cog, f"s{i}", f"s{i}.ogg")
        interaction = _make_interaction()
        interaction.guild_id = GUILD_ID
        interaction.channel_id = 42
        interaction.followup.send = AsyncMock(
            side_effect=[MagicMock(id=1001), MagicMock(id=1002)]
        )

        asyncio.run(Soundboard.board.callback(cog, interaction))

        assert cog.boards.pop_guild(GUILD_ID) == [(42, 1001), (42, 1002)]

    def test_bot_leaving_voice_deletes_that_guilds_boards(self, tmp_path):
        cog, deleted, _ = self._make_board_cog(tmp_path)
        cog.boards.add(GUILD_ID, channel_id=42, message_id=1001)
        cog.boards.add(GUILD_ID, channel_id=43, message_id=1002)
        cog.boards.add(777, channel_id=70, message_id=7001)  # other guild

        self._voice_update(
            cog, member_id=self.BOT_ID, before=MagicMock(), after=None
        )

        assert deleted == [(42, 1001), (43, 1002)]
        assert cog.boards.pop_guild(GUILD_ID) == []
        # The other guild's bot is still in voice; its board stays.
        assert cog.boards.pop_guild(777) == [(70, 7001)]

    def test_bot_moved_between_channels_keeps_boards(self, tmp_path):
        cog, deleted, _ = self._make_board_cog(tmp_path)
        cog.boards.add(GUILD_ID, channel_id=42, message_id=1001)

        self._voice_update(
            cog, member_id=self.BOT_ID, before=MagicMock(), after=MagicMock()
        )

        assert deleted == []

    def test_other_members_leaving_is_ignored(self, tmp_path):
        cog, deleted, _ = self._make_board_cog(tmp_path)
        cog.boards.add(GUILD_ID, channel_id=42, message_id=1001)

        self._voice_update(cog, member_id=1, before=MagicMock(), after=None)

        assert deleted == []
        assert cog.boards.pop_guild(GUILD_ID) == [(42, 1001)]

    def test_delete_failures_do_not_stop_the_rest(self, tmp_path):
        """A board someone already deleted (404) or one in a channel the
        bot lost access to (403) must not strand the remaining boards."""
        cog, deleted, errors = self._make_board_cog(tmp_path)
        cog.boards.add(GUILD_ID, channel_id=42, message_id=1)
        cog.boards.add(GUILD_ID, channel_id=42, message_id=2)
        cog.boards.add(GUILD_ID, channel_id=42, message_id=3)
        errors[1] = self._http_error(discord.NotFound, 404)
        errors[2] = self._http_error(discord.Forbidden, 403)

        self._voice_update(
            cog, member_id=self.BOT_ID, before=MagicMock(), after=None
        )

        assert deleted == [(42, 3)]
        assert cog.boards.pop_guild(GUILD_ID) == []

    def test_startup_purges_boards_orphaned_by_restart(self, tmp_path):
        from soundbot.boards import BoardTracker

        # Written by the previous process; its buttons died with it.
        BoardTracker(tmp_path / "boards.json").add(
            GUILD_ID, channel_id=42, message_id=1001
        )
        cog, deleted, _ = self._make_board_cog(tmp_path)

        asyncio.run(cog._purge_orphaned_boards())

        assert deleted == [(42, 1001)]
        assert cog.boards.pop_all() == []


class TestRenameSoundCommand:
    def _rename(self, cog, old, new):
        interaction = _make_interaction()
        asyncio.run(Soundboard.renamesound.callback(cog, interaction, old, new))
        args, kwargs = interaction.response.send_message.call_args
        return args[0], kwargs.get("ephemeral")

    def test_renames_and_persists(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")

        msg, ephemeral = self._rename(cog, "alpha", "bravo")

        assert "Renamed **alpha** to **bravo**" in msg
        assert not ephemeral
        assert cog.store.get("bravo") is not None
        assert cog.store.get("alpha") is None

    def test_unknown_sound_message_is_not_repr_quoted(self, tmp_path):
        """str(KeyError) wraps the message in quotes, so the user saw
        "Sound 'ghost' not found" *including* the outer quotes."""
        cog = _make_cog(tmp_path)

        msg, ephemeral = self._rename(cog, "ghost", "bravo")

        assert msg == "Sound 'ghost' not found"
        assert ephemeral is True

    def test_existing_target_name_is_rejected(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")
        _add_sound(cog, "bravo", "bravo.ogg")

        msg, ephemeral = self._rename(cog, "alpha", "bravo")

        assert msg == "Sound 'bravo' already exists"
        assert ephemeral is True
        assert cog.store.get("alpha") is not None

    def test_invalid_new_name_is_rejected(self, tmp_path):
        cog = _make_cog(tmp_path)
        _add_sound(cog, "alpha")

        msg, ephemeral = self._rename(cog, "alpha", "bad name!")

        assert ephemeral is True
        assert cog.store.get("alpha") is not None


class TestUserMessage:
    def test_key_error_is_unquoted(self):
        from soundbot.bot import user_message

        assert user_message(KeyError("Sound 'x' not found")) == "Sound 'x' not found"

    def test_value_error_passes_through(self):
        from soundbot.bot import user_message

        assert user_message(ValueError("bad")) == "bad"

    def test_argless_exception_does_not_crash(self):
        from soundbot.bot import user_message

        assert user_message(KeyError()) == "Something went wrong."


class TestJoinLeaveReplyDeadline:
    """Discord voids an interaction not acknowledged within 3 seconds, and
    the voice handshake behind connect()/disconnect() routinely takes
    longer — the join worked but the reply 404'd as "Unknown interaction".
    Both commands must defer before the slow await, then follow up."""

    def test_join_defers_before_connecting(self, tmp_path):
        cog = _make_cog(tmp_path)
        interaction = _make_interaction()
        interaction.user.voice.channel.name = "General"
        order = []
        interaction.response.defer = AsyncMock(
            side_effect=lambda *a, **k: order.append("defer")
        )

        async def connect():
            order.append("connect")
            return MagicMock()

        interaction.user.voice.channel.connect = connect

        asyncio.run(Soundboard.join.callback(cog, interaction))

        assert order == ["defer", "connect"]
        interaction.response.send_message.assert_not_called()
        args, _ = interaction.followup.send.call_args
        assert "Joined **General**" in args[0]

    def test_join_move_also_defers(self, tmp_path):
        existing = _connected_vc()
        existing.move_to = AsyncMock()
        cog = _make_cog(tmp_path)
        interaction = _make_interaction(voice_client=existing)

        asyncio.run(Soundboard.join.callback(cog, interaction))

        interaction.response.defer.assert_awaited_once()
        existing.move_to.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()

    def test_join_without_voice_replies_immediately(self, tmp_path):
        """Nothing slow happens on this path, so it keeps the direct
        ephemeral reply rather than a deferred (public) "thinking..."."""
        cog = _make_cog(tmp_path)
        interaction = _make_interaction()
        interaction.user.voice = None

        asyncio.run(Soundboard.join.callback(cog, interaction))

        interaction.response.defer.assert_not_called()
        _, kwargs = interaction.response.send_message.call_args
        assert kwargs.get("ephemeral") is True

    def test_leave_defers_before_disconnecting(self, tmp_path):
        cog = _make_cog(tmp_path)
        vc = _connected_vc()
        order = []
        interaction = _make_interaction(voice_client=vc)
        interaction.response.defer = AsyncMock(
            side_effect=lambda *a, **k: order.append("defer")
        )
        vc.disconnect = AsyncMock(side_effect=lambda *a, **k: order.append("disconnect"))

        asyncio.run(Soundboard.leave.callback(cog, interaction))

        assert order == ["defer", "disconnect"]
        args, _ = interaction.followup.send.call_args
        assert "Left" in args[0]


class TestShutdownSignalHandler:
    """`docker stop` sends SIGTERM. Python's default action kills the
    process outright, skipping Bot.close() — so cog_unload's final save
    never ran and up to a minute of play counts was lost."""

    def test_sigterm_closes_the_bot(self):
        import signal

        from soundbot.bot import install_shutdown_handler

        bot = MagicMock()
        bot.close = AsyncMock()
        loop = MagicMock()

        async def scenario():
            assert install_shutdown_handler(bot, loop) is True
            sig, callback = loop.add_signal_handler.call_args.args
            assert sig == signal.SIGTERM
            callback()
            # The handler schedules close(); let it run.
            await asyncio.sleep(0)

        asyncio.run(scenario())
        bot.close.assert_awaited_once()

    def test_unsupported_platform_is_tolerated(self):
        """Windows event loops raise NotImplementedError for signal
        handlers; that must not break startup (Ctrl+C still works there)."""
        from soundbot.bot import install_shutdown_handler

        loop = MagicMock()
        loop.add_signal_handler.side_effect = NotImplementedError

        assert install_shutdown_handler(MagicMock(), loop) is False


class TestMatchesRef:
    """Auto-join config names a guild or channel by snowflake id or by name.
    _matches_ref is pure, so it is tested directly rather than through a
    voice-state update."""

    def test_digits_match_the_id(self):
        assert _matches_ref("42", 42, "Chillin") is True

    def test_digits_never_match_a_name(self):
        """The documented trade-off: a channel literally named "42" can only
        be configured by its id, because an all-digit ref is read as one."""
        assert _matches_ref("42", 99, "42") is False

    def test_name_matches_exactly(self):
        assert _matches_ref("Chillin", 1, "Chillin") is True

    def test_name_match_ignores_case(self):
        assert _matches_ref("CHILLIN", 1, "chillin") is True

    def test_name_match_folds_rather_than_lowercases(self):
        """casefold, not lower: Discord channel names are free-form Unicode,
        and lower() would miss equivalences like this one."""
        assert _matches_ref("STRASSE", 1, "straße") is True

    def test_different_name_does_not_match(self):
        assert _matches_ref("Chillin", 1, "General") is False

    def test_surrounding_whitespace_is_ignored(self):
        assert _matches_ref("  Chillin  ", 1, "Chillin") is True

    @pytest.mark.parametrize("ref", ["", "   "])
    def test_blank_ref_matches_nothing(self, ref):
        """config.py already drops blanks; this keeps the helper safe for any
        later caller that does not."""
        assert _matches_ref(ref, 1, "Chillin") is False

    def test_nameless_object_only_matches_by_id(self):
        assert _matches_ref("Chillin", 1, None) is False
        assert _matches_ref("1", 1, None) is True


class TestAutoJoinWatchedChannels:
    """The bot joins a watched voice channel by itself the moment a human
    lands in it, scoped to the one guild named by AUTO_JOIN_GUILD so other
    servers never get surprise joins.

    The mute window after a deliberate exit is clock-driven, so tests read
    the deadline the code set and probe either side of it rather than
    patching time.monotonic — asyncio runs on that same clock.
    """

    GUILD_NAME = "Anti-Union"
    WATCHED = ("Chillin", "Deadlock", "CS2")
    COOLDOWN = 300.0
    BOT_ID = 999
    NOW = 1000.0

    @pytest.fixture
    def cog(self, tmp_path, monkeypatch):
        from soundbot import config

        monkeypatch.setattr(config, "AUTO_JOIN_GUILD", self.GUILD_NAME)
        monkeypatch.setattr(config, "AUTO_JOIN_CHANNELS", self.WATCHED)
        monkeypatch.setattr(config, "AUTO_JOIN_COOLDOWN", self.COOLDOWN)
        cog = _make_cog(tmp_path)
        cog.bot.user.id = self.BOT_ID
        cog.bot.voice_clients = []
        return cog

    def _channel(self, name, *, channel_id=1, guild_id=GUILD_ID, guild_name=None):
        channel = MagicMock()
        # Plain assignment: MagicMock(name=...) names the mock, not the attr.
        channel.name = name
        channel.id = channel_id
        channel.guild.id = guild_id
        channel.guild.name = self.GUILD_NAME if guild_name is None else guild_name
        channel.guild.voice_client = None
        channel.connect = AsyncMock(return_value=MagicMock())
        return channel

    def _member(self, channel, *, is_bot=False):
        member = MagicMock()
        member.id = 1234
        member.bot = is_bot
        member.guild = channel.guild
        return member

    def _arrive(self, cog, channel, *, member=None, before=None, now=None):
        """Dispatch "someone moved into `channel`" at `now`."""
        member = self._member(channel) if member is None else member
        before_state = MagicMock()
        before_state.channel = before
        after_state = MagicMock()
        after_state.channel = channel
        asyncio.run(
            cog._maybe_autojoin(
                member,
                before_state,
                after_state,
                self.NOW if now is None else now,
            )
        )

    def _leave_interaction(self, *, voice_client, guild_id=GUILD_ID, guild_name=None):
        """An interaction from the auto-join guild, so a mute can arm."""
        interaction = _make_interaction(voice_client=voice_client)
        interaction.guild.id = guild_id
        interaction.guild.name = self.GUILD_NAME if guild_name is None else guild_name
        return interaction

    def _bot_left_voice(self, cog, *, guild_id=GUILD_ID):
        """Dispatch the bot's own "disconnected from voice" update."""
        member = MagicMock()
        member.id = self.BOT_ID
        member.guild.id = guild_id
        member.guild.name = self.GUILD_NAME
        before_state = MagicMock()
        before_state.channel = MagicMock()
        after_state = MagicMock()
        after_state.channel = None
        asyncio.run(cog.on_voice_state_update(member, before_state, after_state))

    # -- Matching --

    def test_human_joining_watched_channel_connects_and_starts_mixer(self, cog):
        channel = self._channel("Chillin")

        self._arrive(cog, channel)

        channel.connect.assert_awaited_once()
        vc = channel.connect.return_value
        vc.play.assert_called_once_with(cog.mixers[GUILD_ID])

    @pytest.mark.parametrize("name", ["Chillin", "Deadlock", "CS2"])
    def test_every_configured_channel_is_watched(self, cog, name):
        channel = self._channel(name)

        self._arrive(cog, channel)

        channel.connect.assert_awaited_once()

    def test_unwatched_channel_in_the_same_guild_is_ignored(self, cog):
        channel = self._channel("General")

        self._arrive(cog, channel)

        channel.connect.assert_not_awaited()
        assert cog.mixers == {}

    def test_watched_name_in_another_guild_is_ignored(self, cog):
        """The whole point of the guild scope: a Deadlock channel on some
        other server must not pull the bot in."""
        channel = self._channel(
            "Deadlock", guild_id=777, guild_name="Some Other Server"
        )

        self._arrive(cog, channel)

        channel.connect.assert_not_awaited()

    def test_channel_name_match_is_case_insensitive(self, cog):
        channel = self._channel("chillin")

        self._arrive(cog, channel)

        channel.connect.assert_awaited_once()

    def test_guild_and_channels_may_be_given_as_ids(self, cog, monkeypatch):
        """Ids survive a channel rename, so config takes either form."""
        from soundbot import config

        monkeypatch.setattr(config, "AUTO_JOIN_GUILD", str(GUILD_ID))
        monkeypatch.setattr(config, "AUTO_JOIN_CHANNELS", ("42",))
        channel = self._channel("Renamed Since", channel_id=42, guild_name="Renamed")

        self._arrive(cog, channel)

        channel.connect.assert_awaited_once()

    def test_id_config_does_not_match_a_different_channel(self, cog, monkeypatch):
        from soundbot import config

        monkeypatch.setattr(config, "AUTO_JOIN_CHANNELS", ("42",))
        channel = self._channel("Chillin", channel_id=43)

        self._arrive(cog, channel)

        channel.connect.assert_not_awaited()

    def test_unset_channels_disables_autojoin(self, cog, monkeypatch):
        from soundbot import config

        monkeypatch.setattr(config, "AUTO_JOIN_CHANNELS", ())
        channel = self._channel("Chillin")

        self._arrive(cog, channel)

        channel.connect.assert_not_awaited()

    def test_unset_guild_disables_autojoin(self, cog, monkeypatch):
        """A channel list with no guild scope is ambiguous, not global —
        it must stay off rather than fire on every server."""
        from soundbot import config

        monkeypatch.setattr(config, "AUTO_JOIN_GUILD", "")
        channel = self._channel("Chillin")

        self._arrive(cog, channel)

        channel.connect.assert_not_awaited()

    # -- Which events count --

    def test_other_bots_do_not_trigger_a_join(self, cog):
        channel = self._channel("Chillin")
        member = self._member(channel, is_bot=True)

        self._arrive(cog, channel, member=member)

        channel.connect.assert_not_awaited()

    def test_same_channel_update_is_ignored(self, cog):
        """Mute, deafen and go-live all fire voice_state_update with the
        channel unchanged — none of them is an arrival."""
        channel = self._channel("Chillin")

        self._arrive(cog, channel, before=channel)

        channel.connect.assert_not_awaited()

    def test_leaving_a_watched_channel_is_ignored(self, cog):
        channel = self._channel("Chillin")
        member = self._member(channel)
        before_state = MagicMock()
        before_state.channel = channel
        after_state = MagicMock()
        after_state.channel = None

        asyncio.run(cog._maybe_autojoin(member, before_state, after_state, self.NOW))

        channel.connect.assert_not_awaited()

    def test_listener_routes_human_updates_to_autojoin(self, cog):
        """Wiring check: the board-cleanup listener must not swallow
        everyone else's voice-state updates."""
        channel = self._channel("Chillin")
        member = self._member(channel)
        before_state = MagicMock()
        before_state.channel = None
        after_state = MagicMock()
        after_state.channel = channel

        asyncio.run(cog.on_voice_state_update(member, before_state, after_state))

        channel.connect.assert_awaited_once()

    # -- Staying put --

    def test_already_in_voice_in_that_guild_stays_put(self, cog):
        """Bot is in Chillin, someone joins Deadlock: following them would
        yank it away from whoever is still in Chillin."""
        channel = self._channel("Deadlock", channel_id=2)
        channel.guild.voice_client = _connected_vc()

        self._arrive(cog, channel)

        channel.connect.assert_not_awaited()

    def test_concurrent_arrivals_connect_only_once(self, cog):
        """Two people joining together dispatch two updates, and the second
        must not fire its own connect() while the first is mid-handshake.

        discord.py does register guild.voice_client before connect() awaits,
        so the check above would catch this anyway; mocking connect() out
        removes that safety net, which is the point -- this pins the
        in-flight guard on its own."""
        channel = self._channel("Chillin")
        member = self._member(channel)
        before_state = MagicMock()
        before_state.channel = None
        after_state = MagicMock()
        after_state.channel = channel

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_connect():
                started.set()
                await release.wait()
                return MagicMock()

            channel.connect = AsyncMock(side_effect=slow_connect)
            first = asyncio.create_task(
                cog._maybe_autojoin(member, before_state, after_state, self.NOW)
            )
            await started.wait()
            await cog._maybe_autojoin(member, before_state, after_state, self.NOW)
            release.set()
            await first

        asyncio.run(scenario())

        assert channel.connect.await_count == 1

    def test_connect_failure_is_logged_and_the_next_arrival_retries(
        self, cog, caplog
    ):
        """discord.py turns a listener exception into a bare traceback; a
        channel the bot cannot enter must log the cause instead, and must not
        poison the guild against later attempts."""
        caplog.set_level(logging.WARNING, logger="soundbot")
        failing = self._channel("Chillin")
        failing.connect = AsyncMock(
            side_effect=discord.ClientException("no Connect permission")
        )

        self._arrive(cog, failing)

        assert cog.mixers == {}
        assert GUILD_ID not in cog._autojoin_pending
        assert "no Connect permission" in caplog.text
        # A failure is not a mute: the comment promises the next arrival
        # retries, so pin that rather than trusting it.
        assert cog._autojoin_muted_until == {}
        retry = self._channel("Chillin")
        self._arrive(cog, retry)
        retry.connect.assert_awaited_once()

    # -- Mute window after a deliberate exit --

    def test_manual_leave_mutes_then_rearms_autojoin(self, cog):
        vc = _connected_vc()
        vc.disconnect = AsyncMock()
        cog.mixers[GUILD_ID] = MixerSource()

        asyncio.run(
            Soundboard.leave.callback(cog, self._leave_interaction(voice_client=vc))
        )

        until = cog._autojoin_muted_until[GUILD_ID]
        muted = self._channel("Chillin")
        self._arrive(cog, muted, now=until - 1)
        muted.connect.assert_not_awaited()

        rearmed = self._channel("Chillin")
        self._arrive(cog, rearmed, now=until)
        rearmed.connect.assert_awaited_once()
        assert GUILD_ID not in cog._autojoin_muted_until

    def test_manual_join_clears_the_mute(self, cog):
        """/join says "be here", so it must undo an earlier /leave's mute —
        otherwise auto-join stays silently dead for the rest of the window."""
        leaving = _connected_vc()
        leaving.disconnect = AsyncMock()
        asyncio.run(
            Soundboard.leave.callback(
                cog, self._leave_interaction(voice_client=leaving)
            )
        )
        assert GUILD_ID in cog._autojoin_muted_until

        rejoin = self._leave_interaction(voice_client=None)
        rejoin.user.voice.channel.connect = AsyncMock(return_value=MagicMock())
        asyncio.run(Soundboard.join.callback(cog, rejoin))

        assert cog._autojoin_muted_until == {}

    def test_leave_in_an_unwatched_guild_records_no_mute(self, cog):
        """A /leave anywhere else must not leave a deadline behind: only the
        auto-join guild is ever checked, so nothing would prune it."""
        vc = _connected_vc()
        vc.disconnect = AsyncMock()
        interaction = self._leave_interaction(
            voice_client=vc, guild_id=777, guild_name="Some Other Server"
        )

        asyncio.run(Soundboard.leave.callback(cog, interaction))

        assert cog._autojoin_muted_until == {}
        channel = self._channel("Chillin")
        self._arrive(cog, channel)

        channel.connect.assert_awaited_once()

    def test_zero_cooldown_disables_muting(self, cog, monkeypatch):
        from soundbot import config

        monkeypatch.setattr(config, "AUTO_JOIN_COOLDOWN", 0)
        vc = _connected_vc()
        vc.disconnect = AsyncMock()

        asyncio.run(
            Soundboard.leave.callback(cog, self._leave_interaction(voice_client=vc))
        )

        assert cog._autojoin_muted_until == {}
        channel = self._channel("Chillin")
        self._arrive(cog, channel)
        channel.connect.assert_awaited_once()

    def test_idle_auto_leave_does_not_mute_autojoin(self, cog, monkeypatch):
        """Auto-leave means nobody is here, not go away — the next person
        to arrive should still get the bot."""
        from soundbot import config

        monkeypatch.setattr(config, "IDLE_TIMEOUT", 600.0)
        vc = _connected_vc()
        vc.disconnect = AsyncMock()
        vc.channel.name = "Chillin"
        bot_member = MagicMock()
        bot_member.bot = True
        vc.channel.members = [bot_member]
        cog.mixers[GUILD_ID] = MixerSource()
        cog.bot.voice_clients = [vc]

        asyncio.run(cog._disconnect_if_idle(1000.0))
        asyncio.run(cog._disconnect_if_idle(1600.0))
        vc.disconnect.assert_awaited_once()

        assert cog._autojoin_muted_until == {}
        channel = self._channel("Chillin")
        self._arrive(cog, channel)
        channel.connect.assert_awaited_once()

    def test_external_disconnect_mutes_autojoin_and_clears_stray_mixer(self, cog):
        """Someone hitting Disconnect on the bot in Discord is as
        deliberate as /leave, and leaves the guild mixer orphaned because
        no teardown ran."""
        mixer = MixerSource()
        cog.mixers[GUILD_ID] = mixer

        self._bot_left_voice(cog)

        assert GUILD_ID not in cog.mixers
        assert mixer._sources == []
        until = cog._autojoin_muted_until[GUILD_ID]
        channel = self._channel("Chillin")
        self._arrive(cog, channel, now=until - 1)
        channel.connect.assert_not_awaited()

    def test_external_disconnect_still_deletes_the_boards(self, cog, tmp_path):
        """The stray-mixer branch runs ahead of board cleanup in the same
        method, so the two have to be exercised together: an early return
        added there later would strand boards with a green suite."""
        from soundbot.boards import BoardTracker

        cog.boards = BoardTracker(tmp_path / "boards.json")
        cog.boards.add(GUILD_ID, channel_id=42, message_id=1001)
        cog.mixers[GUILD_ID] = MixerSource()
        deleted = []

        def partial_messageable(channel_id):
            messageable = MagicMock()

            def partial_message(message_id):
                message = MagicMock()

                async def delete():
                    deleted.append((channel_id, message_id))

                message.delete = delete
                return message

            messageable.get_partial_message = partial_message
            return messageable

        cog.bot.get_partial_messageable = partial_messageable

        self._bot_left_voice(cog)

        assert deleted == [(42, 1001)]
        assert GUILD_ID not in cog.mixers
        assert GUILD_ID in cog._autojoin_muted_until

    def test_the_bots_own_arrival_does_not_recurse_into_autojoin(self, cog):
        """The bot joining a watched channel fires its own voice-state
        update; routing that back into auto-join would loop."""
        channel = self._channel("Chillin")
        member = MagicMock()
        member.id = self.BOT_ID
        member.bot = True
        member.guild = channel.guild
        before_state = MagicMock()
        before_state.channel = None
        after_state = MagicMock()
        after_state.channel = channel

        asyncio.run(cog.on_voice_state_update(member, before_state, after_state))

        channel.connect.assert_not_awaited()

    def test_teardown_exit_leaves_no_stray_mixer_to_mute_on(self, cog):
        """_teardown_voice drops the mixer before awaiting disconnect, so
        by the time the bot's own update lands there is nothing parked —
        that absence is how an external kick is told apart from our own."""
        vc = _connected_vc()
        vc.disconnect = AsyncMock()
        cog.mixers[GUILD_ID] = MixerSource()

        asyncio.run(cog._teardown_voice(vc))
        self._bot_left_voice(cog)

        assert cog._autojoin_muted_until == {}
