"""Tests for the shared upload-ingest pipeline.

The pipeline is the post-save half of what /addsound has always done:
video-extract, duration-validate, loudness-normalize, PCM-cache
invalidate, and register in the store. It was extracted from bot.py so
the web panel's upload route runs the exact same code path (issue #1)
instead of a diverging copy.

Happy-path tests use real WAV files generated with the stdlib `wave`
module and run real ffmpeg/ffprobe — skipped when FFmpeg is not
installed, same convention as test_audio.py.
"""
from pathlib import Path

import pytest

from soundbot.ingest import process_upload
from soundbot.pcm_cache import PCMCache
from soundbot.store import SoundStore
from tests.helpers import (
    make_mp3_with_cover_art,
    make_mp4,
    make_wav,
    skip_no_ffmpeg,
)

_skip_no_ffmpeg = skip_no_ffmpeg


def _make_store(tmp_path) -> tuple[SoundStore, Path]:
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    store = SoundStore(
        metadata_path=tmp_path / "sounds.json",
        sounds_dir=sounds_dir,
    )
    return store, sounds_dir


class TestProcessUploadHappyPath:
    @_skip_no_ffmpeg
    def test_valid_loud_wav_is_registered_and_normalized(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)
        dest = make_wav(sounds_dir / "horn.wav")

        final, gain, trimmed_from = process_upload(
            dest,
            store=store,
            pcm_cache=PCMCache(),
            name="horn",
            category="memes",
            tags=["meme", "loud"],
            uploaded_by="web-admin",
            max_duration=6.4,
            target_lufs=-16.0,
        )

        assert final == dest
        assert dest.exists()
        # A ~-3 LUFS tone against a -16 target must be attenuated.
        assert gain is not None and gain < 0
        # 1s file under a 6.4s cap: no trim
        assert trimmed_from is None
        entry = store.get("horn")
        assert entry is not None
        assert entry["file"] == str(dest)
        assert entry["category"] == "memes"
        assert entry["uploaded_by"] == "web-admin"
        assert set(entry["tags"]) == {"loud", "meme"}


class TestProcessUploadCoverArt:
    @_skip_no_ffmpeg
    def test_mp3_with_cover_art_is_accepted_in_place(self, tmp_path):
        """Album art probes as a video stream; treating it as one sent the
        upload down the extract branch, whose .mp3 destination is the
        upload itself -> "already exists" and the file got deleted."""
        store, sounds_dir = _make_store(tmp_path)
        dest = make_mp3_with_cover_art(sounds_dir / "art.mp3")

        final, _gain, _trimmed = process_upload(
            dest,
            store=store,
            pcm_cache=PCMCache(),
            name="art",
            category=None,
            tags=[],
            uploaded_by="tester",
            max_duration=6.4,
            target_lufs=-16.0,
        )

        assert final == dest
        assert dest.exists()
        assert store.get("art")["file"] == str(dest)


class TestProcessUploadVideoBranch:
    @_skip_no_ffmpeg
    def test_video_upload_extracts_audio_and_drops_video(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)
        dest = make_mp4(sounds_dir / "clip.mp4")

        final, _gain, _trimmed = process_upload(
            dest,
            store=store,
            pcm_cache=PCMCache(),
            name="clip",
            category=None,
            tags=[],
            uploaded_by=None,
            max_duration=6.4,
            target_lufs=-16.0,
        )

        assert final == sounds_dir / "clip.mp3"
        assert final.exists()
        assert not dest.exists()
        assert store.get("clip")["file"] == str(final)

    def test_extraction_target_on_disk_is_refused(self, tmp_path, monkeypatch):
        """A file already sitting at the would-be .mp3 path must not be
        clobbered by the extraction."""
        store, sounds_dir = _make_store(tmp_path)
        monkeypatch.setattr("soundbot.ingest.has_video_stream", lambda p: True)
        existing = sounds_dir / "clip.mp3"
        existing.write_bytes(b"someone else's bytes")
        dest = sounds_dir / "clip.mp4"
        dest.write_bytes(b"fake video")

        with pytest.raises(ValueError, match="already exists"):
            process_upload(
                dest,
                store=store,
                pcm_cache=PCMCache(),
                name="clip",
                category=None,
                tags=[],
                uploaded_by=None,
                max_duration=6.4,
                target_lufs=-16.0,
            )

        assert existing.read_bytes() == b"someone else's bytes"
        assert not dest.exists()

    def test_extraction_target_owned_by_entry_is_refused(self, tmp_path, monkeypatch):
        """A store entry can own the .mp3 path even when the file was
        manually deleted off disk — the .exists() check alone misses it."""
        store, sounds_dir = _make_store(tmp_path)
        monkeypatch.setattr("soundbot.ingest.has_video_stream", lambda p: True)
        owned = sounds_dir / "clip.mp3"
        owned.write_bytes(b"x")
        store.add("other", owned)
        owned.unlink()
        dest = sounds_dir / "clip.mp4"
        dest.write_bytes(b"fake video")

        with pytest.raises(ValueError, match="other"):
            process_upload(
                dest,
                store=store,
                pcm_cache=PCMCache(),
                name="clip",
                category=None,
                tags=[],
                uploaded_by=None,
                max_duration=6.4,
                target_lufs=-16.0,
            )

        assert not dest.exists()
        assert store.get("clip") is None


