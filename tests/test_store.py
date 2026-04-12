import json
from pathlib import Path

import pytest

from soundbot.store import SoundStore


class TestAddAndRetrieve:
    def test_add_sound_and_get_by_name(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        sound_file = sounds_dir / "airhorn.mp3"
        sound_file.write_bytes(b"fake audio data")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", sound_file, category="memes", uploaded_by="stuart#1234")

        result = store.get("airhorn")
        assert result is not None
        assert result["file"] == str(sound_file)
        assert result["category"] == "memes"
        assert result["uploaded_by"] == "stuart#1234"
        assert result["play_count"] == 0
        assert "uploaded_at" in result


class TestNameValidation:
    def _make_store(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        return SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )

    def _make_file(self, tmp_path):
        f = tmp_path / "sounds" / "test.mp3"
        f.write_bytes(b"fake audio")
        return f

    def test_rejects_name_with_spaces(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add("air horn", self._make_file(tmp_path))

    def test_rejects_name_with_special_chars(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add("air!horn", self._make_file(tmp_path))

    def test_rejects_name_too_long(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add("a" * 33, self._make_file(tmp_path))

    def test_allows_hyphens_and_underscores(self, tmp_path):
        store = self._make_store(tmp_path)
        store.add("air-horn_2", self._make_file(tmp_path))
        assert store.get("air-horn_2") is not None

    def test_rejects_duplicate_name_case_insensitive(self, tmp_path):
        store = self._make_store(tmp_path)
        f = self._make_file(tmp_path)
        store.add("airhorn", f)
        with pytest.raises(ValueError, match="already exists"):
            store.add("AirHorn", f)


class TestRemove:
    def test_remove_deletes_metadata_and_file(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        sound_file = sounds_dir / "airhorn.mp3"
        sound_file.write_bytes(b"fake audio data")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", sound_file)
        store.remove("airhorn")

        assert store.get("airhorn") is None
        assert not sound_file.exists()

    def test_remove_nonexistent_raises(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        with pytest.raises(KeyError):
            store.remove("nope")


class TestRename:
    def test_rename_moves_metadata_keeps_file(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        sound_file = sounds_dir / "airhorn.mp3"
        sound_file.write_bytes(b"fake audio data")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", sound_file, category="memes")
        store.rename("airhorn", "klaxon")

        assert store.get("airhorn") is None
        assert store.get("klaxon") is not None
        assert store.get("klaxon")["file"] == str(sound_file)
        assert store.get("klaxon")["category"] == "memes"
        assert sound_file.exists()

    def test_rename_validates_new_name(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        sound_file = sounds_dir / "airhorn.mp3"
        sound_file.write_bytes(b"fake audio data")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", sound_file)
        with pytest.raises(ValueError, match="invalid"):
            store.rename("airhorn", "bad name!")


class TestListSounds:
    def test_list_all_sounds(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f1 = sounds_dir / "airhorn.mp3"
        f1.write_bytes(b"fake")
        f2 = sounds_dir / "rimshot.mp3"
        f2.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", f1, category="memes")
        store.add("rimshot", f2, category="comedy")

        result = store.list_sounds()
        names = [s[0] for s in result]
        assert "airhorn" in names
        assert "rimshot" in names
        assert len(result) == 2

    def test_list_filtered_by_category(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f1 = sounds_dir / "airhorn.mp3"
        f1.write_bytes(b"fake")
        f2 = sounds_dir / "rimshot.mp3"
        f2.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", f1, category="memes")
        store.add("rimshot", f2, category="comedy")

        result = store.list_sounds(category="memes")
        assert len(result) == 1
        assert result[0][0] == "airhorn"

    def test_categories_returns_distinct(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f1 = sounds_dir / "a.mp3"
        f1.write_bytes(b"fake")
        f2 = sounds_dir / "b.mp3"
        f2.write_bytes(b"fake")
        f3 = sounds_dir / "c.mp3"
        f3.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("a", f1, category="memes")
        store.add("b", f2, category="memes")
        store.add("c", f3, category="comedy")

        cats = store.categories()
        assert set(cats) == {"memes", "comedy"}


class TestSearch:
    def test_search_exact_prefix_ranked_first(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        for name in ["airhorn", "air-raid", "foghorn", "airbag"]:
            f = sounds_dir / f"{name}.mp3"
            f.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        for name in ["airhorn", "air-raid", "foghorn", "airbag"]:
            store.add(name, sounds_dir / f"{name}.mp3")

        results = store.search("air")
        names = [r[0] for r in results]
        # All "air*" names should come back; "foghorn" should not
        assert "airhorn" in names
        assert "air-raid" in names
        assert "airbag" in names
        assert "foghorn" not in names

    def test_search_substring_match(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        for name in ["airhorn", "foghorn", "rimshot"]:
            f = sounds_dir / f"{name}.mp3"
            f.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        for name in ["airhorn", "foghorn", "rimshot"]:
            store.add(name, sounds_dir / f"{name}.mp3")

        results = store.search("horn")
        names = [r[0] for r in results]
        assert "airhorn" in names
        assert "foghorn" in names
        assert "rimshot" not in names

    def test_search_case_insensitive(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", f)

        results = store.search("AIR")
        assert len(results) >= 1
        assert results[0][0] == "airhorn"

    def test_search_empty_query_returns_all(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", f)

        results = store.search("")
        assert len(results) == 1


class TestPlayCount:
    def test_increment_play_count(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", f)
        assert store.get("airhorn")["play_count"] == 0

        store.increment_play_count("airhorn")
        assert store.get("airhorn")["play_count"] == 1

        store.increment_play_count("airhorn")
        store.increment_play_count("airhorn")
        assert store.get("airhorn")["play_count"] == 3

    def test_increment_nonexistent_raises(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        with pytest.raises(KeyError):
            store.increment_play_count("nope")


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake audio data")
        metadata_path = tmp_path / "sounds.json"

        store = SoundStore(metadata_path=metadata_path, sounds_dir=sounds_dir)
        store.add("airhorn", f, category="memes", uploaded_by="stuart#1234")
        store.increment_play_count("airhorn")
        store.save()

        # Load into a new store instance
        store2 = SoundStore(metadata_path=metadata_path, sounds_dir=sounds_dir)
        result = store2.get("airhorn")
        assert result is not None
        assert result["file"] == str(f)
        assert result["category"] == "memes"
        assert result["uploaded_by"] == "stuart#1234"
        assert result["play_count"] == 1

    def test_load_nonexistent_file_starts_empty(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        assert store.list_sounds() == []

    def test_save_creates_valid_json_with_version(self, tmp_path):
        import json

        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake")
        metadata_path = tmp_path / "sounds.json"

        store = SoundStore(metadata_path=metadata_path, sounds_dir=sounds_dir)
        store.add("airhorn", f)
        store.save()

        data = json.loads(metadata_path.read_text())
        assert "sounds" in data
        assert "version" in data
        assert data["version"] == 2
        assert "airhorn" in data["sounds"]


class TestFolderScan:
    def test_scan_imports_untracked_files(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        (sounds_dir / "airhorn.mp3").write_bytes(b"fake")
        (sounds_dir / "rimshot.wav").write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.scan_folder()

        assert store.get("airhorn") is not None
        assert store.get("rimshot") is not None
        assert store.get("airhorn")["category"] is None

    def test_scan_infers_category_from_subfolder(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        memes_dir = sounds_dir / "memes"
        memes_dir.mkdir()
        (memes_dir / "airhorn.mp3").write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.scan_folder()

        result = store.get("airhorn")
        assert result is not None
        assert result["category"] == "memes"

    def test_scan_skips_already_tracked(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add("airhorn", f, category="custom")
        store.scan_folder()

        # Category should remain "custom", not be overwritten
        assert store.get("airhorn")["category"] == "custom"

    def test_scan_handles_duplicate_filenames_in_subfolders(self, tmp_path):
        """If memes/airhorn.mp3 and sfx/airhorn.mp3 both exist, only first is imported."""
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        memes = sounds_dir / "memes"
        memes.mkdir()
        sfx = sounds_dir / "sfx"
        sfx.mkdir()
        (memes / "airhorn.mp3").write_bytes(b"fake")
        (sfx / "airhorn.mp3").write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.scan_folder()

        # Should have imported one, not crashed
        assert store.get("airhorn") is not None

    def test_scan_skips_file_tracked_under_different_path_form(self, tmp_path):
        """scan_folder should not re-import a file stored with a different path string form.

        If the file is tracked under a relative path like 'sounds/airhorn.mp3'
        but scan_folder sees the absolute path, it should recognize they're the same file.
        """
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        sound_file = sounds_dir / "airhorn.mp3"
        sound_file.write_bytes(b"fake")

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        # Add the sound with a custom name (different from stem) so name check doesn't shortcut
        store.add("my-horn", sound_file, category="custom")
        # Overwrite the stored file path to use the un-resolved form
        # (e.g., includes ../ or different casing on Windows)
        alt_path = sounds_dir / ".." / "sounds" / "airhorn.mp3"
        store._sounds["my-horn"]["file"] = str(alt_path)

        # scan_folder should recognize this file is already tracked
        store.scan_folder()

        # The file should NOT be re-imported under its stem name "airhorn"
        # because it's already tracked (just under a different path form)
        assert store.get("airhorn") is None
        assert len(store.list_sounds()) == 1


class TestTags:
    def _store_with_sound(self, tmp_path, name="airhorn"):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / f"{name}.mp3"
        f.write_bytes(b"fake")
        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        store.add(name, f)
        return store

    def test_new_sound_starts_with_empty_tags(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        assert store.get("airhorn")["tags"] == []

    def test_add_tag_appends_to_list(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        assert store.get("airhorn")["tags"] == ["meme"]

    def test_add_tag_dedupes_on_double_add(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        store.add_tag("airhorn", "meme")
        assert store.get("airhorn")["tags"] == ["meme"]

    def test_add_tag_stores_sorted(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        store.add_tag("airhorn", "alpha")
        store.add_tag("airhorn", "zulu")
        assert store.get("airhorn")["tags"] == ["alpha", "meme", "zulu"]

    def test_add_tag_rejects_invalid_chars(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add_tag("airhorn", "Bad Tag!")

    def test_add_tag_rejects_uppercase(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add_tag("airhorn", "MyTag")

    def test_add_tag_rejects_too_long(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add_tag("airhorn", "a" * 33)

    def test_add_tag_rejects_empty(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.add_tag("airhorn", "")

    def test_add_tag_accepts_alphanumeric_and_hyphen(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "my-server-2")
        assert "my-server-2" in store.get("airhorn")["tags"]

    def test_add_tag_unknown_sound_raises(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(KeyError):
            store.add_tag("nope", "meme")

    def test_remove_tag_removes_existing(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        store.add_tag("airhorn", "funny")
        store.remove_tag("airhorn", "meme")
        assert store.get("airhorn")["tags"] == ["funny"]

    def test_remove_tag_unknown_tag_raises(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        with pytest.raises(KeyError):
            store.remove_tag("airhorn", "nope")

    def test_remove_tag_unknown_sound_raises(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(KeyError):
            store.remove_tag("nope", "meme")

    def test_list_tags_for_sound(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        store.add_tag("airhorn", "funny")
        assert store.list_tags("airhorn") == ["funny", "meme"]

    def test_list_tags_unknown_sound_raises(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        with pytest.raises(KeyError):
            store.list_tags("nope")

    def test_global_tags_returns_counts_sorted_desc(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        for n in ["a", "b", "c"]:
            f = sounds_dir / f"{n}.mp3"
            f.write_bytes(b"fake")
        store = SoundStore(metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir)
        for n in ["a", "b", "c"]:
            store.add(n, sounds_dir / f"{n}.mp3")
        store.add_tag("a", "meme")
        store.add_tag("a", "funny")
        store.add_tag("b", "meme")
        store.add_tag("b", "funny")
        store.add_tag("c", "meme")

        result = store.global_tags()
        # meme: 3, funny: 2 — sorted by count desc
        assert result == [("meme", 3), ("funny", 2)]

    def test_global_tags_returns_empty_when_no_tags(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        assert store.global_tags() == []

    def test_list_sounds_filtered_by_tag(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        for n in ["a", "b", "c"]:
            f = sounds_dir / f"{n}.mp3"
            f.write_bytes(b"fake")
        store = SoundStore(metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir)
        for n in ["a", "b", "c"]:
            store.add(n, sounds_dir / f"{n}.mp3")
        store.add_tag("a", "meme")
        store.add_tag("c", "meme")

        result = store.list_sounds(tag="meme")
        names = [s[0] for s in result]
        assert names == ["a", "c"]

    def test_list_sounds_with_no_tag_filter_returns_all(self, tmp_path):
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        for n in ["a", "b"]:
            f = sounds_dir / f"{n}.mp3"
            f.write_bytes(b"fake")
        store = SoundStore(metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir)
        for n in ["a", "b"]:
            store.add(n, sounds_dir / f"{n}.mp3")
        store.add_tag("a", "meme")
        # No filter → both
        result = store.list_sounds()
        assert len(result) == 2

    def test_save_writes_version_2(self, tmp_path):
        import json

        store = self._store_with_sound(tmp_path)
        store.save()
        data = json.loads((tmp_path / "sounds.json").read_text())
        assert data["version"] == 2

    def test_save_persists_tags(self, tmp_path):
        store = self._store_with_sound(tmp_path)
        store.add_tag("airhorn", "meme")
        store.add_tag("airhorn", "funny")
        store.save()

        # Reload
        store2 = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=tmp_path / "sounds",
        )
        assert store2.get("airhorn")["tags"] == ["funny", "meme"]


class TestV1BackCompat:
    def test_loads_v1_file_without_tags(self, tmp_path):
        """A v1 sounds.json file (no tags) must load and every entry gets tags: []."""
        import json

        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        f = sounds_dir / "airhorn.mp3"
        f.write_bytes(b"fake")

        v1_data = {
            "version": 1,
            "sounds": {
                "airhorn": {
                    "file": str(f),
                    "category": "memes",
                    "uploaded_by": "stuart#1234",
                    "uploaded_at": "2025-01-01T00:00:00+00:00",
                    "play_count": 5,
                },
                "rimshot": {
                    "file": str(sounds_dir / "rimshot.mp3"),
                    "category": None,
                    "uploaded_by": "user#0001",
                    "uploaded_at": "2025-01-02T00:00:00+00:00",
                    "play_count": 0,
                },
            },
        }
        (tmp_path / "sounds.json").write_text(json.dumps(v1_data))

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )

        # Both entries should be loaded with empty tags
        assert store.get("airhorn")["tags"] == []
        assert store.get("rimshot")["tags"] == []
        # Existing fields preserved
        assert store.get("airhorn")["category"] == "memes"
        assert store.get("airhorn")["play_count"] == 5
        assert store.get("airhorn")["uploaded_by"] == "stuart#1234"

    def test_v1_store_reports_version_for_migration(self, tmp_path):
        """The store must expose loaded version so the migration code can decide to run."""
        import json

        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        v1_data = {"version": 1, "sounds": {}}
        (tmp_path / "sounds.json").write_text(json.dumps(v1_data))

        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        assert store.loaded_version == 1

    def test_new_store_reports_current_version(self, tmp_path):
        """A fresh store (no file) reports the current schema version (2)."""
        sounds_dir = tmp_path / "sounds"
        sounds_dir.mkdir()
        store = SoundStore(
            metadata_path=tmp_path / "sounds.json",
            sounds_dir=sounds_dir,
        )
        assert store.loaded_version == 2


class TestMigrationPureFunction:
    """Pure-function migration: v1 store dict + guild→sounds mapping → v2 store dict.

    The mapping is supplied by the caller; no Discord API is involved here.
    """

    def _v1(self, sounds: dict) -> dict:
        return {"version": 1, "sounds": sounds}

    def _entry(self, **overrides) -> dict:
        base = {
            "file": "/tmp/x.mp3",
            "category": None,
            "uploaded_by": "u#1",
            "uploaded_at": "2025-01-01T00:00:00+00:00",
            "play_count": 0,
        }
        base.update(overrides)
        return base

    def test_zero_guild_match_leaves_tags_empty(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({"airhorn": self._entry()})
        guild_map: dict[str, set[str]] = {
            "alpha": {"rimshot"},
            "beta": {"klaxon"},
        }
        v2 = migrate_v1_to_v2(v1, guild_map)

        assert v2["version"] == 2
        assert v2["sounds"]["airhorn"]["tags"] == []

    def test_one_guild_match_applies_one_tag(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({"airhorn": self._entry()})
        guild_map = {
            "alpha": {"airhorn"},
            "beta": {"klaxon"},
        }
        v2 = migrate_v1_to_v2(v1, guild_map)

        assert v2["sounds"]["airhorn"]["tags"] == ["alpha"]

    def test_multi_guild_match_applies_multiple_tags(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({"airhorn": self._entry()})
        guild_map = {
            "alpha": {"airhorn"},
            "beta": {"airhorn"},
            "gamma": {"airhorn"},
        }
        v2 = migrate_v1_to_v2(v1, guild_map)

        assert v2["sounds"]["airhorn"]["tags"] == ["alpha", "beta", "gamma"]

    def test_migration_preserves_existing_fields(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({
            "airhorn": self._entry(
                file="/data/airhorn.mp3",
                category="memes",
                uploaded_by="stuart#1234",
                play_count=42,
            )
        })
        guild_map = {"alpha": {"airhorn"}}
        v2 = migrate_v1_to_v2(v1, guild_map)

        e = v2["sounds"]["airhorn"]
        assert e["file"] == "/data/airhorn.mp3"
        assert e["category"] == "memes"
        assert e["uploaded_by"] == "stuart#1234"
        assert e["play_count"] == 42
        assert e["tags"] == ["alpha"]

    def test_migration_does_not_mutate_input(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({"airhorn": self._entry()})
        guild_map = {"alpha": {"airhorn"}}
        original_v1 = json.loads(json.dumps(v1))  # deep copy snapshot

        migrate_v1_to_v2(v1, guild_map)

        assert v1 == original_v1, "migration must not mutate input v1 dict"

    def test_migration_handles_pre_existing_tags_field(self):
        """If a v1 entry somehow already has tags, the migration appends to them."""
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({
            "airhorn": self._entry(tags=["pre-existing"])
        })
        guild_map = {"alpha": {"airhorn"}}
        v2 = migrate_v1_to_v2(v1, guild_map)

        assert v2["sounds"]["airhorn"]["tags"] == ["alpha", "pre-existing"]

    def test_migration_returns_v2_marker(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({})
        v2 = migrate_v1_to_v2(v1, {})
        assert v2["version"] == 2

    def test_migration_dedupes_overlap_with_existing_tags(self):
        from soundbot.store import migrate_v1_to_v2

        v1 = self._v1({"airhorn": self._entry(tags=["alpha"])})
        guild_map = {"alpha": {"airhorn"}}
        v2 = migrate_v1_to_v2(v1, guild_map)
        assert v2["sounds"]["airhorn"]["tags"] == ["alpha"]


class TestParseTags:
    def test_parses_comma_separated(self):
        from soundbot.store import parse_tags

        assert parse_tags("meme,funny,dave") == ["dave", "funny", "meme"]

    def test_strips_whitespace(self):
        from soundbot.store import parse_tags

        assert parse_tags("meme , funny , dave") == ["dave", "funny", "meme"]

    def test_dedupes(self):
        from soundbot.store import parse_tags

        assert parse_tags("meme,meme,funny") == ["funny", "meme"]

    def test_empty_string_returns_empty_list(self):
        from soundbot.store import parse_tags

        assert parse_tags("") == []

    def test_none_returns_empty_list(self):
        from soundbot.store import parse_tags

        assert parse_tags(None) == []

    def test_skips_empty_elements(self):
        from soundbot.store import parse_tags

        # Trailing comma, double commas
        assert parse_tags("meme,,funny,") == ["funny", "meme"]

    def test_lowercases(self):
        from soundbot.store import parse_tags

        assert parse_tags("MEME,Funny") == ["funny", "meme"]

    def test_rejects_invalid_element(self):
        from soundbot.store import parse_tags

        with pytest.raises(ValueError, match="invalid"):
            parse_tags("meme,bad tag!")

    def test_rejects_too_long_element(self):
        from soundbot.store import parse_tags

        with pytest.raises(ValueError, match="invalid"):
            parse_tags("meme," + "a" * 33)
