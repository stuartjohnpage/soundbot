# Soundbot

A Discord soundboard bot with no limits. Play sound clips in voice channels using slash commands, interactive button boards, and unlimited audio mixing.

## Features

- `/play` with fuzzy autocomplete across your entire sound library
- Unlimited simultaneous sound overlap (no queue, no cap)
- Interactive `/board` with paginated buttons for quick access
- Upload sounds directly in Discord or bulk-load from a folder
- Bind emoji to sounds — a reaction anywhere plays the sound (while the bot is in voice)
- Playback requires being in the bot's voice channel, like Discord's native soundboard — no remote-spamming voice from a text channel
- Optional categories for organization
- Global volume control
- Play count tracking and file logging

## Requirements

- A server (any always-on machine)
- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- A Discord bot token ([how to get one](#creating-a-discord-bot))

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/stuartjohnpage/soundbot.git
cd soundbot
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and add your bot token:

```
DISCORD_TOKEN=your-bot-token-here
```

### 3. Start the bot

```bash
docker compose up -d
```

That's it. The bot syncs its slash commands to each server it's in on startup, and to any new server the moment it's invited. Guild-scoped syncs apply immediately — no waiting for global propagation.

### 4. Invite the bot to your server

Use this URL template, replacing `YOUR_CLIENT_ID` with your bot's application ID:

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=36700160&scope=bot%20applications.commands
```

The permission integer `36700160` grants: Connect, Speak, Use Voice Activity, and Send Messages.

### 5. Create the admin role

Create a role in your Discord server called **Soundbot Admin** (or whatever you set `ADMIN_ROLE` to in `.env`). Assign it to anyone who should be able to use the bot. All commands require this role.

## Commands

| Command | Description |
|---|---|
| `/join` | Bot joins your current voice channel |
| `/leave` | Bot leaves the voice channel |
| `/play <name>` | Play a sound (fuzzy autocomplete) |
| `/random [category]` | Play a random sound |
| `/board` | Show clickable button board of all sounds |
| `/volume <0-100>` | Set playback volume (default: 50) |
| `/addsound <name> <file> [category] [tags]` | Upload a new sound (sounds over 6.4s are trimmed to the first 6.4s; loudness-normalized and auto-tagged with the server's tag) |
| `/removesound <name>` | Delete a sound |
| `/renamesound <old> <new>` | Rename a sound |
| `/listsounds [category] [page]` | List all sounds with play counts |
| `/stats` | Top 10 most played sounds and total play count |
| `/importsounds` | Import this server's Discord soundboard sounds (auto-tagged, loudness-normalized) |
| `/bindemoji <sound> <emoji>` | Bind an emoji: anyone reacting with it plays the sound (bindings are per-server) |
| `/unbindemoji <emoji>` | Remove an emoji binding |
| `/listbindings` | List this server's emoji-to-sound bindings |

## Adding Sounds

### Via Discord

Use `/addsound` and attach an audio file. Any format FFmpeg supports works (mp3, wav, ogg, m4a, flac, opus, etc.). Clips must be 6.4 seconds or shorter.

```
/addsound name:airhorn category:memes file:[attach audio]
```

### Bulk loading from a folder

Drop audio files into the `sounds/` directory on the host machine. The bot scans this folder on startup and imports any untracked files.

Use subfolders to auto-assign categories:

```
sounds/
  bruh.mp3              # no category
  memes/
    airhorn.mp3         # category: memes
    sad-trombone.wav    # category: memes
  games/
    victory.ogg         # category: games
```

Restart the bot after adding files to the folder:

```bash
docker compose restart
```

## Web Admin Panel

An optional browser UI for managing the sound library — browse with search and tag/category filters, upload, rename, delete, edit tags, and preview sounds in the browser.

**Disabled by default.** To enable it, set a token in `.env`:

```
WEB_TOKEN=some-long-random-string
```

Generate one with e.g. `openssl rand -hex 32`. If `WEB_TOKEN` is empty or unset, the web server never starts.

Then restart (`docker compose up -d --build`) and open `http://<host>:8000` on your LAN. Enter the token on the login screen; it's stored in your browser and sent with every request. Uploads go through the same pipeline as `/addsound` (duration limit, loudness normalization), so sounds added from the browser behave exactly like sounds added from Discord.

The panel runs inside the bot process and shares its sound library state. Change the port with `WEB_PORT` in `.env`.

> **Note:** the token is sent over plain HTTP, so treat the panel as LAN-only. Don't port-forward it to the open internet without putting a reverse proxy with TLS in front.

## Configuration

All settings are environment variables, configured in `.env`:

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token from Discord Developer Portal |
| `ADMIN_ROLE` | `Soundbot Admin` | Discord role name required for all commands |
| `SOUNDS_DIR` | `./sounds` | Directory for audio files |
| `METADATA_FILE` | `./sounds.json` | Path to the metadata JSON file |
| `DEFAULT_VOLUME` | `50` | Playback volume on startup (0-100) |
| `TARGET_LUFS` | `-16` | Loudness target for uploads. Sounds louder than this are turned down on upload (never boosted). |
| `LOG_FILE` | `./soundbot.log` | Log file path (rotating, 5MB, 3 backups) |
| `SYNC_COMMANDS` | `true` | Sync slash commands per-guild on startup and on join. Set to `false` to skip syncing entirely. |
| `WEB_TOKEN` | *(empty)* | Auth token for the web admin panel. Empty = panel disabled. |
| `WEB_HOST` | `0.0.0.0` | Interface the web panel binds to inside the container. |
| `WEB_PORT` | `8000` | Port for the web admin panel. |

## Data and Persistence

The bot stores two things:

- **Audio files** in `sounds/` — the actual clips
- **Metadata** in `sounds.json` — names, categories, play counts, upload info

Both are mounted as Docker volumes so they persist across container rebuilds. Back up these two things and you've backed up everything.

Play counts are saved to disk every 60 seconds and on graceful shutdown.

## Updating

```bash
git pull
docker compose up -d --build
```

## Creating a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** in the sidebar
4. Click **Reset Token** and copy it — this is your `DISCORD_TOKEN`
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Go to **OAuth2 > URL Generator**
7. Select scopes: `bot`, `applications.commands`
8. Select permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`
9. Copy the generated URL and open it in your browser to invite the bot

## Logs

Logs are written to the `logs/` directory (mounted from the container). The bot logs every sound play with the user, channel, and timestamp.

View live logs:

```bash
docker compose logs -f
```

## Troubleshooting

**Commands not showing up:** Commands sync per-guild and should appear within seconds. Make sure the bot is actually a member of the server (it must be invited with the `bot` scope, not just `applications.commands`), `SYNC_COMMANDS` is `true`, and try refreshing your Discord client (Ctrl+R).

**Bot joins but no sound plays:** Make sure FFmpeg is installed in the container (it is by default in the Docker image). If running outside Docker, install FFmpeg manually.

**"You don't have permission":** Make sure you have the admin role (default: `Soundbot Admin`). The role name is case-sensitive and must match `ADMIN_ROLE` in `.env` exactly.

**Sound rejected as too long:** Clips must be 6.4 seconds or shorter. Trim your audio before uploading.
