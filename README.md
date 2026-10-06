# Soundbot

A Discord soundboard bot with no limits. Play sound clips in voice channels using slash commands, interactive button boards, and unlimited audio mixing.

## Features

- `/play` with fuzzy autocomplete across your entire sound library
- Unlimited simultaneous sound overlap (no queue, no cap)
- Interactive `/board` with paginated buttons for quick access — boards are deleted automatically once the bot leaves voice
- Upload sounds directly in Discord or bulk-load from a folder
- Bind emoji to sounds — a reaction anywhere plays the sound (while the bot is in voice)
- Playback requires being in the bot's voice channel, like Discord's native soundboard — no remote-spamming voice from a text channel
- Optional categories and tags for organization — tags filter `/play`, `/random` and `/board`
- Uploads (including video files) are auto-trimmed to 6.4s and loudness-normalized
- Optional web admin panel for managing the library from a browser
- Optional auto-join: on one nominated server, the bot walks into named voice channels by itself as soon as someone is in them
- Auto-leaves the voice channel after 10 minutes alone (configurable)
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
| `/board` | Show clickable button board of all sounds (auto-deleted when the bot leaves voice) |
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
| `/tag add <sound> <tag>` | Add a tag to a sound |
| `/tag remove <sound> <tag>` | Remove a tag from a sound |
| `/tag list [sound]` | List a sound's tags, or every tag in use with counts |

`/play`, `/random` and `/board` also take an optional `tag` option: on `/random` and `/board` it filters the sounds; on `/play` it narrows the name autocomplete.

## Adding Sounds

### Via Discord

Use `/addsound` and attach an audio file. Any format FFmpeg supports works (mp3, wav, ogg, m4a, flac, opus, etc.), and video files (mp4, webm, …) have their audio track extracted. Clips longer than 6.4 seconds are trimmed to the first 6.4 seconds, and clips louder than `TARGET_LUFS` are turned down to it. New sounds are auto-tagged with the server's name so they show up on that server's tag-filtered boards.

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

Then restart (`docker compose up -d --build`) and open `http://<host>:8000` on your LAN. Enter the token on the login screen; it's stored in your browser and sent with every request. Uploads go through the same pipeline as `/addsound` (video extraction, auto-trim, loudness normalization), so sounds added from the browser behave exactly like sounds added from Discord.

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
| `BOARDS_FILE` | `boards.json` beside `METADATA_FILE` | Registry of posted `/board` messages, so they can be deleted after the bot leaves voice (or after a restart) |
| `DEFAULT_VOLUME` | `50` | Playback volume on startup (0-100) |
| `TARGET_LUFS` | `-16` | Loudness target for uploads. Sounds louder than this are turned down on upload (never boosted). |
| `LOG_FILE` | `./soundbot.log` | Log file path (rotating, 5MB, 3 backups) |
| `SYNC_COMMANDS` | `true` | Sync slash commands per-guild on startup and on join. Set to `false` to skip syncing entirely. |
| `IDLE_TIMEOUT` | `600` | Seconds the bot may sit alone in a voice channel (no humans, bots don't count) before it disconnects itself. `0` disables auto-leave. |
| `AUTO_JOIN_GUILD` | *(empty)* | The one server auto-join applies to, by name or id. Empty = auto-join disabled. |
| `AUTO_JOIN_CHANNELS` | *(empty)* | Comma-separated voice channels to watch on that server, by name or id. Empty = auto-join disabled. |
| `AUTO_JOIN_COOLDOWN` | `300` | Seconds a deliberate exit keeps auto-join muted in that server. `0` = no mute. |
| `WEB_TOKEN` | *(empty)* | Auth token for the web admin panel. Empty = panel disabled. |
| `WEB_HOST` | `0.0.0.0` | Interface the web panel binds to inside the container. |
| `WEB_PORT` | `8000` | Port for the web admin panel. |

## Auto-Join

The bot can join voice on its own, so nobody has to run `/join` first. Name
one server and the voice channels to watch on it:

```
AUTO_JOIN_GUILD=Anti-Union
AUTO_JOIN_CHANNELS=Chillin,Deadlock,CS2
```

The moment a person appears in one of those channels, the bot connects to it.
Both settings take either a name or a Discord id — ids survive a rename, so
use them for channels you expect to rename (turn on **Developer Mode** in
Discord settings, then right-click a server or channel and **Copy ID**). Name
matching ignores case; a value that is all digits is only ever read as an id.

**Disabled by default.** Leaving either setting empty turns auto-join off
entirely, and it only ever applies to the single server in
`AUTO_JOIN_GUILD` — the bot's other servers keep behaving exactly as before.

Three rules keep it from becoming a nuisance:

- **It stays put.** If the bot is already in voice on that server, a join in
  another watched channel is ignored rather than making it hop — that would
  cut off whoever is still listening in the first channel.
- **A deliberate exit mutes it.** `/leave`, or disconnecting the bot by hand
  in Discord, suppresses auto-join on that server for `AUTO_JOIN_COOLDOWN`
  seconds (default 5 minutes). Without that, the next person to walk in would
  drag the bot straight back and `/leave` would look broken. `/join` clears
  the mute again, and `AUTO_JOIN_COOLDOWN=0` drops it entirely.
- **Auto-leave doesn't mute it.** Disconnecting after `IDLE_TIMEOUT` alone
  means "nobody is here", not "go away", so the next arrival is picked up
  normally.

Auto-join reacts to people arriving, so a bot restart leaves it out of voice
until the next person joins or re-joins a watched channel. Other bots joining
never count.

## Data and Persistence

The bot stores two things:

- **Audio files** in `sounds/` — the actual clips
- **Metadata** in `data/sounds.json` — names, categories, tags, emoji bindings, play counts, upload info

Both `sounds/` and `data/` are mounted as Docker volumes so they persist across container rebuilds (`docker-compose.yml` points `METADATA_FILE` into `data/` for you). Back up these two folders and you've backed up everything.

Play counts are saved to disk every 60 seconds and on graceful shutdown.

## Updating

```bash
git pull
docker compose up -d --build
```

**Upgrading from a version before metadata moved to `data/`:** older setups kept `sounds.json` inside the container, so a rebuild would wipe tags, play counts and emoji bindings. Copy it out *before* you rebuild:

```bash
mkdir -p data
docker compose cp soundbot:/app/sounds.json ./data/sounds.json
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

**Sound cut off early:** Clips longer than 6.4 seconds are trimmed to their first 6.4 seconds on upload. Trim to the part you want before uploading.

**"Already exists" but the sound isn't on the board:** Names are unique across the whole library, but boards are usually tag-filtered. The error lists the existing sound's tags; run `/board` with no filter to find it, or `/tag add` it for this server.
