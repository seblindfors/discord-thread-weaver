# Thread Weaver

A Discord bot that merges forum posts. Messages are replayed using webhooks so they appear as the original authors, with their name and avatar intact.

## Features

- **Slash command** — `/merge` with Discord's built-in command menu
- **Webhook replay** — merged messages look like the original author posted them
- **Dry-run mode** — preview a merge before executing it
- **Auto-detect target** — run `/merge` inside a forum post to use it as the target
- **Attachment support** — images and files are re-uploaded (falls back to links for oversized files)
- **Author tagging** — original authors are mentioned once so they get added to the target thread
- **Source title preserved** — a header shows which post was merged in
- **Redirect duplicates** — `/redirect` closes a duplicate post, tags its users into the target, without replaying messages

## Usage

### Merge

```
/merge target:<link-or-id> source:<link-or-id>
```

Replays all messages from the source into the target using webhooks, then deletes the source.

- **target** — the post that stays (optional if run inside a forum post)
- **source** — the post whose messages get moved, then deleted
- **dry_run** — set to `True` to preview without making changes

### Redirect

```
/redirect target:<link-or-id> source:<link-or-id>
```

Closes a duplicate post and tags its participants into the target. No messages are replayed — just a "Redirected from" header with user mentions.

- **target** — the post to redirect users into (optional if run inside a forum post)
- **source** — the duplicate post (will be deleted)

All status messages are ephemeral (only you see them).

## Setup

### 1. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Go to **Bot** and copy the token.
3. Go to **OAuth2 > URL Generator**, check `bot` and `applications.commands` scopes.
4. Under **Bot Permissions**, check:
   - Manage Threads
   - Send Messages
   - Send Messages in Threads
   - Attach Files
   - Read Message History
   - Manage Webhooks
5. Open the generated URL to invite the bot to your server.

### 2. Install and run

```bash
git clone https://github.com/seblindfors/discord-thread-weaver.git
cd discord-thread-weaver
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your bot token and optionally your guild ID
python bot.py
```

### 3. Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Your bot token |
| `GUILD_ID` | No | Guild ID for instant slash command sync (recommended for development) |

Without `GUILD_ID`, commands sync globally which can take up to an hour.

## Permissions

The invoking user must have **Manage Threads** permission. You can further restrict access in **Server Settings > Integrations > Thread Weaver**.

## Requirements

- Python 3.8+
- discord.py 2.0+

## Future considerations

- **Undo support** — keep a log of merged posts so an `/unmerge` command could recreate the source
- **Cross-channel merge** — merge posts from different forum channels
- **Selective merge** — choose specific messages to move instead of all
