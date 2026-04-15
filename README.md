# Soundbot

A Discord soundboard bot with no limits. Play sound clips in voice channels using slash commands, interactive button boards, and unlimited audio mixing.

## Features

- `/play` with fuzzy autocomplete across your entire sound library
- Unlimited simultaneous sound overlap (no queue, no cap)
- Interactive `/board` with paginated buttons for quick access
- Upload sounds directly in Discord or bulk-load from a folder
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

That's it. The bot will sync its slash commands with Discord on first startup (can take up to an hour for global commands to propagate).

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
| `/addsound <name> <file> [category]` | Upload a new sound (max 6.4 seconds) |
| `/removesound <name>` | Delete a sound |
| `/renamesound <old> <new>` | Rename a sound |
| `/listsounds [category] [page]` | List all sounds with play counts |

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

## Configuration

All settings are environment variables, configured in `.env`:

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token from Discord Developer Portal |
| `ADMIN_ROLE` | `Soundbot Admin` | Discord role name required for all commands |
| `SOUNDS_DIR` | `./sounds` | Directory for audio files |
| `METADATA_FILE` | `./sounds.json` | Path to the metadata JSON file |
| `DEFAULT_VOLUME` | `50` | Playback volume on startup (0-100) |
| `LOG_FILE` | `./soundbot.log` | Log file path (rotating, 5MB, 3 backups) |
| `SYNC_COMMANDS` | `true` | Sync slash commands on startup. Set to `false` after first run to avoid rate limits. |

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

**Commands not showing up:** Slash commands can take up to an hour to propagate globally. Wait, or restart with `SYNC_COMMANDS=true`.

**Bot joins but no sound plays:** Make sure FFmpeg is installed in the container (it is by default in the Docker image). If running outside Docker, install FFmpeg manually.

**"You don't have permission":** Make sure you have the admin role (default: `Soundbot Admin`). The role name is case-sensitive and must match `ADMIN_ROLE` in `.env` exactly.

**Sound rejected as too long:** Clips must be 6.4 seconds or shorter. Trim your audio before uploading.
