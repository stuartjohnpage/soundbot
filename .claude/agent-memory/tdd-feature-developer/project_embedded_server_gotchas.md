---
name: embedded-asyncio-server-gotchas
description: Traps when embedding uvicorn/web servers in the bot's event loop and testing signal/thread behavior
type: project
---

Learned building the web admin panel (issue #1, soundbot/web.py):

1. **Stock `uvicorn.Server.serve()` installs SIGINT/SIGTERM handlers when run on the main thread** — embedded in the bot's loop it swallows Ctrl+C/docker stop: uvicorn shuts down, the bot keeps running headless. Fix: subclass and no-op `capture_signals()` (see `_EmbeddedServer` in web.py). Regression-pinned in tests/test_web.py TestEmbeddedServer.

2. **`asyncio.run()` (Python 3.12+ Runner) installs its own SIGINT handler while the loop runs.** Any test asserting "X didn't change signal handlers" must capture the baseline INSIDE the running loop, not before `asyncio.run` — otherwise the assert compares against the pre-loop default handler and fails for the wrong reason.

3. **FastAPI sync (`def`) route handlers run in a threadpool** — that's the async-hygiene mechanism for the panel (ffmpeg/file work off the loop, equivalent to bot.py's asyncio.to_thread). Consequence: the web panel is the first cross-thread writer to sounds.json, which is why SoundStore.save() has a threading.Lock. Don't convert handlers to `async def` without re-adding to_thread.

4. **Windows curl/Bash tool: cwd resets between Bash calls** — e2e drives that upload files must use absolute paths or curl returns exit code with http_code 000 (looks like a server bug, is a harness bug). Stray output files (`junk.mp3`) can land in the repo root; check `git status` before committing.

**Why:** items 1-2 produced real failures during development; item 3 is a deliberate design invariant a reviewer/maintainer could easily undo.

**How to apply:** whenever adding another in-process server or signal-sensitive startup code to the bot, reuse `_EmbeddedServer`; when writing tests that assert on process signal state, baseline inside the loop.
