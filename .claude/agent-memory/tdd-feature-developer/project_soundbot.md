---
name: Soundbot project structure and testing
description: Discord soundbot project - testing framework, module layout, and TDD patterns used
type: project
---

Discord soundbot built in Python with discord.py. Deployed via Docker with FFmpeg.

**Testing:** pytest, tests in `tests/` directory. Uses `tmp_path` fixtures for store tests. Audio tests skip when FFmpeg unavailable. Mixer tests use `FakeSource` objects with known PCM byte patterns.

**Modules built:** store.py (26 tests), audio.py (4 tests, skip without ffprobe), mixer.py (5 tests), pagination.py (4 tests), bot.py (thin integration layer, untested by design).

**Why:** Stuart is building a personal Discord soundbot to replace Discord's built-in soundboard limitations. All core logic is tested; the bot layer delegates to tested modules.

**How to apply:** When extending the soundbot, follow the same TDD pattern. New features should go in standalone modules tested independently, keeping bot.py as a thin adapter.
