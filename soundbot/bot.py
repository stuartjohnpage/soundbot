import logging
import random
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config
from .audio import extract_audio, has_video_stream, validate_sound
from .mixer import MixerSource
from .pagination import paginate
from .store import (
    CURRENT_SCHEMA_VERSION,
    SoundStore,
    migrate_v1_to_v2,
    parse_tags,
)

logger = logging.getLogger("soundbot")

SOUNDS_PER_BOARD_PAGE = 20  # 5 rows * 5 cols - 1 row for nav = 4*5
DISCORD_IMPORT_CATEGORY = "discord-import"
_MAX_SUMMARY_LENGTH = 1900  # Leave headroom under Discord's 2000-char limit


async def run_migration_if_needed(store: SoundStore, guilds) -> None:
    """Backfill v1 sounds.json with sanitized-guild-name tags from each
    connected guild's Discord soundboard, then save at v2.

    No-op when the store is already at v2. Atomic: if any guild's fetch
    raises, no save happens and the file stays at v1 so the next startup
    retries.

    Tested via tests/test_migration.py with fake guild objects.
    """
    if store.loaded_version >= CURRENT_SCHEMA_VERSION:
        return

    # Fetch each guild once and build the sanitized {tag: {sound_names}} map.
    guild_map: dict[str, set[str]] = {}
    for guild in guilds:
        try:
            guild_tag = SoundStore.sanitize_tag(guild.name)
        except ValueError:
            logger.warning(
                "skipping guild %r during migration: name cannot be sanitized",
                getattr(guild, "name", "?"),
            )
            continue
        # Single fetch per guild.
        sounds = await guild.fetch_soundboard_sounds()
        sanitized_sound_names: set[str] = set()
        for s in sounds:
            try:
                sanitized_sound_names.add(SoundStore.sanitize_name(s.name))
            except ValueError:
                continue
        guild_map[guild_tag] = sanitized_sound_names

    v1_data = {"version": store.loaded_version, "sounds": store._sounds}
    v2_data = migrate_v1_to_v2(v1_data, guild_map)
    # Replace store contents in-memory and persist atomically.
    store._sounds = v2_data["sounds"]
    store.save()

    tagged = sum(1 for e in store._sounds.values() if e.get("tags"))
    untagged = len(store._sounds) - tagged
    logger.info(
        "tag migration complete: processed=%d tagged=%d untagged=%d",
        len(store._sounds),
        tagged,
        untagged,
    )


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
    tag_group = app_commands.Group(
        name="tag",
        description="Manage sound tags",
    )

    def __init__(self, bot: commands.Bot, store: SoundStore) -> None:
        self.bot = bot
        self.store = store
        self.mixer: MixerSource | None = None
        self.volume: float = config.DEFAULT_VOLUME / 100.0

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
            self.mixer = MixerSource()
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

        source = discord.FFmpegPCMAudio(
            entry["file"],
            options=f"-filter:a volume={self.volume}",
        )
        if self.mixer is None:
            self.mixer = MixerSource()
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
        # carrying that tag.
        tag = None
        try:
            tag = interaction.namespace.tag
        except AttributeError:
            pass
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
        sound_name = None
        try:
            sound_name = interaction.namespace.sound
        except AttributeError:
            pass
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
                extract_audio(dest, audio_dest)
                dest.unlink(missing_ok=True)
                dest = audio_dest
            validate_sound(dest, config.MAX_DURATION)
            self.store.add(
                name, dest, category=category, uploaded_by=str(interaction.user)
            )
            for tag in tag_list:
                self.store.add_tag(name, tag)
            self.store.save()
        except ValueError as exc:
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
        try:
            self.store.remove(name)
            self.store.save()
        except KeyError:
            await interaction.response.send_message(
                f"Sound **{name}** not found.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"Removed sound **{name}**.")

    @app_commands.command(name="renamesound", description="Rename a sound")
    @app_commands.describe(old="Current name", new="New name")
    @app_commands.autocomplete(old=_sound_autocomplete)
    @_admin_check()
    async def renamesound(
        self, interaction: discord.Interaction, old: str, new: str
    ) -> None:
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
        tagged_existing: list[str] = []
        skipped = []
        failed = []
        for sound in sounds:
            try:
                key = SoundStore.sanitize_name(sound.name)
            except ValueError:
                key = f"sound_{sound.id}"
            existing_entry = self.store.get(key)
            if existing_entry is not None:
                # Re-import path: don't re-download, but apply the guild tag
                # so cross-server origins are tracked.
                if guild_tag and guild_tag not in existing_entry.get("tags", []):
                    self.store.add_tag(key, guild_tag)
                    tagged_existing.append(key)
                else:
                    skipped.append(key)
                continue
            dest = config.SOUNDS_DIR / f"{key}.ogg"
            if dest.exists():
                skipped.append(f"{key} (file exists)")
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
        if skipped:
            names = ", ".join(skipped)
            parts.append(f"Skipped {len(skipped)} (already tagged): {names}")
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
        except KeyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
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
                "tag migration failed; sounds.json left at v%d for retry",
                store.loaded_version,
            )

    @bot.event
    async def on_close():
        store.save()

    return bot
