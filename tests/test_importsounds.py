"""Unit tests for the /importsounds classifier helper.

The classifier is a pure function extracted from the importsounds command
loop so the bucketing decisions can be tested without standing up a real
discord.Interaction. Each soundboard sound from Discord is one of:

  - "needs_download": no local entry, no file collision -> download path
  - "tagged_existing": local entry exists, did not have the guild tag yet
  - "already_tagged": local entry exists and already carries the guild tag
  - "file_conflict": no local entry, but a file with the destination name
    already exists on disk in a different format

The pre-fix bug was that "tagged_existing", "already_tagged", and
"file_conflict" all collapsed into one "skipped" bucket reported as
"already tagged", which is factually wrong for two of the three cases.
"""

from soundbot.bot import classify_import_sound


class TestClassifyImportSound:
    def test_needs_download_when_no_existing_and_no_file_conflict(self):
        result = classify_import_sound(
            existing_entry=None,
            dest_exists=False,
            guild_tag="my-server",
        )
        assert result == "needs_download"

    def test_needs_download_when_guild_tag_is_none_and_no_local_state(self):
        result = classify_import_sound(
            existing_entry=None,
            dest_exists=False,
            guild_tag=None,
        )
        assert result == "needs_download"

    def test_file_conflict_when_no_existing_but_dest_exists(self):
        """A file with the destination name already exists in a different
        format — refuse to clobber it."""
        result = classify_import_sound(
            existing_entry=None,
            dest_exists=True,
            guild_tag="my-server",
        )
        assert result == "file_conflict"

    def test_tagged_existing_when_local_exists_and_lacks_guild_tag(self):
        result = classify_import_sound(
            existing_entry={"tags": ["other-server"]},
            dest_exists=False,
            guild_tag="my-server",
        )
        assert result == "tagged_existing"

    def test_already_tagged_when_local_exists_and_has_guild_tag(self):
        result = classify_import_sound(
            existing_entry={"tags": ["my-server"]},
            dest_exists=False,
            guild_tag="my-server",
        )
        assert result == "already_tagged"

    def test_already_tagged_when_local_exists_and_guild_tag_is_none(self):
        """Existing entry with no auto-tag to apply: nothing to do.

        Pre-fix this fell into the misleading "skipped (already tagged)"
        bucket; semantically it's "no action needed" so we lump it under
        already_tagged (the bucket meaning is now: existing entry, no
        new tag was added). The summary line distinguishes it from the
        file_conflict case so the user isn't lied to.
        """
        result = classify_import_sound(
            existing_entry={"tags": []},
            dest_exists=False,
            guild_tag=None,
        )
        assert result == "already_tagged"

    def test_existing_entry_takes_precedence_over_dest_exists(self):
        """If we already track the sound, we don't care about file collisions —
        the file we'd download is the one we're already tracking."""
        result = classify_import_sound(
            existing_entry={"tags": ["my-server"]},
            dest_exists=True,
            guild_tag="my-server",
        )
        assert result == "already_tagged"
