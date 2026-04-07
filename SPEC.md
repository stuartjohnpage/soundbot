# Discord Soundbot — v1 Specification

## Overview

A Discord bot that replaces Discord's built-in soundboard, removing the limitations on sound count and clip duration. Built in Python using discord.py, deployed via Docker.

## Core Behavior

### Voice Channel Management

- **Explicit join/leave** — The bot joins a voice channel only when an admin runs `/join` (joins the user's current voice channel) and stays until `/leave` is issued.
- The bot must be in a voice channel before any sound can be played. If a user triggers a sound while the bot is not connected, respond with an ephemeral error.

### Sound Playback

- **Trigger:** `/play <name>` with fuzzy autocomplete against the full sound library.
- **Mixing:** Unlimited simultaneous overlapping sounds. When multiple sounds are triggered concurrently, they are mixed in real time into a single audio stream sent to Discord. There is no queue and no cap on concurrent clips.
- **Format handling:** FFmpeg handles all format conversion at playback time. No pre-conversion on upload.
- **Feedback:** On successful play, the bot posts a public message in the text channel: the sound name and who played it. Errors (sound not found, bot not in channel, etc.) are sent as ephemeral messages visible only to the invoking user.

### Random Play

- `/random` plays a random sound from the full library.
- `/random [category]` plays a random sound from the specified category.

### Button Board

- `/board` posts a paginated embed with buttons for all sounds in the library.
- Discord allows 5 rows of 5 buttons per message (25 max). Navigation buttons (Previous / Next) occupy the last row when pagination is needed, leaving 20 sound buttons per page.
- Pressing a sound button triggers playback (same behavior as `/play`).

### Volume Control

- `/volume <0-100>` sets the bot's global output volume as a percentage. Affects all sound playback.
- Default volume: 50%.
- Volume persists across sounds but resets on bot restart (not persisted to disk in v1).

## Sound Management

### Adding Sounds

**Via Discord (primary):**

- `/addsound name:<name> [category:<category>] file:<attachment>` — Upload an audio file directly in Discord.
- Accepts any audio format that FFmpeg can decode (mp3, wav, ogg, m4a, flac, opus, webm, etc.).
- Validates duration is **<= 6 seconds**. Rejects with an ephemeral error if exceeded.
- Sound names must be unique (case-insensitive). Rejects duplicates.
- Sound names must be alphanumeric + hyphens + underscores only, max 32 characters.
- Category is optional. If omitted, the sound is uncategorized.

**Via folder scan (bulk loading):**

- On startup, the bot scans a configured `sounds/` directory for audio files.
- Files not already tracked in the metadata JSON are imported automatically.
- Filename (without extension) becomes the sound name.
- Category can be inferred from subfolder: `sounds/memes/airhorn.mp3` → name: `airhorn`, category: `memes`.
- Files in the root `sounds/` folder are uncategorized.

### Removing Sounds

- `/removesound name:<name>` — Deletes the sound file and removes its metadata entry.
- Autocomplete on the name parameter.

### Renaming Sounds

- `/renamesound old:<name> new:<name>` — Renames a sound. Same naming rules as `/addsound`.
- Autocomplete on the old name parameter.

### Listing Sounds

- `/listsounds` — Posts a paginated embed listing all sounds with their categories and play counts.
- `/listsounds [category]` — Filters to a specific category.

## Organization

- Each sound may optionally belong to **one category**.
- Categories are created implicitly when a sound is assigned to one (no separate category management).
- Categories are used for:
  - Filtering in `/listsounds`
  - Filtering in `/random`
  - Display grouping in `/board`

## Permissions

- **All commands require an admin role.** The role name is configurable via environment variable (default: `Soundbot Admin`).
- Users without the role receive an ephemeral "you don't have permission" error on any command.

## Storage

### Sound Files

- Stored on disk in a `sounds/` directory.
- Flat structure (files uploaded via Discord go to `sounds/` root regardless of category — category is metadata only).
- Bulk-loaded files from subfolders stay where they are.

### Metadata

- Single `sounds.json` file at the project root (configurable path).
- Schema:

```json
{
  "sounds": {
    "airhorn": {
      "file": "sounds/airhorn.mp3",
      "category": "memes",
      "uploaded_by": "stuart#1234",
      "uploaded_at": "2026-04-06T12:00:00Z",
      "play_count": 42
    }
  },
  "version": 1
}
```

## Logging

- **File log:** All plays logged to a rotating log file with: timestamp, sound name, user, channel, guild.
- **Play counts:** Incremented in `sounds.json` on each play. Persisted to disk periodically (not on every single play — batch writes to avoid thrashing).

## Configuration

All configuration via environment variables (loaded from `.env` in development):

| Variable | Description | Default |
|---|---|---|
| `DISCORD_TOKEN` | Bot token | (required) |
| `ADMIN_ROLE` | Role name required for all commands | `Soundbot Admin` |
| `SOUNDS_DIR` | Directory for sound files | `./sounds` |
| `METADATA_FILE` | Path to sounds.json | `./sounds.json` |
| `DEFAULT_VOLUME` | Default playback volume (0-100) | `50` |
| `LOG_FILE` | Path to log file | `./soundbot.log` |

## Tech Stack

- **Python 3.12+**
- **discord.py** (latest stable, v2.x) with voice support
- **FFmpeg** (required for audio playback and duration validation)
- **Docker + Docker Compose** for deployment
- **python-dotenv** for local development

## Deployment

### Docker Compose

```yaml
services:
  soundbot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sounds:/app/sounds
      - ./sounds.json:/app/sounds.json
      - ./logs:/app/logs
```

### Dockerfile

- Base image: `python:3.12-slim`
- Install FFmpeg via apt
- Copy source, install dependencies
- `CMD ["python", "-m", "soundbot"]`

## Slash Commands Summary

| Command | Description |
|---|---|
| `/join` | Bot joins your voice channel |
| `/leave` | Bot leaves the voice channel |
| `/play <name>` | Play a sound (fuzzy autocomplete) |
| `/random [category]` | Play a random sound |
| `/board` | Post paginated button panel of all sounds |
| `/volume <0-100>` | Set global playback volume |
| `/addsound name file [category]` | Upload a new sound |
| `/removesound name` | Delete a sound |
| `/renamesound old new` | Rename a sound |
| `/listsounds [category]` | List all sounds |

## v2 Backlog

Tracked as GitHub issues:

1. **Web admin panel** — Browse, rename, delete, re-categorize, upload, preview sounds via a web UI.
2. **SQLite migration** — Migrate from JSON to SQLite if the metadata file becomes unwieldy.
3. **`/stats` command** — Show most played sounds, play counts by user, etc.
4. **Favorites system** — Users can favorite sounds; `/board` shows personal favorites by default.
