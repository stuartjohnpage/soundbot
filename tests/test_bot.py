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

from soundbot.bot import Soundboard
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
        from soundbot import config

        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        monkeypatch.setattr(config, "SOUNDS_DIR", sounds_dir)
        monkeypatch.setattr(config, "MAX_DURATION", 60)
        monkeypatch.setattr("soundbot.bot.has_video_stream", lambda p: False)
        monkeypatch.setattr("soundbot.bot.validate_sound", lambda p, d: None)
        return cog, sounds_dir

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
        from soundbot import config

        cog = _make_cog(tmp_path)
        sounds_dir = Path(cog.store._sounds_dir)
        monkeypatch.setattr(config, "SOUNDS_DIR", sounds_dir)
        monkeypatch.setattr(config, "MAX_DURATION", 60)

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

        monkeypatch.setattr("soundbot.bot.has_video_stream", lambda p: False)
        monkeypatch.setattr("soundbot.bot.validate_sound", lambda p, d: None)

        interaction = _make_interaction()
        asyncio.run(
            Soundboard.addsound.callback(cog, interaction, "thing", attachment)
        )

        assert cache_key not in cog.pcm_cache
