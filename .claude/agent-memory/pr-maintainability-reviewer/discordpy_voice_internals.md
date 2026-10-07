---
name: discordpy-voice-internals
description: Verified discord.py 2.7.1 voice connect/disconnect await ordering — needed to judge voice race-condition claims in soundbot/bot.py reviews
type: project
---

Verified against the installed discord.py (2.7.1, Python 3.13) during the PR #35
auto-join review. These orderings decide whether voice race-condition claims in
`soundbot/bot.py` are real, and they are NOT derivable from reading soundbot:

- `discord.abc.Connectable.connect()` calls `state._add_voice_client(...)`
  **synchronously, before** `await voice.connect(...)`. So `guild.voice_client`
  is non-None for the whole handshake — it never "stays None until connect()
  finishes". Any guard that checks `guild.voice_client is not None` therefore
  already covers the concurrent-arrival window; extra in-flight sets are
  defence-in-depth, not the thing that closes the race.
- `guild.voice_client` is `self._state._get_voice_client(self.id)` — a plain
  registry lookup, true even mid-handshake (so `is not None` and
  `is_connected()` disagree during connect; most of bot.py uses
  `is_connected()`, auto-join deliberately does not).
- `VoiceClient.disconnect()` = `stop()` → `await _connection.disconnect(wait=True)`
  → `cleanup()`. `cleanup()` (which calls `_remove_voice_client`) runs **after**
  the disconnect await, and the `wait=True` path awaits the bot's own
  VOICE_STATE_UPDATE first. So `guild.voice_client` stays non-None for the
  entire `/leave` teardown, and nothing can run between it going None and the
  statement after `await vc.disconnect()` returns.
- `connect()` raises `asyncio.TimeoutError`, `discord.ClientException` and
  `discord.opus.OpusNotLoaded`; `OpusNotLoaded` and `ClientException` are both
  `DiscordException` subclasses. On 3.11+ `asyncio.TimeoutError is TimeoutError`
  and `TimeoutError` is an `OSError` subclass, so
  `except (DiscordException, asyncio.TimeoutError, OSError)` has a redundant
  middle term.

**Why:** PR #35 shipped a comment and a test asserting `guild.voice_client`
stays None during the handshake, which is false — the kind of claim that
survives for years and misleads the next maintainer.

**How to apply:** Re-verify with `inspect.getsource` before asserting any voice
ordering in a review; discord.py may change this between versions.
