# Getting Started

TgBridge is a single-user Telegram bot that mirrors forwarded files to Google Drive
through rclone, and can send files back to the chat. It runs as one process, keeps no
database, and treats Telegram messages as its whole UI.

## Prerequisites

- Python 3.12 or newer, or the published container image
- [`rclone`](https://rclone.org/) on `PATH`, with a remote configured. See the
  [rclone documentation](https://rclone.org/docs/) — any remote works, the bot does not
  care which backend it points at.
- A bot token from [@BotFather](https://t.me/BotFather)
- `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org)

Check the remote before starting the bot — the bot performs the same check at startup
and refuses to run if it fails:

```bash
export RCLONE_CONFIG=/path/to/rclone.conf   # optional: skip the default ~/.config/rclone/rclone.conf
rclone listremotes                          # your remote name must appear in the output
```

That is the whole integration: TgBridge shells out to `rclone` and inherits its
environment, so whichever config `RCLONE_CONFIG` points at is the one that gets used.
No rclone settings live in TgBridge's own configuration beyond `RCLONE_REMOTE`.

## Installation

### From source

```bash
git clone https://github.com/YewFence/tgbridge.git
cd tgbridge
mise trust
mise install
mise run deps:fix
uv run python -m tgbridge.entry
```

### As a container

The image runs `python -m tgbridge.entry` and expects configuration through
environment variables. Keep the scratch directory on fast local storage and point
`RCLONE_CONFIG` at a mounted `rclone.conf`:

```yaml
services:
  tgbridge:
    image: ghcr.io/yewfence/tgbridge:latest
    restart: unless-stopped
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      API_ID: ${API_ID}
      API_HASH: ${API_HASH}
      ALLOWED_CHATS: ${ALLOWED_CHATS}
      RCLONE_REMOTE: gdrive:media
      RCLONE_CONFIG: /config/rclone/rclone.conf
    volumes:
      - ./rclone:/config/rclone:ro
    tmpfs:
      - /tmp/tgbridge
```

`docker stop` sends `SIGTERM`, which cancels active tasks (stopping downloads, killing
rclone, clearing scratch files) before the process exits.

## Usage

Put the command in the file's caption, reply with it to a message you already sent,
or send it bare first — the bot then picks up the next file you forward within 60
seconds (a waiting message with a cancel button appears in the meantime).
Both work identically, and it does not matter whether the message was forwarded or
sent directly.

1. **Mirror** — forward a file with `/mirror` (or just forward it when `AUTO_MIRROR` is
   on). The bot replies with a progress message and edits it in place; when it finishes,
   the message shows the remote path it landed on.
2. **Send back** — forward a file with `/back`. Single files come back unchanged as
   documents; albums and sticker sets come back as a zip.
3. **Albums and stickers** — a forwarded media group becomes one task with one progress
   message. A sticker expands to its whole sticker set, so mirroring one sticker files
   the entire set under `<date>/<set_name>/`.

While a task runs, its message carries a **❌ Cancel** button. If a task fails or you
cancel it by mistake, reply to the message with `/retry` to run it again — the same
progress message is reused. After a restart the in-memory mapping is gone, so `/retry`
reports that the task has expired; forward the file again instead.

## Configuration

Environment variables win; `config.toml` in the working directory is the fallback base.
Set `TG_BRIDGE_CONFIG` to point at a different file. See
[`config.example.toml`](https://github.com/YewFence/tgbridge/blob/main/config.example.toml)
for a commented copy.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `BOT_TOKEN` | ✓ | | bot token from @BotFather |
| `API_ID` / `API_HASH` | ✓ | | credentials from my.telegram.org |
| `ALLOWED_CHATS` | ✓ | | comma-separated chat IDs with full access |
| `RCLONE_REMOTE` | ✓ | | `remote:path`, where `remote` is any configured rclone remote |
| `AUTO_MIRROR` | | `false` | initial auto-mirror state; `/auto` toggles it at runtime |
| `MAX_CONCURRENT` | | `2` | tasks running at once (download plus upload) |
| `TMP_DIR` | | `/tmp/tgbridge` | scratch space, wiped on startup — do not share it |
| `LOG_LEVEL` | | `INFO` | log level for both sinks |
| `LOG_FILE` | | | also write JSON lines here; stdout stays human-readable |
| `GD_DIR_TEMPLATE` | | `%Y-%m-%d` | `strftime` template for the remote subfolder; empty flattens the layout |

Every chat in `ALLOWED_CHATS` has full access: there are no roles and no per-user
isolation. `AUTO_MIRROR` is a single global switch held in memory, so it returns to the
configured value after a restart.

`RCLONE_CONFIG` is rclone's own environment variable, not a TgBridge setting: the bot
launches `rclone` as a subprocess and inherits the environment, so setting it to your
`rclone.conf` is all the rclone configuration needed.

### Size limits

`/back` is limited to 2,097,152,000 bytes (2 GiB) because bot accounts cannot use
Telegram's larger upload channel. The check happens twice: once against the message
metadata before any download starts, and again against the finished archive. `/mirror`
has no such limit — downloads are unrestricted, and Google Drive takes the file as-is.
