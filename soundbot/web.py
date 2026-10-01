"""Web admin panel for sound management (issue #1).

A FastAPI app that runs *inside the bot process*, sharing the bot's
SoundStore and PCMCache instances. This is deliberate: sounds.json has
no cross-process locking, so a separate web process could corrupt it.
The app factory takes every dependency explicitly so tests can build it
against a tmp_path store without touching config.

Auth is a static token (WEB_TOKEN env var). If the token is unset the
panel does not start at all — `maybe_create_web_app` returns None and
bot startup skips the web server entirely.

Concurrency: every route handler here is a plain `def`, NOT `async def`.
FastAPI runs sync handlers in a worker threadpool, which is exactly what
we want — the ffmpeg subprocesses and file I/O in the upload/delete
paths never block the bot's event loop (same effect as the
asyncio.to_thread wrapping in bot.py). Don't convert these to async
without re-adding to_thread around the blocking work. Cross-thread
store.save() calls are serialized by the store's internal lock.
"""
import contextlib
import logging
import secrets
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .ingest import (
    RejectionKind,
    UploadRejected,
    precheck_upload,
    process_upload,
    reserve_upload_path,
)
from .pcm_cache import PCMCache
from .store import SoundStore, parse_tags

logger = logging.getLogger("soundbot")

TOKEN_HEADER = "X-Auth-Token"

# Discord caps attachment sizes for /addsound; the web route needs its
# own ceiling or an authed user could fill the disk before duration
# validation ever runs. Generous: a 6.4s clip is well under 1MB, and
# even a video upload (audio gets extracted) fits comfortably.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Explicit map for the preview endpoint: mimetypes guesses nothing (or
# non-audio types) for .opus/.webm/.m4a on some platforms, and a wrong
# content type stops <audio> elements from playing the clip.
_AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


class RenameBody(BaseModel):
    new_name: str


class TagsBody(BaseModel):
    tags: list[str]


# HTTP status for each ingest.UploadRejected kind.
_REJECTION_STATUS: dict[RejectionKind, int] = {
    "invalid": 400,
    "duplicate": 409,
    "conflict": 409,
}


