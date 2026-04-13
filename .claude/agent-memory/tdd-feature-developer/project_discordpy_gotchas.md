---
name: discord.py and soundbot startup-sequence gotchas
description: discord.py Namespace/KeyError quirks plus the bot.setup_hook vs on_ready ordering trap that ate a migration
type: project
---

Three non-obvious behaviors that bit me when extending soundbot:

1. **`interaction.namespace` returns `None` for missing options, not `AttributeError`.** Per the docstring: "If an attribute is not found, then None is returned rather than an attribute error." So `getattr(interaction.namespace, "tag", None)` is the right idiom — `try/except AttributeError` blocks are dead code.

2. **`KeyError.__str__` wraps the message in quotes.** When you `raise KeyError("Tag 'X' not present")` and then `await send_message(str(exc))`, the user sees literal `'Tag 'X' not present'` with extra quotes. Use `exc.args[0]` instead. Note that `bot.py` already had this bug in pre-existing `removesound` and `renamesound` commands — left alone since fixing them was out of scope.

3. **`bot.setup_hook` runs before `on_ready` — any startup-state decision that spans those two phases must use a *frozen snapshot* of pre-setup state.** Soundbot's v1→v2 tag migration was silently skipped for every real user because:
   - `setup_hook` called `store.save()` unconditionally after `scan_folder`.
   - `save()` at the time mutated `self.loaded_version = CURRENT_SCHEMA_VERSION`.
   - When `on_ready` fired and called `run_migration_if_needed`, the gate `if store.loaded_version >= CURRENT_SCHEMA_VERSION: return` tripped.
   - The file on disk was already v2 with empty tags. No retry opportunity.

   Fix pattern: introduce a `startup_version` field that is set exactly once inside `load()` and never mutated by subsequent writes. The migration gate reads that frozen snapshot. Tests that construct `SoundStore` directly and skip `setup_hook` will happily pass against the broken version — the regression test must explicitly simulate `load() -> save() -> run_migration_if_needed` to catch it.

**Why:** All three surfaced during self-review / second-maintainability-review passes. The startup sequence trap is the most load-bearing: it's a silent data-loss bug for every v1 upgrader. Tests that construct units in isolation cannot catch it — you need a test that exercises the real event ordering.

**How to apply:**
- When writing slash commands that read sibling option values for cross-field autocomplete, use `getattr(interaction.namespace, "field", None)`.
- When propagating store-layer KeyError messages to users, prefer `exc.args[0]` over `str(exc)`.
- When a state field is consumed across `setup_hook`/`on_ready` (or any two-phase Discord lifecycle points), treat the pre-hook value as a frozen snapshot. Any "current" field that `save()` or similar persistence calls mutate is a footgun for gate checks.
