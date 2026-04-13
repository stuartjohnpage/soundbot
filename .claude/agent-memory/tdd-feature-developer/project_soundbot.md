---
name: Soundbot project structure and testing
description: Discord soundbot project - testing framework, module layout, and TDD patterns used
type: project
---

Discord soundbot built in Python with discord.py. Deployed via Docker with FFmpeg.

**Testing:** pytest, tests in `tests/` directory. Uses `tmp_path` fixtures for store tests. Audio tests skip when FFmpeg unavailable. Mixer tests use `FakeSource` objects with known PCM byte patterns. **No pytest-asyncio** in requirements-dev.txt — drive coroutines via `asyncio.run()` inside sync test bodies if you need to test async helpers.

**Modules built:** store.py (heavily tested), audio.py (skip without ffprobe), mixer.py, pagination.py, bot.py (thin integration layer — testable helpers can be hoisted to module-level functions like `run_migration_if_needed` to keep coverage where it matters).

**Why:** Stuart is building a personal Discord soundbot to replace Discord's built-in soundboard limitations. All core logic is tested; the bot layer delegates to tested modules.

**How to apply:** When extending the soundbot, follow the same TDD pattern. New features should go in standalone modules tested independently, keeping bot.py as a thin adapter.
