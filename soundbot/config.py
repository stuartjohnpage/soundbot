import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
ADMIN_ROLE: str = os.getenv("ADMIN_ROLE", "Soundbot Admin")
SOUNDS_DIR: Path = Path(os.getenv("SOUNDS_DIR", "./sounds"))
METADATA_FILE: Path = Path(os.getenv("METADATA_FILE", "./sounds.json"))
# Posted /board messages awaiting cleanup once the bot leaves voice.
# Lives beside the metadata file so it lands on the same data volume.
BOARDS_FILE: Path = Path(
    os.getenv("BOARDS_FILE", str(METADATA_FILE.with_name("boards.json")))
)
DEFAULT_VOLUME: int = int(os.getenv("DEFAULT_VOLUME", "50"))
LOG_FILE: Path = Path(os.getenv("LOG_FILE", "./soundbot.log"))
MAX_DURATION: float = 6.4
# Uploads louder than this are attenuated down to it (never boosted up).
TARGET_LUFS: float = float(os.getenv("TARGET_LUFS", "-16"))
SYNC_COMMANDS: bool = os.getenv("SYNC_COMMANDS", "true").lower() == "true"
# Seconds the bot may sit alone (no human members) in a voice channel
# before disconnecting itself. 0 disables auto-leave.
IDLE_TIMEOUT: float = float(os.getenv("IDLE_TIMEOUT", "600"))
# Auto-join: the bot walks into a watched voice channel on its own the
# moment a human appears there. Scoped to the single guild named by
# AUTO_JOIN_GUILD so the bot's other servers never get surprise joins;
# leaving either setting empty disables the feature outright.
# Both accept a snowflake id or a (case-insensitive) name -- ids survive a
# rename, names save hunting through Discord's developer mode.
AUTO_JOIN_GUILD: str = os.getenv("AUTO_JOIN_GUILD", "").strip()
AUTO_JOIN_CHANNELS: tuple[str, ...] = tuple(
    ref.strip()
    for ref in os.getenv("AUTO_JOIN_CHANNELS", "").split(",")
    if ref.strip()
)
# Seconds a deliberate exit (/leave, or someone disconnecting the bot in
# Discord) suppresses auto-join in that guild, so the bot does not bounce
# straight back in on the next arrival. 0 = no suppression.
AUTO_JOIN_COOLDOWN: float = float(os.getenv("AUTO_JOIN_COOLDOWN", "300"))
# Text channel in the auto-join guild where the bot posts a /board each time
# it auto-joins, by id or (case-insensitive) name; a leading "#" is ignored.
# Empty = no board.
AUTO_JOIN_BOARD_CHANNEL: str = (
    os.getenv("AUTO_JOIN_BOARD_CHANNEL", "").strip().lstrip("#").strip()
)


def _parse_join_sounds(raw: str) -> dict[str, str]:
    """Parse "who:tag,who:tag" into {who: tag}, dropping malformed pairs.

    `who` is a Discord username or user id; tags are lowercased to match
    how the store keeps them.
    """
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        who, sep, tag = item.partition(":")
        if sep and who.strip() and tag.strip():
            pairs[who.strip()] = tag.strip().lower()
    return pairs


# Join sounds: when one of these people walks into the bot's voice channel
# while someone else is already there, play a random sound carrying their
# tag. Comma-separated "username:tag" pairs (a user id works in place of
# the username). Empty = off.
JOIN_SOUNDS: dict[str, str] = _parse_join_sounds(os.getenv("JOIN_SOUNDS", ""))

# Web admin panel (issue #1). Empty token = panel disabled entirely.
WEB_TOKEN: str = os.getenv("WEB_TOKEN", "")
WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT: int = int(os.getenv("WEB_PORT", "8000"))
