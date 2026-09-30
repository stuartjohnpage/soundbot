"""Registry of posted /board messages, so they can be deleted later.

Board buttons only work while the bot sits in voice (and while this
process lives — BoardView isn't persistent), so once the bot leaves, its
boards are clutter. The registry is persisted because boards outlive a
restart even though their buttons don't: startup cleanup needs to find
what the previous process posted.

Only ever touched from the event loop (/board and the voice-state
listener), so unlike SoundStore it needs no lock.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger("soundbot")

# (channel_id, message_id)
BoardRef = tuple[int, int]


class BoardTracker:
    def __init__(self, path: Path | None) -> None:
        """`path=None` keeps the registry in memory only."""
        self._path = path
        self._boards: dict[int, list[BoardRef]] = self._load()

    def add(self, guild_id: int, *, channel_id: int, message_id: int) -> None:
        self._boards.setdefault(guild_id, []).append((channel_id, message_id))
        self._save()

    def pop_guild(self, guild_id: int) -> list[BoardRef]:
        """Remove and return every board recorded for one guild."""
        boards = self._boards.pop(guild_id, [])
        if boards:
            self._save()
        return boards

    def pop_all(self) -> list[BoardRef]:
        """Remove and return every board recorded for every guild."""
        boards = [ref for refs in self._boards.values() for ref in refs]
        if boards:
            self._boards = {}
            self._save()
        return boards

    def _load(self) -> dict[int, list[BoardRef]]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
            return {
                int(guild_id): [(int(c), int(m)) for c, m in refs]
                for guild_id, refs in raw.items()
            }
        except (ValueError, TypeError, AttributeError) as exc:
            # Worst case of dropping a bad registry is a few orphaned
            # boards someone deletes by hand — not worth failing startup.
            logger.warning("ignoring unreadable board registry %s: %s", self._path, exc)
            return {}

    def _save(self) -> None:
        if self._path is None:
            return
        data = {str(guild_id): refs for guild_id, refs in self._boards.items()}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._path)
