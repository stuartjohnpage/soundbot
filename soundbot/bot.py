import asyncio
import logging
import random
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config
from .ingest import process_upload
from .migration import run_migration_if_needed
from .mixer import MixerSource
from .pagination import paginate
from .pcm_cache import CachedPCMSource, PCMCache
from .store import SoundStore, parse_tags
from .web import build_web_server, maybe_create_web_app, serve_web_app

logger = logging.getLogger("soundbot")

SOUNDS_PER_BOARD = 25  # Discord's hard cap: 5 action rows * 5 buttons per row
DISCORD_IMPORT_CATEGORY = "discord-import"
_MAX_SUMMARY_LENGTH = 1900  # Leave headroom under Discord's 2000-char limit


def classify_import_sound(
    existing_entry: dict | None,
    dest_exists: bool,
    guild_tag: str | None,
) -> str:
    """Classify an incoming /importsounds soundboard sound into a bucket.

    Pure function so the bucketing decisions can be unit-tested without
    standing up a real discord.Interaction. Returns one of:

      - "needs_download": no local entry, no file collision -> proceed
      - "tagged_existing": local entry exists, lacked the guild tag
        (the caller will add it)
      - "already_tagged": local entry exists, no new tag to add
        (either it already had the guild_tag, or guild_tag is None)
      - "file_conflict": no local entry, but a file with the destination
        name already exists on disk in a different format

    ``guild_tag``, if non-None, must already be sanitized (lowercase, the
    [a-z0-9-] character set produced by SoundStore.sanitize_tag). This
    function does no normalization — it compares against entry["tags"]
    with == membership, and those are always stored canonicalized.

    Pre-fix bug: tagged_existing, already_tagged, and file_conflict all
    collapsed into a single "skipped (already tagged)" bucket which was
    factually wrong for the latter two cases.
    """
    if existing_entry is not None:
        # Direct subscript: SoundStore.load() guarantees the tags key exists
        # on every entry, so falling back with .get() would be lying about
        # the invariant.
        if guild_tag and guild_tag not in existing_entry["tags"]:
            return "tagged_existing"
        return "already_tagged"
    if dest_exists:
        return "file_conflict"
    return "needs_download"


def duplicate_sound_message(name: str, entry: dict) -> str:
    """Explain a name collision in terms of where the existing sound is visible.

    Names are unique across the whole library, but boards are usually
    tag-filtered — so "already exists" alone reads as a lie when the
    existing sound carries no tag for (or a different tag than) the guild
    the uploader is looking at. Spell out the tags so the user can find it.
    Pure function so the wording is unit-testable without an Interaction.
    """
    # Direct subscript: SoundStore.load() guarantees the tags key exists
    # on every entry (same invariant classify_import_sound relies on).
    tags = entry["tags"]
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


# Keycap emoji (1️⃣, #️⃣, …) start with a plain ASCII char before the
# combining enclosing keycap — the one legitimate ASCII in an emoji key.
_KEYCAP_BASES = set("0123456789#*")
_MAX_EMOJI_KEY_LENGTH = 16  # ZWJ family sequences run ~11 code points


def parse_emoji_key(raw: str) -> str:
    """Canonicalize user-typed emoji text into a binding key, or raise ValueError.

    The key must equal ``str(payload.emoji)`` at reaction time, so custom
    emoji round-trip through PartialEmoji (yielding ``<:name:id>`` /
    ``<a:name:id>``) and unicode emoji are stored as the bare character(s).

    Unicode validation is a shape heuristic, not a Unicode-database check:
    every char must sit above the plain-ASCII range words are written in
    (letters, digits, punctuation), except keycap bases. That rejects the
    realistic failure mode — someone typing a word like "airhorn" into the
    emoji field — while accepting flags, skin tones, ZWJ sequences, and
    keycaps. An exotic non-emoji symbol slipping through is harmless: the
    binding just never matches a real reaction.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("Emoji cannot be empty.")
    partial = discord.PartialEmoji.from_str(candidate)
    if partial.id is not None:
        return str(partial)
    if len(candidate) > _MAX_EMOJI_KEY_LENGTH:
        raise ValueError(f"'{raw}' doesn't look like a single emoji.")
    for ch in candidate:
        if ord(ch) < 0x2000 and ch not in _KEYCAP_BASES:
            raise ValueError(
                f"'{raw}' doesn't look like an emoji. Use a standard emoji "
                "or a custom emoji from this server."
            )
    return candidate


def _admin_check() -> app_commands.check:
    """Check that the invoking user has the configured admin role."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if not any(r.name == config.ADMIN_ROLE for r in interaction.user.roles):
            raise app_commands.MissingRole(config.ADMIN_ROLE)
        return True

    return app_commands.check(predicate)


