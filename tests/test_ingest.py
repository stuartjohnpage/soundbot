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
from tests.helpers import make_mp4, make_wav, skip_no_ffmpeg

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
