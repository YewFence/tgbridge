# TgBridge

[![Release](https://img.shields.io/github/v/release/YewFence/tgbridge?sort=semver)](https://github.com/YewFence/tgbridge/releases)
[![Docs](https://img.shields.io/badge/docs-online-blue)](https://YewFence.github.io/tgbridge/)
[![License](https://img.shields.io/github/license/YewFence/tgbridge)](LICENSE)

Minimal personal bot mirroring forwarded Telegram files to Google Drive via rclone

> [!NOTE]
> This project is in an early development stage. Core features may be missing, and backward compatibility is not guaranteed.

## What It Does

Forward a file to the bot (or send one directly) and it either:

- **mirrors** it to Google Drive through rclone, filed under a daily folder, or
- **sends it back** to the chat as a document, packing albums and sticker sets into a zip.

Everything is driven from Telegram: one progress message per task, an inline cancel
button, and `/retry` for the ones that failed. There is no database, no web UI, and no
state that survives a restart — the bot holds at most `MAX_CONCURRENT` tasks in memory.

## Quick Start

### Requirements

- Python 3.12+ (or the container image)
- [`rclone`](https://rclone.org/) on `PATH` with any remote configured — see the
  [rclone documentation](https://rclone.org/docs/) for setup. Any remote works; the bot
  does not care which one it is. Point `RCLONE_CONFIG` at your own `rclone.conf` if it
  is not at the default location, and `rclone listremotes` must list your remote.
- A bot token from [@BotFather](https://t.me/BotFather) and API credentials from
  [my.telegram.org](https://my.telegram.org)

### Configure

The bot reads environment variables first and falls back to `config.toml` in the
current directory (override the path with `TG_BRIDGE_CONFIG`). Only the first five
keys are required:

| Key | Required | Default | Meaning |
|---|---|---|---|
| `BOT_TOKEN` | ✓ | | bot token from @BotFather |
| `API_ID` / `API_HASH` | ✓ | | credentials from my.telegram.org |
| `ALLOWED_CHATS` | ✓ | | comma-separated chat IDs allowed to use the bot |
| `RCLONE_REMOTE` | ✓ | | `remote:path`, where `remote` is any configured rclone remote |
| `AUTO_MIRROR` | | `false` | mirror plain forwards without a command |
| `MAX_CONCURRENT` | | `2` | tasks running at once |
| `TMP_DIR` | | `/tmp/tgbridge` | download scratch space, owned by the bot |
| `LOG_LEVEL` | | `INFO` | log level for both sinks |
| `LOG_FILE` | | | also write JSON lines here; stdout stays human-readable |
| `GD_DIR_TEMPLATE` | | `%Y-%m-%d` | `strftime` template for the remote subfolder; empty to disable |

A `config.toml` copy of the same keys (lowercase) works for local development — see
[`config.example.toml`](config.example.toml).

### Run the Container Image

```bash
cp .env.example .env          # fill in the credentials and RCLONE_REMOTE
docker compose up -d
docker compose logs -f tgbridge
```

The image is published to `ghcr.io/yewfence/tgbridge` for `linux/amd64` and `linux/arm64`,
carrying a version tag and `latest` for stable releases. Set `TGBRIDGE_IMAGE` in `.env` to
pin a version. See the [container guide](https://github.com/YewFence/tgbridge/blob/main/docs/CONTAINER.md)
for the build layout and the local development overlay.

### Build From Source

```bash
git clone https://github.com/YewFence/tgbridge.git
cd tgbridge
mise trust
mise install
mise run deps:fix
uv run python -m tgbridge.entry
```

The bot exits immediately if the rclone remote is missing or unconfigured, so a typo
in `RCLONE_REMOTE` shows up at startup rather than on the first upload.

### Usage

Send a command to the bot. Put it in the caption of the file, reply with it to a
message you already sent, or send it bare and follow up with the file — the bot
pairs it with your next media message for 60 seconds (a "waiting" message with a
cancel button appears in the meantime):

| Command | Effect |
|---|---|
| `/mirror`, `/m` | download and upload to Google Drive |
| `/back`, `/b` | pack (if needed) and send back to this chat |
| `/auto` | toggle auto-mirroring of plain forwards |
| `/status`, `/st` | version, auto-mirror state, task counts, remote, disk space |
| `/retry` | reply to a failed or cancelled task message to run it again |
| `/help`, `/start` | command summary |

Notes:

- Albums and stickers count as one task: a forwarded media group becomes a single
  task, and a sticker becomes its whole sticker set.
- `/back` is capped at 2 GiB (`2,097,152,000` bytes) because bot accounts cannot use
  the larger upload channel. `/mirror` has no such cap.
- Task state lives in memory only. After a restart, `/retry` on an old message reports
  that the task has expired; forward the file again instead.

## Development

```bash
mise run hooks:install
mise run check
mise run test
```

See the [Contributing Guide](CONTRIBUTING.md) for the complete development workflow.

## Documentation

See the [documentation site](https://YewFence.github.io/tgbridge/) for setup, usage, and configuration guidance.

## Contributing

If you have suggestions or find a bug, please [open an issue](https://github.com/YewFence/tgbridge/issues).

Pull requests are welcome. See the [Contributing Guide](CONTRIBUTING.md).

## License

[MIT License](LICENSE)
