---
name: soundbot project stack
description: Core tech stack and shape of the soundbot Discord bot for targeting reviews
type: project
---

soundbot is a discord.py-based Discord soundboard bot. Playback goes through a custom `MixerSource` (`soundbot/mixer.py`) that subclasses `discord.AudioSource` and emits 20ms/48kHz/stereo/s16le frames (FRAME_SIZE=3840, SAMPLES_PER_FRAME=1920). Audio is decoded via ffmpeg subprocesses.

As of PR #23 (~2026-07) there is a FastAPI web admin panel (`soundbot/web.py`) embedded in the bot's event loop via a custom uvicorn `_EmbeddedServer`, and a shared upload pipeline in `soundbot/ingest.py` (extracted from `/addsound`). Web route handlers are deliberately sync `def` (FastAPI threadpool) — this means store methods now run cross-thread; check thread-safety of any new shared state. Review gotcha verified during PR #23 review: uvicorn's `Server.startup()` calls `sys.exit()` on bind failure, and SystemExit raised inside an asyncio Task escapes `asyncio.run` and kills the whole process — any embedded-server or task-spawning code here needs a SystemExit guard.

`tests/test_bot.py` covers command handlers by calling `Soundboard.<cmd>.callback(cog, mock_interaction, ...)` directly with a real `SoundStore` on tmp_path and monkeypatched audio helpers — since PR #23 the patch targets live in `soundbot.ingest.*` (`soundbot.ingest.validate_sound`, `has_video_stream`, `normalize_loudness`), not `soundbot.bot.*`. Handler-level tests are the established convention — missing coverage for a new handler branch is a legitimate review finding. Web tests (`tests/test_web.py`) use FastAPI TestClient against a real store, no mocks.

**Why:** The `callback(...)` + mock-Interaction pattern is the project's accepted way to test cogs; real-store-on-tmp_path (never a mocked store) is the accepted way to test the web layer.

**How to apply:** When reviewing `soundbot/bot.py` or `soundbot/web.py` changes, expect handler/route tests in this style and flag branches without them. Any per-frame hot loop in `mixer.py` runs at 50Hz × 1920 samples = ~96K Python ops/sec, so perf notes there are load-bearing. Test gotcha the codebase itself documents: `MagicMock(name=...)` sets the mock's own name — use plain attribute assignment for `.name`.
