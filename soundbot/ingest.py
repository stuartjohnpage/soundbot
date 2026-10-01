"""Shared upload-ingest pipeline.

Both halves of an upload, shared by the Discord commands (/addsound,
/importsounds) and the web panel's upload route so they run the exact
same code instead of diverging copies:

- pre-save: precheck_upload (validate input, refuse collisions) and
  reserve_upload_path (atomically claim the destination file);
- post-save: process_upload (validate audio, trim, loudness-normalize,
  invalidate stale cached PCM, register in the store).

Messages raised from here are plain text naming files by basename: they
reach both Discord replies and web API error details, so no Discord
markdown and no server paths. (duplicate_sound_message is the one
exception, with an explicit markdown switch.)

Everything here is blocking (ffmpeg/ffprobe subprocesses) — callers on
the bot's event loop must wrap calls in `asyncio.to_thread`.
"""
import logging
from pathlib import Path
from typing import Literal

from .audio import (
    extract_audio,
    get_duration,
    has_video_stream,
    normalize_loudness,
    trim_audio,
)
from .pcm_cache import PCMCache
from .store import SoundStore, parse_tags

logger = logging.getLogger("soundbot")


RejectionKind = Literal["invalid", "duplicate", "conflict"]


class UploadRejected(ValueError):
    """An upload refused because its input or destination is unusable.

    Mostly raised before any bytes are written (precheck_upload,
    reserve_upload_path), but also from inside process_upload when a
    video's extracted-audio destination is taken. `kind` lets each front
    end pick its own response: "invalid" (bad input, HTTP 400),
    "duplicate" (sound name taken, 409) or "conflict" (destination file
    already in use, 409).
    """

    def __init__(self, message: str, kind: RejectionKind) -> None:
        super().__init__(message)
        self.kind: RejectionKind = kind


def duplicate_sound_message(name: str, entry: dict, *, markdown: bool = True) -> str:
    """Explain a name collision in terms of where the existing sound is visible.

    Names are unique across the whole library, but boards are usually
    tag-filtered, so "already exists" alone reads as a lie when the
    existing sound carries no tag for (or a different tag than) the guild
    the uploader is looking at. Spell out the tags so the user can find it.
    `markdown=False` gives the plain-text form for the web panel.
    """
    # Direct subscript: SoundStore.load() guarantees the tags key exists.
    tags = entry["tags"]
    if markdown:
        if tags:
            tag_list = ", ".join(f"`{t}`" for t in sorted(tags))
            return (
                f"A sound named **{name.lower()}** already exists, tagged {tag_list}. "
                f"It only shows on boards filtered by those tags — run `/board` with "
                f"no filter to see it, or pick another name."
            )
        return (
            f"A sound named **{name.lower()}** already exists but has **no tags**, "
            f"so it never appears on tag-filtered boards. Run `/board` with no "
            f"filter to see it, `/tag add` to tag it for this server, or pick "
            f"another name."
        )
    if tags:
        return (
            f"Sound '{name.lower()}' already exists, tagged: "
            f"{', '.join(sorted(tags))}. Clear the tag filter to see it, "
            f"or pick another name."
        )
    return (
        f"Sound '{name.lower()}' already exists but has no tags, so it "
        f"never appears on tag-filtered views. Clear the tag filter to "
        f"see it, or pick another name."
    )


def precheck_upload(
    store: SoundStore,
    sounds_dir: Path,
    *,
    name: str,
    tags: str | None,
    filename: str | None,
    markdown: bool,
) -> tuple[Path, list[str]]:
    """Validate an upload before any bytes hit disk.

    Returns (dest, tag_list) or raises UploadRejected. Ordered cheapest-
    first so bad input fails before any file I/O. Passing these checks
    doesn't claim `dest` — call reserve_upload_path for that, since
    another upload can race in between. `markdown` only affects the
    duplicate-name message (the one with Discord-specific advice).
    """
    try:
        SoundStore.validate_name(name)
        tag_list = parse_tags(tags)
    except ValueError as exc:
        raise UploadRejected(str(exc), "invalid") from exc
    existing = store.get(name)
    if existing is not None:
        raise UploadRejected(
            duplicate_sound_message(name, existing, markdown=markdown),
            "duplicate",
        )
    # Path(...).name strips directory parts, defusing path traversal.
    safe_name = Path(filename or "").name
    if not safe_name:
        raise UploadRejected("Missing filename.", "invalid")
    dest = sounds_dir / safe_name
    if not dest.resolve().is_relative_to(sounds_dir.resolve()):
        raise UploadRejected("Invalid filename.", "invalid")
    # Never write to a path another entry owns: the pipeline's error-path
    # unlink would delete that entry's file.
    owner = store.find_by_path(dest)
    if owner is not None:
        raise UploadRejected(
            f"The file name '{dest.name}' is already used by sound "
            f"'{owner}'. Remove that sound first or rename your file.",
            "conflict",
        )
    # Nor to a file nobody owns: overwriting a stray file in sounds/
    # (say one scan_folder skipped) destroys it, and a failed upload then
    # deletes it outright. reserve_upload_path would refuse it too; this
    # earlier check exists for the clearer "not in the library" message.
    if dest.exists():
        raise UploadRejected(
            f"A file named '{dest.name}' is already in the sounds folder "
            f"(but not in the library). Rename your file and try again.",
            "conflict",
        )
    return dest, tag_list


