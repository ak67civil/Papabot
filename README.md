# Admin Control Bot

Telegram bot to manage multiple channels/groups at once: find where a user
is a member (and since when), remove them from everywhere in one click,
mass-remove all members from a chat, and bulk-delete media files.

## Files
- `bot.py` — main bot code
- `requirements.txt` — Python dependencies
- `Procfile` — tells Render/Heroku how to run the worker
- `runtime.txt` — pins a Python version compatible with Pyrogram

## Environment Variables (set these on Render/Heroku)

| Variable | Where to get it |
|---|---|
| `API_ID` | https://my.telegram.org |
| `API_HASH` | https://my.telegram.org |
| `BOT_TOKEN` | @BotFather on Telegram → `/newbot` (use a NEW bot, separate from your other bots) |
| `MONGO_URL` | Your MongoDB connection string (same cluster as your other bots is fine — this bot uses its own separate database inside it) |
| `OWNER_ID` | Your personal Telegram numeric user ID (get it from @userinfobot) |

## First-time setup (after deploying)

1. Add the bot as **admin** to every channel/group you want it to manage,
   with ban + delete-messages permissions.
2. DM the bot `/start` to see the command list.
3. Send `/addchannel`, then forward a message from each channel/group you
   want linked, one at a time. Send `/donechannels` when finished.
4. Confirm with `/listchannels`.

## Commands

| Command | What it does |
|---|---|
| `/addchannel` | Start linking channels/groups (forward messages one by one) |
| `/donechannels` | Finish linking |
| `/listchannels` | Show all linked channels/groups with their IDs |
| `/checkuser <id>` | Find every linked chat a user is in, since when (if known), with a "Remove from ALL" button |
| `/kickall <chat_id>` | Permanently remove ALL members from one linked chat (admins/owner kept) |
| `/purge <chat_id>` | Delete ALL video/document/photo/audio messages from one linked chat |

## Important notes

- **Join dates**: only tracked from the moment a chat is linked onward.
  Members who joined before that show as "unknown (joined before bot)".
- **Removals are permanent bans**, not temporary kicks — undo manually if needed.
- **/purge is irreversible** — deleted files cannot be recovered.
- Both `/kickall` and `/purge` ask for confirmation before doing anything.
