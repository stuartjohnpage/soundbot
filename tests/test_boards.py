"""BoardTracker: the registry of posted /board messages awaiting cleanup."""
import json

from soundbot.boards import BoardTracker


def test_add_and_pop_guild(tmp_path):
    tracker = BoardTracker(tmp_path / "boards.json")
    tracker.add(1, channel_id=10, message_id=100)
    tracker.add(1, channel_id=10, message_id=101)
    tracker.add(2, channel_id=20, message_id=200)

    assert tracker.pop_guild(1) == [(10, 100), (10, 101)]
    # Popped means gone: a second leave has nothing to delete.
    assert tracker.pop_guild(1) == []
    assert tracker.pop_guild(2) == [(20, 200)]


def test_pop_unknown_guild_is_empty(tmp_path):
    assert BoardTracker(tmp_path / "boards.json").pop_guild(99) == []


def test_pop_all_drains_every_guild(tmp_path):
    tracker = BoardTracker(tmp_path / "boards.json")
    tracker.add(1, channel_id=10, message_id=100)
    tracker.add(2, channel_id=20, message_id=200)

    assert sorted(tracker.pop_all()) == [(10, 100), (20, 200)]
    assert tracker.pop_all() == []


def test_persists_across_instances(tmp_path):
    """Boards outlive a restart (their buttons don't), so the registry
    must survive one for startup cleanup to find them."""
    path = tmp_path / "boards.json"
    BoardTracker(path).add(1, channel_id=10, message_id=100)

    assert BoardTracker(path).pop_guild(1) == [(10, 100)]
    # ...and the pop itself was persisted.
    assert BoardTracker(path).pop_guild(1) == []


def test_missing_file_starts_empty(tmp_path):
    tracker = BoardTracker(tmp_path / "nope" / "boards.json")
    assert tracker.pop_all() == []


def test_corrupt_file_starts_empty(tmp_path):
    """A garbled registry costs at most some orphaned boards — never a
    crash at startup."""
    path = tmp_path / "boards.json"
    path.write_text("{not json")
    tracker = BoardTracker(path)

    assert tracker.pop_all() == []
    tracker.add(1, channel_id=10, message_id=100)
    assert json.loads(path.read_text()) == {"1": [[10, 100]]}