def reserve_upload_path(dest: Path) -> None:
    """Atomically claim `dest` by creating it empty, or raise UploadRejected.

    precheck_upload's checks and the later write are separate steps, so
    two uploads with the same filename could both pass and then write the
    same file. Exclusive create ("xb") lets exactly one claim it. The
    winner then owns `dest`: it overwrites the placeholder with its bytes
    and must unlink it on any failure.
    """
    try:
        with dest.open("xb"):
            pass
    except FileExistsError:
        # Can't tell a concurrent upload from a stray file here, so the
        # message has to fit both.
        raise UploadRejected(
            f"A file named '{dest.name}' already exists in the sounds "
            f"folder. Rename your file and try again.",
            "conflict",
        ) from None


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
) -> tuple[Path, float | None, float | None]:
    """Validate, trim, normalize, and register a file already saved at `dest`.

    Returns (final_path, gain_db, trimmed_from_seconds) — gain_db is None
    when no attenuation was applied; trimmed_from_seconds is the original
    duration when the upload exceeded max_duration and was auto-trimmed
    to the cap (issue #20), else None.
    Raises ValueError with a user-facing message on failure (an
    UploadRejected if the extracted-audio destination is taken). On any
    failure, the upload's file (the video, or its extracted audio once
    swapped in) is deleted so nothing is left behind, unless a store
    entry already owns it. Callers must own `dest`, i.e. have claimed it
    with reserve_upload_path, before saving bytes there; otherwise the
    cleanup here could delete someone else's file.
    Does NOT call store.save(); the caller decides when to persist.
    """
    try:
        if has_video_stream(dest):
            audio_dest = dest.with_suffix(".mp3")
            # Same no-clobber guard as precheck_upload, but for the
            # extracted audio destination. The owner check covers a store
            # entry whose file was deleted off disk (so it isn't on disk
            # for the reservation below to collide with).
            audio_owner = store.find_by_path(audio_dest)
            if audio_owner is not None:
                raise UploadRejected(
                    f"Can't extract the audio: '{audio_dest.name}' is "
                    f"already used by sound '{audio_owner}'. Remove that "
                    f"sound first.",
                    "conflict",
                )
            # Claim it like the upload itself, so neither a stray file nor
            # a concurrent upload's extraction gets overwritten.
            reserve_upload_path(audio_dest)
            try:
                extract_audio(dest, audio_dest)
            except BaseException:
                audio_dest.unlink(missing_ok=True)
                raise
            dest.unlink(missing_ok=True)
            dest = audio_dest
        # get_duration doubles as the is-this-readable-audio check that
        # validate_sound used to provide. Over-length uploads are trimmed
        # to the cap instead of rejected (issue #20); a trim failure still
        # rejects, same as the old over-length error path.
        duration = get_duration(dest)
        trimmed_from: float | None = None
        if duration > max_duration:
            trim_audio(dest, max_duration)
            trimmed_from = duration
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
    except BaseException:
        # Any failure, not just ValueError: an orphan left by an
        # unexpected error would block its filename (precheck_upload
        # refuses files already on disk) and register as a broken sound on
        # the next scan_folder. `dest` is whichever file this upload owns
        # right now. The ownership check keeps a file that store.add
        # already registered from being pulled out from under its entry.
        if store.find_by_path(dest) is None:
            dest.unlink(missing_ok=True)
        raise
    return dest, gain, trimmed_from
