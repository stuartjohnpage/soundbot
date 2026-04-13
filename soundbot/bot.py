import asyncio
import logging
import random
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config
from .audio import extract_audio, has_video_stream, validate_sound
from .migration import run_migration_if_needed
from .mixer import MixerSource
from .pagination import paginate
from .pcm_cache import CachedPCMSource, PCMCache
from .store import SoundStore, parse_tags

logger = logging.getLogger("soundbot")

SOUNDS_PER_BOARD_PAGE = 20  # 5 rows * 5 cols - 1 row for nav = 4*5
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

    async def _play_sound(
        self, interaction: discord.Interaction, name: str, *, suppress_reply: bool = False
    ) -> None:
        try:
            vc = self._ensure_voice(interaction)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        entry = self.store.get(name)
        if not entry:
            await interaction.response.send_message(
                f"Sound **{name}** not found.", ephemeral=True
            )
            return

        # First play for a file pays the ffmpeg decode cost; every subsequent
        # press is an in-memory slice. Done via to_thread so a cold miss
        # doesn't block the event loop.
        try:
            pcm_bytes = await asyncio.to_thread(self.pcm_cache.get, entry["file"])
        except ValueError as exc:
            logger.warning("decode failed for %s: %s", name, exc)
            await interaction.response.send_message(
                f"Failed to decode **{name}**.", ephemeral=True
            )
            return

        # The to_thread await above is a yield point: a concurrent /leave can
        # tear down the mixer and disconnect the voice client before we get
        # back here. Re-check before touching self.mixer — otherwise we'd
        # silently drop the sound and leak an orphan mixer.
        if self.mixer is None or not vc.is_connected():
            logger.info("voice torn down during decode, dropping sound=%s", name)
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "Voice connection lost while loading sound.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
            return

        source = CachedPCMSource(pcm_bytes)
        self.mixer.add(source)

        self.store.increment_play_count(name)
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
        pages = paginate(sounds, per_page=SOUNDS_PER_BOARD_PAGE)
        view = BoardView(self, pages, page=0)
        embed = view.make_embed()
        await interaction.response.send_message(embed=embed, view=view)

    # -- CRUD commands --

    def _find_existing_by_path(self, path: Path) -> str | None:
        """Return the name of any store entry whose file is `path`, or None.

        Used by addsound and importsounds to refuse a write that would
        silently overwrite another sound's file. Pre-fix bug: two sounds
        with the same destination filename (different names) would both
        land at the same path on disk. The second write would clobber the
        first's bytes, corrupting the first entry without raising.

        Both sides are resolved to absolute paths before comparison so a
        relative-vs-absolute mismatch (e.g. `SOUNDS_DIR=sounds` in config
        vs `sounds/foo.mp3` already stored absolutely) doesn't defeat the
        check. `.resolve(strict=False)` doesn't raise on missing files,
        but we still guard against OSError on path encoding edge cases.
        """
        try:
            target = Path(path).resolve(strict=False)
        except OSError:
            return None
        for existing_name, existing_entry in self.store.list_sounds():
            try:
                if Path(existing_entry["file"]).resolve(strict=False) == target:
                    return existing_name
            except OSError:
                continue
        return None

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
        # Validate tags before any file I/O so we fail cleanly on bad input.
        try:
            tag_list = parse_tags(tags)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
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
            if has_video_stream(dest):
                audio_dest = dest.with_suffix(".mp3")
                if audio_dest.exists():
                    dest.unlink(missing_ok=True)
                    await interaction.followup.send(
                        f"A file named `{audio_dest.name}` already exists.",
                        ephemeral=True,
                    )
                    return
                # Same no-clobber guard as the pre-save check, but for the
                # extracted audio destination. Covers the case where a store
                # entry references a file path that was manually deleted off
                # disk — the .exists() check above would miss it.
                audio_owner = self._find_existing_by_path(audio_dest)
                if audio_owner is not None:
                    dest.unlink(missing_ok=True)
                    await interaction.followup.send(
                        f"Cannot upload: `{audio_dest.name}` is already in use "
                        f"by sound **{audio_owner}**. Remove that sound first.",
                        ephemeral=True,
                    )
                    return
                extract_audio(dest, audio_dest)
                dest.unlink(missing_ok=True)
                dest = audio_dest
            validate_sound(dest, config.MAX_DURATION)
            # Drop any stale cached PCM for this path before the new entry
            # is added. Two distinct sound names uploaded with the same
            # filename land at the same dest on disk, and a previous /play
            # may have populated the cache with the old file's bytes.
            self.pcm_cache.invalidate(dest)
            self.store.add(
                name, dest, category=category, uploaded_by=str(interaction.user)
            )
            for tag in tag_list:
                self.store.add_tag(name, tag)
            # Single save after the batch — add_tag mutates only in-memory state.
            self.store.save()
        except ValueError as exc:
            # Safe to unlink: _find_existing_by_path above guarantees no
            # other entry references this path, and the same check fires
            # for audio_dest post-extraction. (Concurrent /addsound calls
            # could race around the file.save yield point — that's a
            # pre-existing TOCTOU limitation, not introduced by this.)
            dest.unlink(missing_ok=True)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        msg = f"Added sound **{name}**."
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
                validate_sound(dest, config.MAX_DURATION)
                self.store.add(
                    key,
                    dest,
                    category=DISCORD_IMPORT_CATEGORY,
                    uploaded_by=str(interaction.user),
                )
                if guild_tag:
                    self.store.add_tag(key, guild_tag)
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


class BoardView(discord.ui.View):
    def __init__(self, cog: Soundboard, pages, page: int = 0) -> None:
        # timeout=None: buttons stay active until the bot restarts. Views aren't
        # persistent, so any existing boards go dead on restart — users re-run /board.
        super().__init__(timeout=None)
        self.cog = cog
        self.pages = pages
        self.page = page
        self._build_buttons()

    def _build_buttons(self) -> None:
        self.clear_items()
        for name, _ in self.pages[self.page]:
            btn = discord.ui.Button(label=name, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(name)
            self.add_item(btn)

        if len(self.pages) > 1:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary)
                prev_btn.callback = self._prev
                self.add_item(prev_btn)
            if self.page < len(self.pages) - 1:
                next_btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary)
                next_btn.callback = self._next
                self.add_item(next_btn)

    def make_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Soundboard",
            description=f"Page {self.page + 1}/{len(self.pages)}",
        )

    def _make_callback(self, name: str):
        async def callback(interaction: discord.Interaction):
            await self.cog._play_sound(interaction, name, suppress_reply=True)
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=self.make_embed(), view=self)

        return callback

    async def _prev(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._build_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._build_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


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
        await bot.add_cog(Soundboard(bot, store))
        if config.SYNC_COMMANDS:
            if config.GUILD_ID:
                guild = discord.Object(id=config.GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                # Wipe any leftover global registrations from prior non-guild
                # syncs so commands don't appear twice in the guild.
                bot.tree.clear_commands(guild=None)
                await bot.tree.sync()
            else:
                await bot.tree.sync()

    bot.setup_hook = setup_hook

    @bot.event
    async def on_ready():
        logger.info("Connected as %s", bot.user)
        # One-shot v1 -> v2 tag backfill against connected soundboards.
        try:
            await run_migration_if_needed(store, list(bot.guilds))
        except Exception:
            logger.exception(
                "tag migration failed; startup snapshot was v%d, will retry next startup",
                store.startup_version,
            )

    @bot.event
    async def on_close():
        store.save()

    return bot