class TestProcessUploadRejection:
    def _kwargs(self, store):
        return dict(
            store=store,
            pcm_cache=PCMCache(),
            name="bad",
            category=None,
            tags=[],
            uploaded_by=None,
            max_duration=6.4,
            target_lufs=-16.0,
        )

    @_skip_no_ffmpeg
    def test_unreadable_file_raises_and_is_deleted(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)
        dest = sounds_dir / "junk.mp3"
        dest.write_bytes(b"this is not audio")

        with pytest.raises(ValueError):
            process_upload(dest, **self._kwargs(store))

        assert not dest.exists()
        assert store.get("bad") is None

    @_skip_no_ffmpeg
    def test_over_length_file_is_trimmed_not_rejected(self, tmp_path):
        """Issue #20: an upload past the cap is cut to the first
        max_duration seconds and registered, instead of erroring."""
        from soundbot.audio import get_duration

        store, sounds_dir = _make_store(tmp_path)
        dest = make_wav(sounds_dir / "long.wav", duration=3.0)

        final, _gain, trimmed_from = process_upload(
            dest, **{**self._kwargs(store), "max_duration": 1.0}
        )

        assert final.exists()
        assert store.get("bad") is not None
        assert trimmed_from == pytest.approx(3.0, abs=0.1)
        # Stream-copy cut can overshoot by ~a packet; allow slack.
        assert get_duration(final) <= 1.3

    @_skip_no_ffmpeg
    def test_trim_failure_rejects_and_deletes(self, tmp_path, monkeypatch):
        """If the trim itself fails, behavior matches the old over-length
        rejection: ValueError, file gone, nothing registered."""
        store, sounds_dir = _make_store(tmp_path)
        dest = make_wav(sounds_dir / "long.wav", duration=3.0)

        def boom(path, max_duration):
            raise ValueError(f"Failed to trim {path}")

        monkeypatch.setattr("soundbot.ingest.trim_audio", boom)

        with pytest.raises(ValueError, match="Failed to trim"):
            process_upload(dest, **{**self._kwargs(store), "max_duration": 1.0})

        assert not dest.exists()
        assert store.get("bad") is None


