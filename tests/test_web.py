"""Tests for the web admin panel (issue #1).

FastAPI TestClient against a real SoundStore on tmp_path — no mocked
store. The app factory takes every dependency explicitly (store, cache,
token, sounds_dir, limits) so tests never monkeypatch config.
"""
import asyncio
import signal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from soundbot.pcm_cache import PCMCache
from soundbot.store import SoundStore
from soundbot.web import (
    TOKEN_HEADER,
    build_web_server,
    create_web_app,
    maybe_create_web_app,
    serve_web_app,
)
from tests.helpers import make_wav, skip_no_ffmpeg

TOKEN = "test-secret-token"

_skip_no_ffmpeg = skip_no_ffmpeg


def _make_store(tmp_path) -> tuple[SoundStore, Path]:
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    store = SoundStore(
        metadata_path=tmp_path / "sounds.json",
        sounds_dir=sounds_dir,
    )
    return store, sounds_dir


def _make_client(
    tmp_path, pcm_cache: PCMCache | None = None, **app_kwargs
) -> tuple[TestClient, SoundStore, Path]:
    store, sounds_dir = _make_store(tmp_path)
    app = create_web_app(
        store,
        pcm_cache or PCMCache(),
        token=TOKEN,
        sounds_dir=sounds_dir,
        max_duration=6.4,
        target_lufs=-16.0,
        **app_kwargs,
    )
    client = TestClient(app)
    client.headers[TOKEN_HEADER] = TOKEN
    return client, store, sounds_dir


def _add_sound(store: SoundStore, sounds_dir: Path, name: str, **kwargs) -> Path:
    path = sounds_dir / f"{name}.mp3"
    path.write_bytes(f"bytes-of-{name}".encode())
    store.add(name, path, **kwargs)
    return path