class Soundboard(commands.Cog):
    # Class-level Group is the discord.py pattern for Cog-scoped grouped
    # commands: methods decorated with @tag_group.command get registered as
    # /tag <subcommand> when the Cog is added. See discord.py docs:
    # https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.app_commands.Group
    tag_group = app_commands.Group(
        name="tag",
        description="Manage sound tags",
    )

    def __init__(self, bot: commands.Bot, store: SoundStore) -> None:
        self.bot = bot
        self.store = store
        self.mixer: MixerSource | None = None
        self.volume: float = config.DEFAULT_VOLUME / 100.0
        self.pcm_cache = PCMCache()

    async def cog_load(self) -> None:
        self._save_loop.start()

    async def cog_unload(self) -> None:
        self._save_loop.cancel()
        self.store.save()

    @tasks.loop(seconds=60)
    async def _save_loop(self) -> None:
        """Persist play counts periodically."""
        self.store.save()

    # -- Voice management --

    @app_commands.command(name="join", description="Bot joins your voice channel")
    @_admin_check()
    async def join(self, interaction: discord.Interaction) -> None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You must be in a voice channel.", ephemeral=True
            )
            return
        channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            vc = await channel.connect()
            self.mixer = MixerSource(volume=self.volume)
            vc.play(self.mixer)
        await interaction.response.send_message(f"Joined **{channel.name}**.")

    @app_commands.command(name="leave", description="Bot leaves the voice channel")
    @_admin_check()
    async def leave(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                "Not in a voice channel.", ephemeral=True
            )
            return
        if self.mixer:
            self.mixer.stop()
            self.mixer.cleanup()
            self.mixer = None
        await vc.disconnect()
        await interaction.response.send_message("Left the voice channel.")

    # -- Playback helpers --

    def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            raise ValueError("Bot is not in a voice channel. Use `/join` first.")
        return vc

    async def _start_playback(self, vc: discord.VoiceClient, name: str) -> str | None:
        """Decode `name` and feed it to the live mixer.

        Returns None on success, or a short user-facing failure reason.
        Shared core between interaction-driven playback (_play_sound turns
        the reason into an ephemeral reply) and reaction-triggered playback
        (which logs the reason and stays silent, per issue #9).
        """
        entry = self.store.get(name)
        if not entry:
            return f"Sound **{name}** not found."

        # First play for a file pays the ffmpeg decode cost; every subsequent
        # press is an in-memory slice. Done via to_thread so a cold miss
        # doesn't block the event loop.
        try:
            pcm_bytes = await asyncio.to_thread(self.pcm_cache.get, entry["file"])
        except ValueError as exc:
            logger.warning("decode failed for %s: %s", name, exc)
            return f"Failed to decode **{name}**."

        # The to_thread await above is a yield point: a concurrent /leave can
        # tear down the mixer and disconnect the voice client before we get
        # back here. Re-check before touching self.mixer — otherwise we'd
        # silently drop the sound and leak an orphan mixer.
        if self.mixer is None or not vc.is_connected():
            logger.info("voice torn down during decode, dropping sound=%s", name)
            return "Voice connection lost while loading sound."

        self.mixer.add(CachedPCMSource(pcm_bytes))
        self.store.increment_play_count(name)
        return None

    async def _play_sound(
        self, interaction: discord.Interaction, name: str, *, suppress_reply: bool = False
    ) -> None:
        try:
            vc = self._ensure_voice(interaction)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        error = await self._start_playback(vc, name)
        if error is not None:
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(error, ephemeral=True)
                except discord.HTTPException:
                    pass
            return

        logger.info(
            "play sound=%s user=%s guild=%s channel=%s",
            name,
            interaction.user,
            interaction.guild,
            getattr(interaction.user.voice, "channel", None),
        )
        if not suppress_reply:
            await interaction.response.send_message(
                f"Playing **{name}**", ephemeral=True
            )

    # -- Reaction-triggered playback (issue #9) --

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        """Play the bound sound when a bound emoji reaction is added.

        Every early-out is silent by design (issue #9): reactions carry no
        interaction to reply to, and a binding miss is the overwhelmingly
        common case — most reactions have nothing to do with the bot.
        """
        if payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        name = self.store.get_emoji_binding(payload.guild_id, str(payload.emoji))
        if name is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        vc = guild.voice_client if guild is not None else None
        if vc is None or not vc.is_connected() or self.mixer is None:
            return  # bot not in voice -> ignore silently
        error = await self._start_playback(vc, name)
        if error is not None:
            logger.info(
                "reaction play failed sound=%s guild_id=%s: %s",
                name, payload.guild_id, error,
            )
            return
        logger.info(
            "play sound=%s trigger=reaction emoji=%s user_id=%s guild_id=%s",
            name, payload.emoji, payload.user_id, payload.guild_id,
        )

    # -- Sound name autocomplete --

    async def _sound_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # If a tag was already typed in the same interaction, restrict to sounds
        # carrying that tag. Namespace returns None (not AttributeError) for
        # absent options, so commands without a `tag` field fall through cleanly.
        tag = getattr(interaction.namespace, "tag", None)
        if tag:
            tagged = {n for n, _ in self.store.list_sounds(tag=tag)}
            matches = [(n, e) for n, e in self.store.search(current) if n in tagged]
        else:
            matches = self.store.search(current)
        return [
            app_commands.Choice(name=n, value=n) for n, _ in matches[:25]
        ]

    # -- Tag autocomplete --

    async def _global_tag_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete from the global set of tags currently in use."""
        current_lower = current.lower()
        matches = [t for t, _ in self.store.global_tags() if current_lower in t]
        return [app_commands.Choice(name=t, value=t) for t in matches[:25]]

    async def _sound_tag_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete tags actually present on the currently-typed sound.

        Reads the `sound` option from the same interaction's namespace.
        """
        sound_name = getattr(interaction.namespace, "sound", None)
        if not sound_name:
            return []
        try:
            tags = self.store.list_tags(sound_name)
        except KeyError:
            return []
        current_lower = current.lower()
        matches = [t for t in tags if current_lower in t]
        return [app_commands.Choice(name=t, value=t) for t in matches[:25]]

    # -- Commands --

    @app_commands.command(name="play", description="Play a sound")
    @app_commands.describe(name="Sound name", tag="Optional tag filter for autocomplete")
    @app_commands.autocomplete(name=_sound_autocomplete, tag=_global_tag_autocomplete)
    @_admin_check()
    async def play(
        self,
        interaction: discord.Interaction,
        name: str,
        tag: str | None = None,
    ) -> None:
        # `tag` is intentionally consumed only by _sound_autocomplete via
        # interaction.namespace.tag — it scopes the name autocomplete to
        # sounds carrying that tag. The function body never reads it; the
        # explicit `del` keeps a future maintainer from helpfully removing
        # the parameter and breaking the autocomplete coupling.
        del tag
        await self._play_sound(interaction, name)

    @app_commands.command(name="random", description="Play a random sound")
    @app_commands.describe(
        category="Optional category filter",
        tag="Optional tag filter",
    )
    @app_commands.autocomplete(tag=_global_tag_autocomplete)
    @_admin_check()
    async def random_sound(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        tag: str | None = None,
    ) -> None:
        sounds = self.store.list_sounds(category=category, tag=tag)
        if not sounds:
            await interaction.response.send_message(
                "No sounds found.", ephemeral=True
            )
            return
        name, _ = random.choice(sounds)
        await self._play_sound(interaction, name)

    @app_commands.command(name="volume", description="Set playback volume (0-100)")
    @app_commands.describe(level="Volume percentage (0-100)")
    @_admin_check()
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        if not 0 <= level <= 100:
            await interaction.response.send_message(
                "Volume must be between 0 and 100.", ephemeral=True
            )
            return
        self.volume = level / 100.0
        # Mixer holds its own copy so read() can apply volume without
        # reaching back into the cog on every frame.
        if self.mixer is not None:
            self.mixer.volume = self.volume
        await interaction.response.send_message(f"Volume set to **{level}%**.")

    # -- Board --

    @app_commands.command(name="board", description="Show sound button board")
    @app_commands.describe(tag="Optional tag filter")
    @app_commands.autocomplete(tag=_global_tag_autocomplete)
    @_admin_check()
    async def board(
        self, interaction: discord.Interaction, tag: str | None = None
    ) -> None:
        sounds = self.store.list_sounds(tag=tag)
        if not sounds:
            await interaction.response.send_message(
                "No sounds in the library.", ephemeral=True
            )
            return
        pages = paginate(sounds, per_page=SOUNDS_PER_BOARD)
        # Defer so we can send multiple followup messages, one per board chunk.
        await interaction.response.defer()
        for idx, page_sounds in enumerate(pages, start=1):
            view = BoardView(self, page_sounds)
            embed = discord.Embed(
                title="Soundboard" if len(pages) == 1 else f"Soundboard ({idx}/{len(pages)})",
            )
            await interaction.followup.send(embed=embed, view=view)

    # -- CRUD commands --

    def _find_existing_by_path(self, path: Path) -> str | None:
        """Return the name of any store entry whose file is `path`, or None.

        Thin delegate to SoundStore.find_by_path — the logic was hoisted
        into the store so the web panel's upload route can use the same
        no-clobber guard without reaching into the cog.
        """
        return self.store.find_by_path(path)

    @app_commands.command(name="addsound", description="Add a new sound")
    @app_commands.describe(
        name="Sound name",
        file="Audio file to upload",
        category="Optional category",
        tags="Optional comma-separated tags (e.g. meme,funny,dave)",
    )
    @_admin_check()
    async def addsound(
        self,
        interaction: discord.Interaction,
        name: str,
        file: discord.Attachment,
        category: str | None = None,
        tags: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # Validate name and tags before any file I/O so we fail cleanly
        # on bad input (otherwise an invalid name isn't caught until
        # store.add, after the download and the whole ffmpeg pipeline).
        try:
            SoundStore.validate_name(name)
            tag_list = parse_tags(tags)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        # Name-collision check before any file I/O. store.add would catch
        # this too, but by then the upload is already on disk — and its
        # bare "already exists" doesn't explain why the sound is invisible
        # on tag-filtered boards (the usual reason the user re-uploads it).
        existing = self.store.get(name)
        if existing is not None:
            await interaction.followup.send(
                duplicate_sound_message(name, existing), ephemeral=True
            )
            return
        # Auto-tag with the guild so the new sound shows up on this
        # server's tag-filtered board immediately. Same convention as
        # /importsounds; explicit user tags ride along unchanged.
        if interaction.guild is not None:
            try:
                guild_tag = SoundStore.sanitize_tag(interaction.guild.name)
                if guild_tag not in tag_list:
                    tag_list.append(guild_tag)
            except ValueError:
                logger.warning(
                    "guild name %r could not be sanitized into a tag; "
                    "sound will be added without the guild auto-tag",
                    interaction.guild.name,
                )
        # Sanitize filename to prevent path traversal
        safe_name = Path(file.filename).name
        dest = config.SOUNDS_DIR / safe_name
        if not dest.resolve().is_relative_to(config.SOUNDS_DIR.resolve()):
            await interaction.followup.send("Invalid filename.", ephemeral=True)
            return
        # Must come before file.save: we never want to write to a path that
        # another entry already owns. Catches both same-name re-uploads
        # (refuse, tell user to remove first) and different-name same-filename
        # collisions (would otherwise corrupt the existing entry).
        owner = self._find_existing_by_path(dest)
        if owner is not None:
            if owner == name.lower():
                msg = (
                    f"Sound **{owner}** already uses `{dest.name}`. "
                    "Remove it first if you want to replace it."
                )
            else:
                msg = (
                    f"Cannot upload: `{dest.name}` is already in use by sound "
                    f"**{owner}**. Remove that sound first or rename your upload."
                )
            await interaction.followup.send(msg, ephemeral=True)
            return
        await file.save(dest)
        try:
            # Shared ingest pipeline (video-extract, validate, normalize,
            # cache-invalidate, register) — same code path as the web
            # panel's upload route. to_thread keeps the ffmpeg subprocess
            # work off the event loop. On ValueError the pipeline has
            # already unlinked dest; safe because _find_existing_by_path
            # above guarantees no other entry references this path.
            # (Concurrent /addsound calls could race around the file.save
            # yield point — that's a pre-existing TOCTOU limitation.)
            dest, gain = await asyncio.to_thread(
                process_upload,
                dest,
                store=self.store,
                pcm_cache=self.pcm_cache,
                name=name,
                category=category,
                tags=tag_list,
                uploaded_by=str(interaction.user),
                max_duration=config.MAX_DURATION,
                target_lufs=config.TARGET_LUFS,
            )
            # Single save after the batch — add_tag mutates only in-memory state.
            self.store.save()
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        msg = f"Added sound **{name}**."
        if gain is not None:
            msg += f" Normalized {gain:+.1f} dB."
        if tag_list:
            msg += f" Tagged: {', '.join(f'`{t}`' for t in tag_list)}."
        await interaction.followup.send(msg)

    @app_commands.command(name="removesound", description="Remove a sound")
    @app_commands.describe(name="Sound name")
    @app_commands.autocomplete(name=_sound_autocomplete)
    @_admin_check()
    async def removesound(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        entry = self.store.get(name)
        try:
            self.store.remove(name)
            self.store.save()
        except KeyError:
            await interaction.response.send_message(
                f"Sound **{name}** not found.", ephemeral=True
            )
            return
        # Drop any cached PCM so a re-add under the same filename doesn't
        # serve stale bytes. `entry` was fetched before remove(), so we
        # still have the path even though the store entry is gone.
        if entry is not None:
            self.pcm_cache.invalidate(entry["file"])
        await interaction.response.send_message(f"Removed sound **{name}**.")

    @app_commands.command(name="renamesound", description="Rename a sound")
    @app_commands.describe(old="Current name", new="New name")
    @app_commands.autocomplete(old=_sound_autocomplete)
    @_admin_check()
    async def renamesound(
        self, interaction: discord.Interaction, old: str, new: str
    ) -> None:
        # No pcm_cache interaction needed: store.rename swaps the dict key
        # in metadata but leaves the file on disk at the same path, and the
        # cache is keyed by file path. The same bytes are still correct
        # under the new name.
        try:
            self.store.rename(old, new)
            self.store.save()
        except (KeyError, ValueError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Renamed **{old}** to **{new}**."
        )

    @app_commands.command(
        name="importsounds",
        description="Import sounds from Discord's built-in soundboard",
    )
    @_admin_check()
    async def importsounds(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        # Sanitize the guild name once — this is the auto-tag we apply to
        # every imported AND every pre-existing sound matched on re-import.
        try:
            guild_tag = SoundStore.sanitize_tag(guild.name)
        except ValueError:
            guild_tag = None
            logger.warning(
                "guild name %r could not be sanitized into a tag; importing without auto-tag",
                guild.name,
            )

        try:
            sounds = await guild.fetch_soundboard_sounds()
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Failed to fetch soundboard sounds: {exc}", ephemeral=True
            )
            return

        if not sounds:
            await interaction.followup.send(
                "No sounds found in Discord's soundboard.", ephemeral=True
            )
            return

        imported = []
        tagged_existing = []
        already_tagged = []
        file_conflict = []
        path_conflict = []
        failed = []
        for sound in sounds:
            try:
                key = SoundStore.sanitize_name(sound.name)
            except ValueError:
                key = f"sound_{sound.id}"
            existing_entry = self.store.get(key)
            dest = config.SOUNDS_DIR / f"{key}.ogg"
            bucket = classify_import_sound(
                existing_entry, dest.exists(), guild_tag
            )
            if bucket == "tagged_existing":
                self.store.add_tag(key, guild_tag)
                tagged_existing.append(key)
                continue
            if bucket == "already_tagged":
                already_tagged.append(key)
                continue
            if bucket == "file_conflict":
                file_conflict.append(key)
                continue
            # bucket == "needs_download". Even though `key` doesn't have a
            # store entry, the dest path could still be owned by an entry
            # under a different name (dangling pointer, manual JSON edit,
            # etc.). Don't silently overwrite — same guard addsound uses.
            other_owner = self._find_existing_by_path(dest)
            if other_owner is not None:
                path_conflict.append(f"{key} (owned by '{other_owner}')")
                continue
            try:
                await sound.save(dest)
                # Same ingest pipeline as /addsound — imported soundboard
                # sounds arrive at whatever level they were uploaded to
                # Discord at, so they get the same validation and
                # upload-time normalization.
                await asyncio.to_thread(
                    process_upload,
                    dest,
                    store=self.store,
                    pcm_cache=self.pcm_cache,
                    name=key,
                    category=DISCORD_IMPORT_CATEGORY,
                    tags=[guild_tag] if guild_tag else [],
                    uploaded_by=str(interaction.user),
                    max_duration=config.MAX_DURATION,
                    target_lufs=config.TARGET_LUFS,
                )
                imported.append(key)
            except (discord.HTTPException, ValueError, OSError) as exc:
                dest.unlink(missing_ok=True)
                failed.append(f"{key}: {exc}")
                logger.warning("Failed to import soundboard sound %s: %s", key, exc)

        if imported or tagged_existing:
            self.store.save()
        parts = [f"**Imported {len(imported)}** sound(s)."]
        if guild_tag:
            parts[0] += f" Auto-tag: `{guild_tag}`."
        if tagged_existing:
            names = ", ".join(tagged_existing)
            parts.append(
                f"Tagged existing {len(tagged_existing)}: {names}"
            )
        if already_tagged:
            names = ", ".join(already_tagged)
            parts.append(
                f"Already tagged {len(already_tagged)}: {names}"
            )
        if file_conflict:
            names = ", ".join(file_conflict)
            parts.append(
                f"File conflict {len(file_conflict)} (a different file with that name exists on disk): {names}"
            )
        if path_conflict:
            names = ", ".join(path_conflict)
            parts.append(
                f"Path conflict {len(path_conflict)} (another sound entry already owns that file): {names}"
            )
        if failed:
            parts.append(f"Failed {len(failed)}: {', '.join(failed)}")
        msg = "\n".join(parts)
        if len(msg) > _MAX_SUMMARY_LENGTH:
            msg = msg[:_MAX_SUMMARY_LENGTH] + "\n... (truncated)"
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="listsounds", description="List all sounds")
    @app_commands.describe(
        category="Optional category filter",
        page="Page number (default 1)",
    )
    @_admin_check()
    async def listsounds(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        page: int = 1,
    ) -> None:
        sounds = self.store.list_sounds(category=category)
        if not sounds:
            await interaction.response.send_message(
                "No sounds found.", ephemeral=True
            )
            return
        pages = paginate(sounds, per_page=20)
        # Clamp page to valid range
        page_idx = max(0, min(page - 1, len(pages) - 1))
        lines = []
        for name, entry in pages[page_idx]:
            cat = entry.get("category") or "\u2014"
            plays = entry.get("play_count", 0)
            lines.append(f"`{name}` | {cat} | {plays} plays")
        embed = discord.Embed(
            title="Sound Library",
            description="\n".join(lines),
        )
        embed.set_footer(text=f"Page {page_idx + 1} of {len(pages)}")
        await interaction.response.send_message(embed=embed)

    # -- /tag commands --

    @tag_group.command(name="add", description="Add a tag to a sound")
    @app_commands.describe(sound="Sound name", tag="Tag to add")
    @app_commands.autocomplete(sound=_sound_autocomplete, tag=_global_tag_autocomplete)
    @_admin_check()
    async def tag_add(
        self, interaction: discord.Interaction, sound: str, tag: str
    ) -> None:
        try:
            self.store.add_tag(sound, tag)
            self.store.save()
        except KeyError:
            await interaction.response.send_message(
                f"Sound **{sound}** not found.", ephemeral=True
            )
            return
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Tagged **{sound}** with `{tag}`.", ephemeral=True
        )

    @tag_group.command(name="remove", description="Remove a tag from a sound")
    @app_commands.describe(sound="Sound name", tag="Tag to remove")
    @app_commands.autocomplete(sound=_sound_autocomplete, tag=_sound_tag_autocomplete)
    @_admin_check()
    async def tag_remove(
        self, interaction: discord.Interaction, sound: str, tag: str
    ) -> None:
        try:
            self.store.remove_tag(sound, tag)
            self.store.save()
        except (KeyError, ValueError) as exc:
            # remove_tag raises KeyError when the sound doesn't exist and
            # ValueError when the tag isn't on the sound. Both render to
            # the user via str(exc); KeyError's repr quoting is avoided
            # because exc.args[0] is the user-facing message either way.
            msg = exc.args[0] if exc.args else "Not found."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Removed `{tag}` from **{sound}**.", ephemeral=True
        )

    @tag_group.command(
        name="list", description="List tags on a sound, or all tags globally"
    )
    @app_commands.describe(sound="Optional sound name; omit to list all tags")
    @app_commands.autocomplete(sound=_sound_autocomplete)
    @_admin_check()
    async def tag_list(
        self, interaction: discord.Interaction, sound: str | None = None
    ) -> None:
        if sound is not None:
            try:
                tags = self.store.list_tags(sound)
            except KeyError:
                await interaction.response.send_message(
                    f"Sound **{sound}** not found.", ephemeral=True
                )
                return
            if not tags:
                await interaction.response.send_message(
                    f"**{sound}** has no tags.", ephemeral=True
                )
                return
            tag_str = ", ".join(f"`{t}`" for t in tags)
            await interaction.response.send_message(
                f"Tags on **{sound}**: {tag_str}", ephemeral=True
            )
            return

        # Global listing
        all_tags = self.store.global_tags()
        if not all_tags:
            await interaction.response.send_message(
                "No tags in the library yet.", ephemeral=True
            )
            return
        lines = [f"`{t}` ({n})" for t, n in all_tags]
        embed = discord.Embed(
            title="Tags",
            description="\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- Emoji binding commands (issue #9) --

    async def _bound_emoji_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete /unbindemoji from this guild's current bindings.

        Choices display as "emoji → sound" but the submitted value is the
        bare emoji key, matching what unbind_emoji expects.
        """
        if interaction.guild is None:
            return []
        current_lower = current.lower()
        return [
            app_commands.Choice(name=f"{emoji} → {sound}", value=emoji)
            for emoji, sound in self.store.list_emoji_bindings(interaction.guild.id)
            if current_lower in sound or current_lower in emoji
        ][:25]

    @app_commands.command(
        name="bindemoji",
        description="Bind an emoji so reacting with it plays a sound",
    )
    @app_commands.describe(
        sound="Sound to play",
        emoji="Emoji to bind (standard or this server's custom emoji)",
    )
    @app_commands.autocomplete(sound=_sound_autocomplete)
    @_admin_check()
    async def bindemoji(
        self, interaction: discord.Interaction, sound: str, emoji: str
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        try:
            emoji_key = parse_emoji_key(emoji)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            previous = self.store.bind_emoji(interaction.guild.id, emoji_key, sound)
            self.store.save()
        except KeyError:
            await interaction.response.send_message(
                f"Sound **{sound}** not found.", ephemeral=True
            )
            return
        if previous is not None and previous != sound.lower():
            msg = f"Rebound {emoji_key} from **{previous}** to **{sound.lower()}**."
        else:
            msg = (
                f"Bound {emoji_key} to **{sound.lower()}**. "
                "Reacting with it on any message plays the sound."
            )
        await interaction.response.send_message(msg)

    @app_commands.command(
        name="unbindemoji", description="Remove an emoji-to-sound binding"
    )
    @app_commands.describe(emoji="Bound emoji to remove")
    @app_commands.autocomplete(emoji=_bound_emoji_autocomplete)
    @_admin_check()
    async def unbindemoji(
        self, interaction: discord.Interaction, emoji: str
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        try:
            emoji_key = parse_emoji_key(emoji)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            sound = self.store.unbind_emoji(interaction.guild.id, emoji_key)
            self.store.save()
        except KeyError as exc:
            # exc.args[0], not str(exc): KeyError.__str__ wraps the message
            # in an extra layer of quotes.
            await interaction.response.send_message(exc.args[0], ephemeral=True)
            return
        await interaction.response.send_message(
            f"Unbound {emoji_key} (was **{sound}**)."
        )

    @app_commands.command(
        name="listbindings",
        description="List this server's emoji-to-sound bindings",
    )
    @_admin_check()
    async def listbindings(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        bindings = self.store.list_emoji_bindings(interaction.guild.id)
        if not bindings:
            await interaction.response.send_message(
                "No emoji bindings yet. Use `/bindemoji` to create one.",
                ephemeral=True,
            )
            return
        lines = [f"{emoji} → **{sound}**" for emoji, sound in bindings]
        embed = discord.Embed(
            title="Emoji Bindings",
            description="\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BoardView(discord.ui.View):
    def __init__(self, cog: Soundboard, sounds) -> None:
        # timeout=None: buttons stay active until the bot restarts. Views aren't
        # persistent, so any existing boards go dead on restart — users re-run /board.
        super().__init__(timeout=None)
        self.cog = cog
        for name, _ in sounds:
            btn = discord.ui.Button(label=name, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(name)
            self.add_item(btn)

    def _make_callback(self, name: str):
        async def callback(interaction: discord.Interaction):
            await self.cog._play_sound(interaction, name, suppress_reply=True)
            if not interaction.response.is_done():
                await interaction.response.defer()

        return callback


async def sync_guild_commands(
    tree: app_commands.CommandTree, guild: discord.abc.Snowflake
) -> None:
    """Copy the global command set into one guild and push it.

    Guild-scoped syncs take effect immediately, unlike global syncs which can
    take up to an hour to propagate. This is what makes the bot's commands show
    up in a server the moment it is present there — at startup and the instant
    the bot is invited to a new server (see ``on_guild_join``).
    """
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


async def deploy_commands(
    tree: app_commands.CommandTree,
    http: "discord.http.HTTPClient",
    application_id: int,
    guilds: "list[discord.abc.Snowflake]",
) -> None:
    """Make commands available instantly across every connected guild.

    Each guild is synced individually (instant), then any leftover *global*
    registrations are deleted so commands don't appear twice. The global wipe
    goes through the HTTP layer rather than ``tree.clear_commands(guild=None)``
    on purpose: clearing the in-memory global tree would leave
    ``copy_global_to`` with nothing to copy for guilds joined later at runtime.
    This deletes Discord's global copies while keeping the in-memory command
    set intact so ``sync_guild_commands`` keeps working for future joins.
    """
    for guild in guilds:
        await sync_guild_commands(tree, guild)
    await http.bulk_upsert_global_commands(application_id, [])


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    store = SoundStore(
        metadata_path=config.METADATA_FILE,
        sounds_dir=config.SOUNDS_DIR,
    )

    async def setup_hook():
        config.SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        store.scan_folder()
        store.save()
        cog = Soundboard(bot, store)
        await bot.add_cog(cog)
        # Command sync happens in on_ready, not here: bot.guilds is empty until
        # the gateway delivers guild data after READY, and we sync per-guild.

        # Web admin panel — runs on this same event loop, sharing the
        # same SoundStore and PCMCache instances (sounds.json has no
        # cross-process locking, so a separate web process could corrupt
        # it). No WEB_TOKEN -> maybe_create_web_app returns None and the
        # panel simply does not exist.
        web_app = maybe_create_web_app(
            store,
            cog.pcm_cache,
            token=config.WEB_TOKEN,
            sounds_dir=config.SOUNDS_DIR,
            max_duration=config.MAX_DURATION,
            target_lufs=config.TARGET_LUFS,
        )
        if web_app is None:
            logger.info("WEB_TOKEN not set; web admin panel disabled")
        else:
            server = build_web_server(
                web_app, host=config.WEB_HOST, port=config.WEB_PORT
            )

            def log_web_exit(task: asyncio.Task) -> None:
                # Surface mid-serve crashes — otherwise the task
                # exception is swallowed and the panel silently goes
                # missing. (Startup failures like a bound port are
                # handled inside serve_web_app, which must contain
                # uvicorn's SystemExit before it reaches the loop.)
                if not task.cancelled() and task.exception() is not None:
                    logger.error(
                        "web admin panel stopped", exc_info=task.exception()
                    )

            # Keep a reference on the bot so the task isn't GC'd.
            bot.web_server_task = asyncio.create_task(serve_web_app(server))
            bot.web_server_task.add_done_callback(log_web_exit)
            logger.info(
                "starting web admin panel on %s:%d",
                config.WEB_HOST,
                config.WEB_PORT,
            )

    bot.setup_hook = setup_hook

    commands_deployed = False

    @bot.event
    async def on_ready():
        nonlocal commands_deployed
        logger.info("Connected as %s", bot.user)
        # on_ready fires again on every reconnect; only deploy commands once.
        if config.SYNC_COMMANDS and not commands_deployed:
            try:
                await deploy_commands(
                    bot.tree, bot.http, bot.application_id, list(bot.guilds)
                )
                commands_deployed = True
                logger.info("deployed commands to %d guild(s)", len(bot.guilds))
            except Exception:
                logger.exception("command deploy failed; will retry on next ready")
        # One-shot v1 -> v2 tag backfill against connected soundboards.
        try:
            await run_migration_if_needed(store, list(bot.guilds))
        except Exception:
            logger.exception(
                "tag migration failed; startup snapshot was v%d, will retry next startup",
                store.startup_version,
            )

    @bot.event
    async def on_guild_join(guild: discord.Guild):
        # Sync the moment we're invited so commands appear without a restart.
        logger.info("joined guild %s (id=%s); syncing commands", guild.name, guild.id)
        if config.SYNC_COMMANDS:
            try:
                await sync_guild_commands(bot.tree, guild)
            except Exception:
                logger.exception("failed to sync commands to new guild id=%s", guild.id)

    @bot.event
    async def on_close():
        store.save()

    return bot