class TestPrecheckUpload:
    """The pre-save checks shared by /addsound and the web upload route.
    They used to be two hand-maintained copies; one function means a rule
    change can't land on only one front end."""

    def _check(self, store, sounds_dir, *, name="horn", tags=None,
               filename="horn.wav", markdown=False):
        from soundbot.ingest import precheck_upload

        return precheck_upload(
            store, sounds_dir, name=name, tags=tags, filename=filename,
            markdown=markdown,
        )

    def test_returns_destination_and_parsed_tags(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)

        dest, tags = self._check(store, sounds_dir, tags="Meme, loud")

        assert dest == sounds_dir / "horn.wav"
        assert tags == ["meme", "loud"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"name": "bad name!"},
            {"tags": "ok,bad tag!"},
            {"filename": ""},
        ],
    )
    def test_bad_input_is_invalid(self, tmp_path, kwargs):
        from soundbot.ingest import UploadRejected

        store, sounds_dir = _make_store(tmp_path)

        with pytest.raises(UploadRejected) as exc_info:
            self._check(store, sounds_dir, **kwargs)
        assert exc_info.value.kind == "invalid"

    def test_path_components_are_stripped_from_filename(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)

        dest, _ = self._check(store, sounds_dir, filename="../../etc/horn.wav")

        assert dest == sounds_dir / "horn.wav"

    def test_duplicate_name_explains_tags(self, tmp_path):
        from soundbot.ingest import UploadRejected

        store, sounds_dir = _make_store(tmp_path)
        existing = sounds_dir / "old.wav"
        existing.write_bytes(b"x")
        store.add("horn", existing)
        store.add_tag("horn", "elsewhere")

        with pytest.raises(UploadRejected) as exc_info:
            self._check(store, sounds_dir)
        assert exc_info.value.kind == "duplicate"
        assert "elsewhere" in str(exc_info.value)

    def test_plain_text_messages_carry_no_discord_markdown(self, tmp_path):
        from soundbot.ingest import UploadRejected

        store, sounds_dir = _make_store(tmp_path)
        (sounds_dir / "old.wav").write_bytes(b"x")
        store.add("horn", sounds_dir / "old.wav")

        with pytest.raises(UploadRejected) as plain:
            self._check(store, sounds_dir, markdown=False)
        with pytest.raises(UploadRejected) as discord_md:
            self._check(store, sounds_dir, markdown=True)

        assert "**" not in str(plain.value) and "`" not in str(plain.value)
        assert "**horn**" in str(discord_md.value)

    def test_filename_owned_by_another_entry_conflicts(self, tmp_path):
        from soundbot.ingest import UploadRejected

        store, sounds_dir = _make_store(tmp_path)
        (sounds_dir / "horn.wav").write_bytes(b"theirs")
        store.add("other", sounds_dir / "horn.wav")

        with pytest.raises(UploadRejected) as exc_info:
            self._check(store, sounds_dir, name="mine")
        assert exc_info.value.kind == "conflict"
        assert "other" in str(exc_info.value)

    def test_unregistered_file_on_disk_conflicts_and_survives(self, tmp_path):
        """A file in sounds/ that isn't in the library (e.g. scan_folder
        skipped it for an invalid name) used to be overwritten by the
        upload — and deleted outright if processing then failed."""
        from soundbot.ingest import UploadRejected

        store, sounds_dir = _make_store(tmp_path)
        stray = sounds_dir / "horn.wav"
        stray.write_bytes(b"someone's file")

        with pytest.raises(UploadRejected) as exc_info:
            self._check(store, sounds_dir)
        assert exc_info.value.kind == "conflict"
        assert stray.read_bytes() == b"someone's file"


class TestReserveUploadPath:
    def test_creates_the_file(self, tmp_path):
        from soundbot.ingest import reserve_upload_path

        dest = tmp_path / "horn.wav"
        reserve_upload_path(dest)

        assert dest.exists()

    def test_second_reservation_of_same_path_conflicts(self, tmp_path):
        """Two concurrent uploads with one filename both passed the
        pre-checks and wrote the same file. Exclusive create makes the
        reservation atomic: exactly one of them wins."""
        from soundbot.ingest import UploadRejected, reserve_upload_path

        dest = tmp_path / "horn.wav"
        reserve_upload_path(dest)
        dest.write_bytes(b"winner")

        with pytest.raises(UploadRejected) as exc_info:
            reserve_upload_path(dest)
        assert exc_info.value.kind == "conflict"
        assert dest.read_bytes() == b"winner"


class TestPipelineMessagesArePlainText:
    def test_extract_destination_conflict_has_no_markdown(self, tmp_path, monkeypatch):
        store, sounds_dir = _make_store(tmp_path)
        (sounds_dir / "clip.mp3").write_bytes(b"x")
        store.add("other", sounds_dir / "clip.mp3")
        (sounds_dir / "clip.mp3").unlink()  # dangling entry: owner check fires
        dest = sounds_dir / "clip.mp4"
        dest.write_bytes(b"video")
        monkeypatch.setattr("soundbot.ingest.has_video_stream", lambda p: True)

        with pytest.raises(ValueError) as exc_info:
            process_upload(
                dest, store=store, pcm_cache=PCMCache(), name="clip",
                category=None, tags=[], uploaded_by="t", max_duration=6.4,
                target_lufs=-16.0,
            )
        message = str(exc_info.value)
        assert "**" not in message and "`" not in message
        assert "other" in message
