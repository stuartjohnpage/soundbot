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
        assert data["version"] == 1
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
