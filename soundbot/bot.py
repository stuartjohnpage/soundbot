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
from .store import SoundStore

logger = logging.getLogger("soundbot")

SOUNDS_PER_BOARD_PAGE = 20  # 5 rows * 5 cols - 1 row for nav = 4*5
DISCORD_IMPORT_CATEGORY = "discord-import"
_MAX_SUMMARY_LENGTH = 1900  # Leave headroom under Discord's 2000-char limit


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
        matches = self.store.search(current)
        return [
            app_commands.Choice(name=n, value=n) for n, _ in matches[:25]
        ]

    # -- Commands --

    @app_commands.command(name="play", description="Play a sound")
    @app_commands.describe(name="Sound name")
    @app_commands.autocomplete(name=_sound_autocomplete)
    @_admin_check()
    async def play(self, interaction: discord.Interaction, name: str) -> None:
        await self._play_sound(interaction, name)

    @app_commands.command(name="random", description="Play a random sound")
    @app_commands.describe(category="Optional category filter")
    @_admin_check()
    async def random_sound(
        self, interaction: discord.Interaction, category: str | None = None
    ) -> None:
        sounds = self.store.list_sounds(category=category)
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
    @_admin_check()
    async def board(self, interaction: discord.Interaction) -> None:
        sounds = self.store.list_sounds()
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
    )
    @_admin_check()
    async def addsound(
        self,
        interaction: discord.Interaction,
        name: str,
        file: discord.Attachment,
        category: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
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
            self.store.save()
        except ValueError as exc:
            dest.unlink(missing_ok=True)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(f"Added sound **{name}**.")

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
        skipped = []
        failed = []
        for sound in sounds:
            try:
                key = SoundStore.sanitize_name(sound.name)
            except ValueError:
                key = f"sound_{sound.id}"
            if self.store.get(key):
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
                imported.append(key)
            except (discord.HTTPException, ValueError, OSError) as exc:
                dest.unlink(missing_ok=True)
                failed.append(f"{key}: {exc}")
                logger.warning("Failed to import soundboard sound %s: %s", key, exc)

        if imported:
            self.store.save()
        parts = [f"**Imported {len(imported)}** sound(s)."]
        if skipped:
            names = ", ".join(skipped)
            parts.append(f"Skipped {len(skipped)} (already exist): {names}")
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

    @bot.event
    async def on_close():
        store.save()

    return bot