def _auth_dependency(token: str):
    """Build a FastAPI dependency that validates the static token.

    Accepts the token via `X-Auth-Token: <token>` or
    `Authorization: Bearer <token>`. Comparison is constant-time
    (secrets.compare_digest) so the token can't be guessed
    byte-by-byte via response timing.
    """

    def require_token(request: Request) -> None:
        supplied = request.headers.get(TOKEN_HEADER)
        if supplied is None:
            authorization = request.headers.get("Authorization", "")
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
        # Compare as bytes: compare_digest(str, str) raises TypeError on
        # non-ASCII input, and starlette decodes header bytes as latin-1
        # — a raw client could turn an unauthenticated request into a 500.
        if supplied is None or not secrets.compare_digest(
            supplied.encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    return require_token


def create_web_app(
    store: SoundStore,
    pcm_cache: PCMCache,
    *,
    token: str,
    sounds_dir: Path,
    max_duration: float,
    target_lufs: float,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> FastAPI:
    """Build the admin panel app. Raises ValueError if token is empty —
    an unauthenticated panel must never exist."""
    if not token:
        raise ValueError("web panel requires a non-empty auth token")

    require_token = _auth_dependency(token)
    app = FastAPI(title="Soundbot Admin", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        # Deliberately unauthenticated: the page is a static shell with
        # no library data. It prompts for the token and sends it with
        # every /api request; every data-bearing route requires auth.
        return FileResponse(
            Path(__file__).parent / "static" / "index.html",
            media_type="text/html",
        )

    def serialize(name: str, entry: dict) -> dict:
        """Shape a store entry for the API. The on-disk path stays
        server-side; clients only ever see the basename."""
        return {
            "name": name,
            "category": entry.get("category"),
            "tags": entry.get("tags", []),
            "play_count": entry.get("play_count", 0),
            "uploaded_by": entry.get("uploaded_by"),
            "uploaded_at": entry.get("uploaded_at"),
            "filename": Path(entry["file"]).name,
        }

    @app.get("/api/sounds", dependencies=[Depends(require_token)])
    def list_sounds(
        q: str | None = None,
        tag: str | None = None,
        category: str | None = None,
    ) -> list[dict]:
        if q:
            # store.search orders prefix matches before substring matches;
            # apply tag/category as post-filters to preserve that order.
            results = [
                (n, e)
                for n, e in store.search(q)
                if (tag is None or tag in e.get("tags", []))
                and (category is None or e.get("category") == category)
            ]
        else:
            results = store.list_sounds(category=category, tag=tag)
        return [serialize(n, e) for n, e in results]

    @app.post(
        "/api/sounds",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    def upload_sound(
        file: UploadFile,
        name: str = Form(...),
        category: str | None = Form(None),
        tags: str | None = Form(None),
    ) -> dict:
        # The pre-save checks /addsound runs (shared, so they can't
        # drift), then an atomic claim on the destination file so a
        # concurrent upload with the same filename can't write it too.
        try:
            dest, tag_list = precheck_upload(
                store,
                sounds_dir,
                name=name,
                tags=tags,
                filename=file.filename,
                markdown=False,
            )
            reserve_upload_path(dest)
        except UploadRejected as exc:
            raise HTTPException(
                status_code=_REJECTION_STATUS[exc.kind], detail=str(exc)
            )
        try:
            written = 0
            with dest.open("wb") as out:
                while chunk := file.file.read(64 * 1024):
                    written += len(chunk)
                    if written > max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Upload exceeds "
                                f"{max_upload_bytes // (1024 * 1024)}MB limit"
                            ),
                        )
                    out.write(chunk)
        except BaseException:
            # A failed write (413 above, disk full mid-write) must not
            # orphan a partial file in sounds_dir, where it would block
            # the filename and register as a broken sound on the next
            # scan_folder(). Safe to unlink: reserve_upload_path created
            # this file, so nothing else owns it.
            dest.unlink(missing_ok=True)
            raise
        try:
            # Shared ingest pipeline — the exact code path /addsound runs
            # (video-extract, validate, normalize, cache-invalidate,
            # register). It cleans up its own file on any failure.
            final_dest, gain, trimmed_from = process_upload(
                dest,
                store=store,
                pcm_cache=pcm_cache,
                name=name,
                category=category or None,
                tags=tag_list,
                uploaded_by="web",
                max_duration=max_duration,
                target_lufs=target_lufs,
            )
        except UploadRejected as exc:
            raise HTTPException(
                status_code=_REJECTION_STATUS[exc.kind], detail=str(exc)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.save()
        return {
            "name": name.lower(),
            "filename": final_dest.name,
            "gain_db": gain,
            "trimmed_from_seconds": trimmed_from,
            "tags": store.list_tags(name),
        }

    @app.delete("/api/sounds/{name}", dependencies=[Depends(require_token)])
    def delete_sound(name: str) -> dict:
        entry = store.get(name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Sound '{name}' not found")
        store.remove(name)
        store.save()
        # Drop any cached PCM so a re-add under the same filename doesn't
        # serve stale bytes (mirrors /removesound).
        pcm_cache.invalidate(entry["file"])
        return {"deleted": name.lower()}

    @app.post("/api/sounds/{name}/rename", dependencies=[Depends(require_token)])
    def rename_sound(name: str, body: RenameBody) -> dict:
        if store.get(name) is None:
            raise HTTPException(status_code=404, detail=f"Sound '{name}' not found")
        if store.get(body.new_name) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Sound '{body.new_name}' already exists",
            )
        try:
            store.rename(name, body.new_name)
        except ValueError as exc:
            # Invalid new name. str(exc), not exc.args[0]: ValueError
            # renders cleanly (only KeyError has the repr-quoting trap)
            # and an argless ValueError would make args[0] an IndexError.
            raise HTTPException(status_code=400, detail=str(exc))
        store.save()
        # No pcm_cache interaction needed: rename swaps the metadata key
        # but the file (the cache key) stays at the same path.
        return {"name": body.new_name.lower()}

    @app.put("/api/sounds/{name}/tags", dependencies=[Depends(require_token)])
    def replace_tags(name: str, body: TagsBody) -> dict:
        if store.get(name) is None:
            raise HTTPException(status_code=404, detail=f"Sound '{name}' not found")
        # Tags can't contain commas, so round-tripping through parse_tags
        # reuses the exact validation/canonicalization /addsound applies —
        # and it raises before any mutation, keeping this all-or-nothing.
        try:
            new_tags = parse_tags(",".join(body.tags))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        for tag in store.list_tags(name):
            if tag not in new_tags:
                store.remove_tag(name, tag)
        for tag in new_tags:
            store.add_tag(name, tag)
        store.save()
        return {"name": name.lower(), "tags": store.list_tags(name)}

    @app.get("/api/sounds/{name}/audio", dependencies=[Depends(require_token)])
    def preview_audio(name: str) -> FileResponse:
        entry = store.get(name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Sound '{name}' not found")
        path = Path(entry["file"])
        if not path.exists():
            raise HTTPException(
                status_code=404, detail=f"File for '{name}' is missing on disk"
            )
        media_type = _AUDIO_MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg")
        return FileResponse(path, media_type=media_type)

    @app.get("/api/tags", dependencies=[Depends(require_token)])
    def list_tags() -> list[dict]:
        return [{"tag": t, "count": c} for t, c in store.global_tags()]

    @app.get("/api/categories", dependencies=[Depends(require_token)])
    def list_categories() -> list[str]:
        return store.categories()

    return app


class _EmbeddedServer(uvicorn.Server):
    """uvicorn Server that never touches process signal handlers.

    The stock Server installs its own SIGINT/SIGTERM handlers when it
    runs on the main thread. Embedded in the bot's event loop, that
    would swallow Ctrl+C / docker stop: uvicorn would shut itself down
    and leave the Discord bot running headless. The bot owns process
    signals; the panel just serves until the process exits.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def build_web_server(app: FastAPI, *, host: str, port: int) -> uvicorn.Server:
    """Build the uvicorn server for the panel. Start it with
    `asyncio.create_task(serve_web_app(server))` on the bot's running loop."""
    return _EmbeddedServer(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )


async def serve_web_app(server: uvicorn.Server) -> None:
    """Run the panel server; never let a startup failure kill the bot.

    uvicorn's Server.startup() calls sys.exit(STARTUP_FAILURE) when the
    bind fails (port in use, bad host). Raised inside a task on the
    bot's event loop, that SystemExit escapes asyncio.run and takes the
    whole Discord bot down — under docker `restart: unless-stopped`
    that's a crash loop. The panel is optional; the bot is not.
    """
    host, port = server.config.host, server.config.port
    try:
        await server.serve()
    except SystemExit:
        logger.error(
            "web admin panel failed to start on %s:%d (port already in "
            "use?); bot continues without the panel",
            host,
            port,
        )


def maybe_create_web_app(
    store: SoundStore,
    pcm_cache: PCMCache,
    *,
    token: str | None,
    sounds_dir: Path,
    max_duration: float,
    target_lufs: float,
) -> FastAPI | None:
    """Return the app, or None when no token is configured (safe default:
    the panel simply does not start)."""
    if not token:
        return None
    return create_web_app(
        store,
        pcm_cache,
        token=token,
        sounds_dir=sounds_dir,
        max_duration=max_duration,
        target_lufs=target_lufs,
    )
