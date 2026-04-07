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
MAX_DURATION: float = 6.0
SYNC_COMMANDS: bool = os.getenv("SYNC_COMMANDS", "true").lower() == "true"
