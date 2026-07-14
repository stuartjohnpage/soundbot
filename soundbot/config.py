import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
ADMIN_ROLE: str = os.getenv("ADMIN_ROLE", "Soundbot Admin")
SOUNDS_DIR: Path = Path(os.getenv("SOUNDS_DIR", "./sounds"))
METADATA_FILE: Path = Path(os.getenv("METADATA_FILE", "./sounds.json"))
DEFAULT_VOLUME: int = int(os.getenv("DEFAULT_VOLUME", "50"))
LOG_FILE: Path = Path(os.getenv("LOG_FILE", "./soundbot.log"))
MAX_DURATION: float = 6.4
# Uploads louder than this are attenuated down to it (never boosted up).
TARGET_LUFS: float = float(os.getenv("TARGET_LUFS", "-16"))
SYNC_COMMANDS: bool = os.getenv("SYNC_COMMANDS", "true").lower() == "true"
# Web admin panel (issue #1). Empty token = panel disabled entirely.
WEB_TOKEN: str = os.getenv("WEB_TOKEN", "")
WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT: int = int(os.getenv("WEB_PORT", "8000"))
