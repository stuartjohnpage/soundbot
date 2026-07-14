"""Shared upload-ingest pipeline.

The post-save half of what /addsound does: validate, loudness-normalize,
invalidate stale cached PCM, and register the sound in the store. Lives
in its own module so the Discord commands (/addsound, /importsounds) and
the web panel's upload route run the exact same code path instead of
diverging copies.

Everything here is blocking (ffmpeg/ffprobe subprocesses) — callers on
the bot's event loop must wrap calls in `asyncio.to_thread`.
"""
import logging
from pathlib import Path

from .audio import (
    extract_audio,
    has_video_stream,
    normalize_loudness,
    validate_sound,
)
from .pcm_cache import PCMCache
from .store import SoundStore

logger = logging.getLogger("soundbot")


def normalize_upload(dest: Path, target_lufs: float) -> float | None:
    """Best-effort loudness normalization for a just-saved upload.

    Returns the gain applied in dB (or None if the file was already at or
    below target). Never raises: a sound that can't be normalized is still
    a playable sound, so measurement/encode failures degrade to keeping
    the original file rather than refusing the upload.
    """
    try:
        return normalize_loudness(dest, target_lufs)
    except ValueError:
        logger.warning(
            "loudness normalization failed for %s; keeping original", dest,
            exc_info=True,
        )
        return None


def process_upload(
    dest: Path,
    *,
    store: SoundStore,
    pcm_cache: PCMCache,
    name: str,
    category: str | None,
    tags: list[str],
    uploaded_by: str | None,
    max_duration: float,
    target_lufs: float,
) -> tuple[Path, float | None]:
    """Validate, normalize, and register a file already saved at `dest`.

    Returns (final_path, gain_db) — gain_db is None when no attenuation
    was applied. Raises ValueError with a user-facing message on failure;
    the file at `dest` is deleted so a rejected upload leaves nothing
    behind. Callers must ensure no *other* store entry owns `dest`
    before saving bytes there (store.find_by_path) — otherwise the
    error-path unlink here would delete that entry's file.
    Does NOT call store.save(); the caller decides when to persist.
    """
    try:
        if has_video_stream(dest):
            audio_dest = dest.with_suffix(".mp3")
            if audio_dest.exists():
                raise ValueError(
                    f"A file named `{audio_dest.name}` already exists."
                )
            # Same no-clobber guard as the caller's pre-save check, but
            # for the extracted audio destination. Covers the case where
            # a store entry references a file path that was manually
            # deleted off disk — the .exists() check above misses it.
            audio_owner = store.find_by_path(audio_dest)
            if audio_owner is not None:
                raise ValueError(
                    f"Cannot upload: `{audio_dest.name}` is already in use "
                    f"by sound **{audio_owner}**. Remove that sound first."
                )
            extract_audio(dest, audio_dest)
            dest.unlink(missing_ok=True)
            dest = audio_dest
        validate_sound(dest, max_duration)
        # Normalize before the cache invalidation below so no consumer
        # can cache the pre-normalization bytes.
        gain = normalize_upload(dest, target_lufs)
        # Drop any stale cached PCM for this path before the new entry is
        # added. Two distinct sound names uploaded with the same filename
        # land at the same dest on disk, and a previous play may have
        # populated the cache with the old file's bytes.
        pcm_cache.invalidate(dest)
        store.add(name, dest, category=category, uploaded_by=uploaded_by)
        for tag in tags:
            store.add_tag(name, tag)
    except ValueError:
        dest.unlink(missing_ok=True)
        raise
    return dest, gain
