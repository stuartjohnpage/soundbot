import logging.handlers

from . import config
from .bot import create_bot

# Setup logging
logger = logging.getLogger("soundbot")
handler = logging.handlers.RotatingFileHandler(
    config.LOG_FILE, maxBytes=5_000_000, backupCount=3
)
handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Also log to console
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(console)

if not config.DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required")

bot = create_bot()
bot.run(config.DISCORD_TOKEN, log_handler=None)