class TestAuth:
    def test_empty_token_refuses_to_build_app(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)
        with pytest.raises(ValueError, match="token"):
            create_web_app(
                store,
                PCMCache(),
                token="",
                sounds_dir=sounds_dir,
                max_duration=6.4,
                target_lufs=-16.0,
            )

    def test_maybe_create_returns_none_when_token_unset(self, tmp_path):
        """Safe default: no WEB_TOKEN -> the panel does not exist at all."""
        store, sounds_dir = _make_store(tmp_path)
        for token in ("", None):
            app = maybe_create_web_app(
                store,
                PCMCache(),
                token=token,
                sounds_dir=sounds_dir,
                max_duration=6.4,
                target_lufs=-16.0,
            )
            assert app is None

    def test_maybe_create_builds_app_when_token_set(self, tmp_path):
        store, sounds_dir = _make_store(tmp_path)
        app = maybe_create_web_app(
            store,
            PCMCache(),
            token=TOKEN,
            sounds_dir=sounds_dir,
            max_duration=6.4,
            target_lufs=-16.0,
        )
        assert app is not None

    def test_missing_token_is_401(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        del client.headers[TOKEN_HEADER]
        assert client.get("/api/sounds").status_code == 401

    def test_wrong_token_is_401(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        client.headers[TOKEN_HEADER] = "wrong-token"
        assert client.get("/api/sounds").status_code == 401

    def test_non_ascii_token_is_401_not_500(self, tmp_path):
        """secrets.compare_digest(str, str) raises TypeError on
        non-ASCII input; starlette decodes header bytes as latin-1, so a
        raw client can reach the comparison with 'café' and turn an
        unauthenticated request into a 500."""
        client, _, _ = _make_client(tmp_path)
        del client.headers[TOKEN_HEADER]

        resp = client.get(
            "/api/sounds", headers=[(b"x-auth-token", b"caf\xe9")]
        )

        assert resp.status_code == 401

    def test_correct_token_is_accepted(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        assert client.get("/api/sounds").status_code == 200

    def test_bearer_authorization_header_also_accepted(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        del client.headers[TOKEN_HEADER]
        resp = client.get(
            "/api/sounds", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200


class TestListSounds:
    def test_lists_sounds_with_metadata(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn", category="memes")
        store.add_tag("airhorn", "loud")
        store.increment_play_count("airhorn")

        resp = client.get("/api/sounds")

        assert resp.status_code == 200
        sounds = resp.json()
        assert len(sounds) == 1
        entry = sounds[0]
        assert entry["name"] == "airhorn"
        assert entry["category"] == "memes"
        assert entry["tags"] == ["loud"]
        assert entry["play_count"] == 1
        assert entry["filename"] == "airhorn.mp3"

    def test_sounds_sorted_by_name(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "zebra")
        _add_sound(store, sounds_dir, "alpha")

        names = [s["name"] for s in client.get("/api/sounds").json()]
        assert names == ["alpha", "zebra"]


class TestSearchAndFilter:
    def _populate(self, store, sounds_dir):
        _add_sound(store, sounds_dir, "airhorn", category="memes")
        _add_sound(store, sounds_dir, "sad-horn", category="memes")
        _add_sound(store, sounds_dir, "victory", category="games")
        store.add_tag("airhorn", "loud")
        store.add_tag("victory", "loud")
        store.add_tag("victory", "win")

    def test_search_query_matches_prefix_first_then_substring(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        self._populate(store, sounds_dir)

        names = [s["name"] for s in client.get("/api/sounds", params={"q": "air"}).json()]
        assert names == ["airhorn"]
        names = [s["name"] for s in client.get("/api/sounds", params={"q": "horn"}).json()]
        assert names == ["airhorn", "sad-horn"]

    def test_tag_filter(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        self._populate(store, sounds_dir)

        names = [s["name"] for s in client.get("/api/sounds", params={"tag": "loud"}).json()]
        assert names == ["airhorn", "victory"]

    def test_category_filter(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        self._populate(store, sounds_dir)

        names = [s["name"] for s in client.get("/api/sounds", params={"category": "games"}).json()]
        assert names == ["victory"]

    def test_search_combines_with_tag_filter(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        self._populate(store, sounds_dir)

        names = [
            s["name"]
            for s in client.get(
                "/api/sounds", params={"q": "horn", "tag": "loud"}
            ).json()
        ]
        assert names == ["airhorn"]


class TestTagAndCategoryListing:
    def test_global_tags_with_counts(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "a")
        _add_sound(store, sounds_dir, "b")
        store.add_tag("a", "meme")
        store.add_tag("b", "meme")
        store.add_tag("b", "win")

        resp = client.get("/api/tags")

        assert resp.status_code == 200
        assert resp.json() == [
            {"tag": "meme", "count": 2},
            {"tag": "win", "count": 1},
        ]

    def test_categories(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "a", category="memes")
        _add_sound(store, sounds_dir, "b", category="games")
        _add_sound(store, sounds_dir, "c")

        resp = client.get("/api/categories")

        assert resp.status_code == 200
        assert resp.json() == ["games", "memes"]

    def test_both_require_auth(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        del client.headers[TOKEN_HEADER]
        assert client.get("/api/tags").status_code == 401
        assert client.get("/api/categories").status_code == 401


class TestDelete:
    def test_delete_removes_entry_file_cache_and_persists(self, tmp_path):
        cache = PCMCache(decoder=lambda p: b"pcm")
        client, store, sounds_dir = _make_client(tmp_path, pcm_cache=cache)
        path = _add_sound(store, sounds_dir, "airhorn")
        cache.get(str(path))
        assert str(path) in cache

        resp = client.delete("/api/sounds/airhorn")

        assert resp.status_code == 200
        assert store.get("airhorn") is None
        assert not path.exists()
        # Stale PCM dropped so a re-add under the same filename can't
        # serve the old bytes (mirrors /removesound).
        assert str(path) not in cache
        # Persisted: a fresh store built from the same metadata file
        # must not resurrect the sound.
        reloaded = SoundStore(
            metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir
        )
        assert reloaded.get("airhorn") is None

    def test_delete_unknown_sound_is_404(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        assert client.delete("/api/sounds/ghost").status_code == 404

    def test_delete_requires_auth(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")
        del client.headers[TOKEN_HEADER]

        assert client.delete("/api/sounds/airhorn").status_code == 401
        assert store.get("airhorn") is not None


class TestRename:
    def test_rename_updates_store_and_persists(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        path = _add_sound(store, sounds_dir, "airhorn")

        resp = client.post(
            "/api/sounds/airhorn/rename", json={"new_name": "klaxon"}
        )

        assert resp.status_code == 200
        assert store.get("airhorn") is None
        assert store.get("klaxon")["file"] == str(path)
        # File stays where it is — the cache is keyed by path, so the
        # same bytes are still correct under the new name.
        assert path.exists()
        reloaded = SoundStore(
            metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir
        )
        assert reloaded.get("klaxon") is not None

    def test_rename_unknown_sound_is_404(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        resp = client.post("/api/sounds/ghost/rename", json={"new_name": "x"})
        assert resp.status_code == 404

    def test_rename_to_existing_name_is_409(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")
        _add_sound(store, sounds_dir, "klaxon")

        resp = client.post(
            "/api/sounds/airhorn/rename", json={"new_name": "klaxon"}
        )

        assert resp.status_code == 409
        assert store.get("airhorn") is not None

    def test_rename_to_invalid_name_is_400(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")

        resp = client.post(
            "/api/sounds/airhorn/rename", json={"new_name": "bad name!"}
        )

        assert resp.status_code == 400
        assert store.get("airhorn") is not None


class TestEditTags:
    def test_put_replaces_tag_set_and_persists(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")
        store.add_tag("airhorn", "old-tag")
        store.add_tag("airhorn", "keep")

        resp = client.put(
            "/api/sounds/airhorn/tags", json={"tags": ["keep", "new-tag"]}
        )

        assert resp.status_code == 200
        assert set(store.get("airhorn")["tags"]) == {"keep", "new-tag"}
        reloaded = SoundStore(
            metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir
        )
        assert set(reloaded.get("airhorn")["tags"]) == {"keep", "new-tag"}

    def test_tags_are_canonicalized_lowercase(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")

        resp = client.put("/api/sounds/airhorn/tags", json={"tags": ["MEME"]})

        assert resp.status_code == 200
        assert store.get("airhorn")["tags"] == ["meme"]

    def test_invalid_tag_is_400_and_store_untouched(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")
        store.add_tag("airhorn", "original")

        resp = client.put(
            "/api/sounds/airhorn/tags", json={"tags": ["ok", "bad tag!"]}
        )

        assert resp.status_code == 400
        # All-or-nothing: the valid element must not have been applied.
        assert store.get("airhorn")["tags"] == ["original"]

    def test_unknown_sound_is_404(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        resp = client.put("/api/sounds/ghost/tags", json={"tags": ["x"]})
        assert resp.status_code == 404


def _upload(client, *, name, filename, content, data=None):
    return client.post(
        "/api/sounds",
        data={"name": name, **(data or {})},
        files={"file": (filename, content, "application/octet-stream")},
    )


class TestUpload:
    @_skip_no_ffmpeg
    def test_valid_wav_is_registered_normalized_and_persisted(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        wav = make_wav(tmp_path / "source.wav")

        resp = _upload(
            client,
            name="horn",
            filename="horn.wav",
            content=wav.read_bytes(),
            data={"category": "memes", "tags": "meme,loud"},
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "horn"
        # A loud tone against the -16 LUFS target must be attenuated.
        assert body["gain_db"] is not None and body["gain_db"] < 0
        entry = store.get("horn")
        assert entry is not None
        assert entry["category"] == "memes"
        assert set(entry["tags"]) == {"loud", "meme"}
        assert Path(entry["file"]) == sounds_dir / "horn.wav"
        assert (sounds_dir / "horn.wav").exists()
        reloaded = SoundStore(
            metadata_path=tmp_path / "sounds.json", sounds_dir=sounds_dir
        )
        assert reloaded.get("horn") is not None

    def test_duplicate_name_is_409_with_tag_hint(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "horn")
        store.add_tag("horn", "somewhere")

        resp = _upload(client, name="horn", filename="other.wav", content=b"x")

        assert resp.status_code == 409
        # The collision message must explain where the existing sound is
        # visible (tag-hidden collisions confused users — see PR #22).
        assert "somewhere" in resp.json()["detail"]
        # Nothing written
        assert not (sounds_dir / "other.wav").exists()

    def test_filename_owned_by_other_entry_is_409(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        path = _add_sound(store, sounds_dir, "first")  # first.mp3

        resp = _upload(
            client, name="second", filename="first.mp3", content=b"new bytes"
        )

        assert resp.status_code == 409
        assert "first" in resp.json()["detail"]
        # The existing entry's bytes were not clobbered.
        assert path.read_bytes() == b"bytes-of-first"
        assert store.get("second") is None

    @_skip_no_ffmpeg
    def test_path_traversal_filename_is_neutralized(self, tmp_path):
        """A '../'-laden filename must not escape sounds_dir. Same
        defense as /addsound: strip to the basename and store inside
        sounds_dir — the upload succeeds, the traversal doesn't."""
        client, store, sounds_dir = _make_client(tmp_path)
        wav = make_wav(tmp_path / "source.wav")

        resp = _upload(
            client,
            name="escapee",
            filename="../../escapee.wav",
            content=wav.read_bytes(),
        )

        assert resp.status_code == 201
        # Landed inside sounds_dir under the basename...
        assert (sounds_dir / "escapee.wav").exists()
        assert Path(store.get("escapee")["file"]) == sounds_dir / "escapee.wav"
        # ...and nowhere above it.
        assert not (tmp_path / "escapee.wav").exists()

    def test_invalid_name_rejected_before_write(self, tmp_path):
        """Without a pre-check, an invalid name sails through the file
        write and the whole ffmpeg pipeline before store.add finally
        raises — contradicting the route's fail-before-I/O contract."""
        client, store, sounds_dir = _make_client(tmp_path)

        resp = _upload(
            client, name="bad name!", filename="horn.wav", content=b"x"
        )

        assert resp.status_code == 400
        # The *name* validation message — not the pipeline's "Cannot
        # read audio file" that fires only after the write.
        assert resp.json()["detail"].startswith("Sound name")
        assert not (sounds_dir / "horn.wav").exists()

    def test_invalid_tags_rejected_before_write(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)

        resp = _upload(
            client,
            name="horn",
            filename="horn.wav",
            content=b"x",
            data={"tags": "bad tag!"},
        )

        assert resp.status_code == 400
        assert not (sounds_dir / "horn.wav").exists()

    @_skip_no_ffmpeg
    def test_unreadable_audio_is_400_and_cleaned_up(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)

        resp = _upload(
            client, name="junk", filename="junk.mp3", content=b"not audio"
        )

        assert resp.status_code == 400
        assert store.get("junk") is None
        assert not (sounds_dir / "junk.mp3").exists()

    @_skip_no_ffmpeg
    def test_upload_invalidates_stale_cache_for_destination(self, tmp_path):
        """A deleted sound's PCM can still sit in the cache under this
        path — re-uploading the same filename must not serve old bytes."""
        cache = PCMCache(decoder=lambda p: b"stale")
        client, store, sounds_dir = _make_client(tmp_path, pcm_cache=cache)
        dest_key = str(sounds_dir / "horn.wav")
        cache.get(dest_key)
        assert dest_key in cache

        wav = make_wav(tmp_path / "source.wav")
        resp = _upload(
            client, name="horn", filename="horn.wav", content=wav.read_bytes()
        )

        assert resp.status_code == 201
        assert dest_key not in cache

    def test_unexpected_error_does_not_orphan_partial_file(self, tmp_path, monkeypatch):
        """A non-ValueError failure (disk full, transient AV lock on
        Windows) must not leave a partial file in sounds_dir, where the
        next scan_folder() would register it as a broken sound."""

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("soundbot.web.process_upload", explode)
        client, store, sounds_dir = _make_client(tmp_path)

        with pytest.raises(OSError):
            _upload(client, name="doomed", filename="doomed.wav", content=b"x")

        assert store.get("doomed") is None
        assert not (sounds_dir / "doomed.wav").exists()

    def test_oversized_upload_is_413_and_leaves_no_file(self, tmp_path):
        """Discord caps attachment sizes; the web route needs its own
        cap or an authed user could fill the disk before validation."""
        client, store, sounds_dir = _make_client(tmp_path, max_upload_bytes=1024)

        resp = _upload(
            client, name="big", filename="big.wav", content=b"\x00" * 2048
        )

        assert resp.status_code == 413
        assert store.get("big") is None
        assert not (sounds_dir / "big.wav").exists()

    def test_upload_requires_auth(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        del client.headers[TOKEN_HEADER]

        resp = _upload(client, name="horn", filename="horn.wav", content=b"x")

        assert resp.status_code == 401
        assert not (sounds_dir / "horn.wav").exists()


class TestAudioPreview:
    def test_serves_file_bytes_with_audio_content_type(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")  # writes bytes-of-airhorn

        resp = client.get("/api/sounds/airhorn/audio")

        assert resp.status_code == 200
        assert resp.content == b"bytes-of-airhorn"
        assert resp.headers["content-type"].startswith("audio/")

    def test_unknown_sound_is_404(self, tmp_path):
        client, _, _ = _make_client(tmp_path)
        assert client.get("/api/sounds/ghost/audio").status_code == 404

    def test_entry_with_missing_file_is_404(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        path = _add_sound(store, sounds_dir, "dangling")
        path.unlink()

        assert client.get("/api/sounds/dangling/audio").status_code == 404

    def test_requires_auth(self, tmp_path):
        client, store, sounds_dir = _make_client(tmp_path)
        _add_sound(store, sounds_dir, "airhorn")
        del client.headers[TOKEN_HEADER]

        assert client.get("/api/sounds/airhorn/audio").status_code == 401


class TestEmbeddedServer:
    def test_serves_on_existing_loop_without_touching_signal_handlers(self, tmp_path):
        """The panel runs as a task on the bot's event loop. uvicorn's
        default behavior is to install its own SIGINT/SIGTERM handlers,
        which would swallow Ctrl+C / docker stop and leave the bot
        running headless — the embedded server must not do that."""
        store, sounds_dir = _make_store(tmp_path)
        app = create_web_app(
            store,
            PCMCache(),
            token=TOKEN,
            sounds_dir=sounds_dir,
            max_duration=6.4,
            target_lufs=-16.0,
        )
        async def scenario():
            # Baseline inside the running loop: asyncio.run's Runner
            # installs its own SIGINT handler, which is fine — the
            # invariant is that *the server* doesn't change anything.
            sigint_before = signal.getsignal(signal.SIGINT)
            sigterm_before = signal.getsignal(signal.SIGTERM)
            server = build_web_server(app, host="127.0.0.1", port=0)
            task = asyncio.create_task(server.serve())
            try:
                while not server.started:
                    await asyncio.sleep(0.01)
                assert signal.getsignal(signal.SIGINT) is sigint_before
                assert signal.getsignal(signal.SIGTERM) is sigterm_before
                port = server.servers[0].sockets[0].getsockname()[1]
                async with httpx.AsyncClient() as client:
                    return await client.get(
                        f"http://127.0.0.1:{port}/api/sounds",
                        headers={TOKEN_HEADER: TOKEN},
                    )
            finally:
                server.should_exit = True
                await task

        resp = asyncio.run(scenario())
        assert resp.status_code == 200
        assert resp.json() == []


class TestServeStartupFailure:
    def test_port_conflict_does_not_kill_the_event_loop(self, tmp_path):
        """uvicorn's Server.startup() calls sys.exit(3) when the bind
        fails. Raised inside a bot-loop task, that SystemExit escapes
        asyncio.run and kills the whole Discord bot — a stale process on
        WEB_PORT must degrade to a logged error, not a crash loop."""
        import socket

        store, sounds_dir = _make_store(tmp_path)
        app = create_web_app(
            store,
            PCMCache(),
            token=TOKEN,
            sounds_dir=sounds_dir,
            max_duration=6.4,
            target_lufs=-16.0,
        )

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:

            async def scenario():
                server = build_web_server(app, host="127.0.0.1", port=port)
                task = asyncio.create_task(serve_web_app(server))
                await task  # must complete without raising
                return "loop survived"

            assert asyncio.run(scenario()) == "loop survived"
        finally:
            blocker.close()


class TestIndexPage:
    def test_index_serves_html_without_auth(self, tmp_path):
        """The page is a static shell with no library data — auth happens
        client-side, so the login screen must load without a token."""
        client, _, _ = _make_client(tmp_path)
        del client.headers[TOKEN_HEADER]

        resp = client.get("/")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "Soundbot" in resp.text
