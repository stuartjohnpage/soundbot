import struct

from soundbot.mixer import MixerSource


# discord.py reads 20ms frames: 48000 Hz * 2 channels * 2 bytes * 0.02s = 3840 bytes
FRAME_SIZE = 3840


class FakeSource:
    """A fake AudioSource that yields a fixed number of frames with a known sample value."""

    def __init__(self, sample_value: int, num_frames: int = 1):
        self._sample_value = sample_value
        self._frames_left = num_frames
        self.cleaned_up = False

    def read(self) -> bytes:
        if self._frames_left <= 0:
            return b""
        self._frames_left -= 1
        num_samples = FRAME_SIZE // 2  # 16-bit = 2 bytes per sample
        return struct.pack(f"<{num_samples}h", *([self._sample_value] * num_samples))

    def cleanup(self):
        self.cleaned_up = True


class TestMixerSingleSource:
    def test_single_source_plays_through(self):
        mixer = MixerSource()
        source = FakeSource(sample_value=1000, num_frames=2)
        mixer.add(source)

        frame1 = mixer.read()
        assert len(frame1) == FRAME_SIZE
        # Verify samples are the expected value
        samples = struct.unpack(f"<{FRAME_SIZE // 2}h", frame1)
        assert all(s == 1000 for s in samples)

        frame2 = mixer.read()
        assert len(frame2) == FRAME_SIZE

        # Source exhausted - should return silence (not empty bytes)
        frame3 = mixer.read()
        assert len(frame3) == FRAME_SIZE
        assert frame3 == b"\x00" * FRAME_SIZE


class TestMixerTwoSources:
    def test_two_sources_are_summed(self):
        mixer = MixerSource()
        mixer.add(FakeSource(sample_value=1000, num_frames=1))
        mixer.add(FakeSource(sample_value=2000, num_frames=1))

        frame = mixer.read()
        samples = struct.unpack(f"<{FRAME_SIZE // 2}h", frame)
        assert all(s == 3000 for s in samples)

    def test_clipping_at_int16_max(self):
        mixer = MixerSource()
        mixer.add(FakeSource(sample_value=30000, num_frames=1))
        mixer.add(FakeSource(sample_value=30000, num_frames=1))

        frame = mixer.read()
        samples = struct.unpack(f"<{FRAME_SIZE // 2}h", frame)
        # 30000 + 30000 = 60000, should clip to 32767
        assert all(s == 32767 for s in samples)


class TestMixerSilenceAndStop:
    def test_returns_silence_when_no_sources_active(self):
        """Mixer should return silence (not empty bytes) when no sources are active."""
        mixer = MixerSource()
        frame = mixer.read()
        assert len(frame) == FRAME_SIZE
        assert frame == b"\x00" * FRAME_SIZE

    def test_returns_silence_after_sources_exhausted(self):
        """After all sources finish, mixer returns silence (stays alive for more sounds)."""
        mixer = MixerSource()
        mixer.add(FakeSource(sample_value=1000, num_frames=1))
        mixer.read()  # consume the one frame
        frame = mixer.read()  # no active sources now
        assert len(frame) == FRAME_SIZE
        assert frame == b"\x00" * FRAME_SIZE

    def test_returns_empty_when_stopped(self):
        """Stopped mixer returns empty bytes to signal end-of-stream."""
        mixer = MixerSource()
        mixer.stop()
        frame = mixer.read()
        assert frame == b""

    def test_reset_clears_stopped_state(self):
        """reset() allows the mixer to be reused after being stopped."""
        mixer = MixerSource()
        mixer.stop()
        assert mixer.read() == b""
        mixer.reset()
        frame = mixer.read()
        assert len(frame) == FRAME_SIZE
        assert frame == b"\x00" * FRAME_SIZE

    def test_is_audio_source(self):
        """MixerSource should be a discord.AudioSource subclass."""
        import discord
        mixer = MixerSource()
        assert isinstance(mixer, discord.AudioSource)


class TestMixerShortFrames:
    def test_short_frame_is_padded(self):
        """If a source returns fewer bytes than FRAME_SIZE, pad with zeros."""
        mixer = MixerSource()

        class ShortSource:
            def read(self):
                # Return a frame that's only 100 bytes (too short)
                return b"\x01" * 100

        mixer.add(ShortSource())
        frame = mixer.read()
        assert len(frame) == FRAME_SIZE


class TestMixerCleanup:
    def test_finished_sources_are_cleaned_up(self):
        mixer = MixerSource()
        short = FakeSource(sample_value=100, num_frames=1)
        long = FakeSource(sample_value=200, num_frames=3)
        mixer.add(short)
        mixer.add(long)

        # Frame 1: both active
        frame1 = mixer.read()
        samples1 = struct.unpack(f"<{FRAME_SIZE // 2}h", frame1)
        assert all(s == 300 for s in samples1)
        assert not short.cleaned_up

        # Frame 2: short exhausted, long still playing
        frame2 = mixer.read()
        samples2 = struct.unpack(f"<{FRAME_SIZE // 2}h", frame2)
        assert all(s == 200 for s in samples2)
        assert short.cleaned_up  # cleaned up after returning empty

    def test_cleanup_stops_all_sources(self):
        mixer = MixerSource()
        s1 = FakeSource(sample_value=100, num_frames=10)
        s2 = FakeSource(sample_value=200, num_frames=10)
        mixer.add(s1)
        mixer.add(s2)

        mixer.cleanup()
        assert s1.cleaned_up
        assert s2.cleaned_up
        # Cleanup stops the mixer, so it returns empty bytes
        assert mixer.read() == b""
