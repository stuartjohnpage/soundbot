---
name: Soundbot project structure and testing
description: Discord soundbot project - testing framework, module layout, and TDD patterns used
type: project
---

Discord soundbot built in Python with discord.py + FastAPI (web admin panel runs in-process with the bot, sharing SoundStore/PCMCache). Deployed via Docker with FFmpeg.

**Testing:** pytest, tests in `tests/` directory. Uses `tmp_path` fixtures for store tests. Audio/ffmpeg tests use `_skip_no_ffmpeg = pytest.mark.skipif(shutil.which("ffprobe") is None, ...)`. **No pytest-asyncio** — drive coroutines via `asyncio.run()` inside sync test bodies. Web tests use FastAPI TestClient (httpx in requirements-dev.txt) against a real SoundStore.

Useful fixture tricks proven here:
- Real audio: generate a 1s sine WAV with stdlib `wave` (loud tone ≈ -3 LUFS, so normalization measurably applies); real video via `ffmpeg -f lavfi testsrc + sine` (see tests/test_ingest.py `make_wav`/`make_mp4`).
- Concurrency bugs are reproducible: 4 threads x 200 `store.save()` calls deterministically collided on Windows before the save lock existed.
- `_setup_addsound` in test_bot.py monkeypatches `soundbot.ingest.*` (NOT soundbot.bot) since the upload pipeline was extracted to ingest.py.

**Modules:** store.py, audio.py, mixer.py, pagination.py, pcm_cache.py, ingest.py (shared /addsound + web upload pipeline), web.py (FastAPI panel), bot.py (thin integration layer — testable helpers hoisted to module-level functions).

**Why:** Stuart is building a personal Discord soundbot to replace Discord's built-in soundboard limitations. All core logic is tested; the bot layer delegates to tested modules.

**How to apply:** When extending the soundbot, follow the same TDD pattern. New features go in standalone modules tested independently, keeping bot.py as a thin adapter. Related gotchas: [[discord.py and soundbot startup-sequence gotchas]], [[embedded-asyncio-server-gotchas]].
