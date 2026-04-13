import subprocess
from unittest.mock import patch

import discord
import pytest

from soundbot.mixer import FRAME_SIZE
from soundbot.pcm_cache import CachedPCMSource, PCMCache, decode_to_pcm


class TestDecodeToPcm:
    def test_returns_ffmpeg_stdout(self):
        fake_pcm = b"\x01\x02\x03\x04"
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_pcm
            out = decode_to_pcm("sounds/hello.ogg")
        assert out == fake_pcm

    def test_invokes_ffmpeg_with_48k_stereo_s16le(self):
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.return_value.stdout = b""
            decode_to_pcm("sounds/hello.ogg")
        args, _ = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "ffmpeg"
        assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "s16le"
        assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "48000"
        assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "2"
        assert cmd[-1] == "-"

    def test_called_process_error_raises_value_error(self):
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "ffmpeg")
            with pytest.raises(ValueError, match="Failed to decode"):
                decode_to_pcm("missing.ogg")

    def test_called_process_error_preserves_ffmpeg_stderr(self):
        """The opaque 'Failed to decode' error was the #1 review nit —
        we should surface ffmpeg's stderr so logs name the real cause."""
        err = subprocess.CalledProcessError(
            1, "ffmpeg", stderr=b"unsupported codec: foo"
        )
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.side_effect = err
            with pytest.raises(ValueError, match="unsupported codec: foo"):
                decode_to_pcm("bad.ogg")

    def test_called_process_error_with_no_stderr_is_safe(self):
        err = subprocess.CalledProcessError(1, "ffmpeg", stderr=None)
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.side_effect = err
            with pytest.raises(ValueError, match="Failed to decode"):
                decode_to_pcm("bad.ogg")

    def test_timeout_raises_value_error(self):
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("ffmpeg", 30)
            with pytest.raises(ValueError, match="Failed to decode"):
                decode_to_pcm("hang.ogg")

    def test_missing_ffmpeg_raises_value_error(self):
        with patch("soundbot.pcm_cache.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(ValueError, match="Failed to decode"):
                decode_to_pcm("anything.ogg")


class TestPCMCache:
    def test_miss_invokes_decoder(self):
        calls = []

        def fake_decode(path):
            calls.append(str(path))
            return b"decoded"

        cache = PCMCache(decoder=fake_decode)
        assert cache.get("sounds/a.ogg") == b"decoded"
        assert calls == ["sounds/a.ogg"]

    def test_hit_does_not_reinvoke_decoder(self):
        calls = []

        def fake_decode(path):
            calls.append(str(path))
            return b"decoded"

        cache = PCMCache(decoder=fake_decode)
        cache.get("sounds/a.ogg")
        cache.get("sounds/a.ogg")
        cache.get("sounds/a.ogg")
        assert len(calls) == 1

    def test_different_paths_cached_separately(self):
        def fake_decode(path):
            return f"bytes-for-{path}".encode()

        cache = PCMCache(decoder=fake_decode)
        assert cache.get("a.ogg") == b"bytes-for-a.ogg"
        assert cache.get("b.ogg") == b"bytes-for-b.ogg"

    def test_invalidate_forces_redecode(self):
        calls = []

        def fake_decode(path):
            calls.append(str(path))
            return b"fresh"

        cache = PCMCache(decoder=fake_decode)
        cache.get("a.ogg")
        cache.invalidate("a.ogg")
        cache.get("a.ogg")
        assert len(calls) == 2

    def test_invalidate_unknown_path_is_noop(self):
        cache = PCMCache(decoder=lambda p: b"")
        cache.invalidate("never-seen.ogg")  # must not raise

    def test_clear_empties_cache(self):
        calls = []

        def fake_decode(path):
            calls.append(str(path))
            return b"x"

        cache = PCMCache(decoder=fake_decode)
        cache.get("a.ogg")
        cache.get("b.ogg")
        cache.clear()
        cache.get("a.ogg")
        assert len(calls) == 3

    def test_decoder_errors_propagate(self):
        def fake_decode(path):
            raise ValueError("decode boom")

        cache = PCMCache(decoder=fake_decode)
        with pytest.raises(ValueError, match="decode boom"):
            cache.get("a.ogg")

    def test_decoder_error_does_not_poison_cache(self):
        # First call fails, second succeeds. A failed decode must not
        # cache an empty entry or any sentinel.
        state = {"first": True}

        def fake_decode(path):
            if state["first"]:
                state["first"] = False
                raise ValueError("transient")
            return b"good"

        cache = PCMCache(decoder=fake_decode)
        with pytest.raises(ValueError):
            cache.get("a.ogg")
        assert cache.get("a.ogg") == b"good"

    def test_contains_reflects_cache_state(self):
        cache = PCMCache(decoder=lambda p: b"x")
        assert "a.ogg" not in cache
        cache.get("a.ogg")
        assert "a.ogg" in cache
        cache.invalidate("a.ogg")
        assert "a.ogg" not in cache


class TestCachedPCMSource:
    def test_is_audio_source(self):
        source = CachedPCMSource(b"")
        assert isinstance(source, discord.AudioSource)

    def test_reads_full_frames_in_order(self):
        data = b"\xAA" * (FRAME_SIZE * 3)
        source = CachedPCMSource(data)

        frame1 = source.read()
        frame2 = source.read()
        frame3 = source.read()
        assert len(frame1) == FRAME_SIZE
        assert len(frame2) == FRAME_SIZE
        assert len(frame3) == FRAME_SIZE
        assert frame1 + frame2 + frame3 == data

    def test_exhausted_source_returns_empty_bytes(self):
        source = CachedPCMSource(b"\xAA" * FRAME_SIZE)
        source.read()
        assert source.read() == b""

    def test_empty_data_returns_empty_immediately(self):
        source = CachedPCMSource(b"")
        assert source.read() == b""

    def test_partial_final_frame_is_returned_raw(self):
        # Mixer pads short frames with zeros; we should return the
        # partial tail rather than dropping it.
        partial_size = FRAME_SIZE // 3
        data = b"\xAA" * (FRAME_SIZE + partial_size)
        source = CachedPCMSource(data)

        full = source.read()
        tail = source.read()
        assert len(full) == FRAME_SIZE
        assert len(tail) == partial_size
        assert source.read() == b""

    def test_cleanup_is_safe(self):
        source = CachedPCMSource(b"\xAA" * FRAME_SIZE)
        source.cleanup()  # must not raise
        # cleanup is a no-op; read still works the same way
        assert len(source.read()) == FRAME_SIZE
