import os
import time
import logging
import asyncio
import pymongo
from pyrogram import Client, filters
from pyrogram.enums import ParseMode, ChatType, ChatMemberStatus
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from pyrogram.errors import FloodWait

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (set these as environment variables on your host)
# ---------------------------------------------------------------------------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URL = os.environ["MONGO_URL"]
OWNER_ID = int(os.environ["OWNER_ID"])  # YOUR Telegram numeric user ID

app = Client("admin_control_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------------------------------------------------------
# Stylized text helper - converts plain ASCII into Unicode Mathematical
# Sans-Bold characters for section headers, giving the bot a more premium,
# polished look without relying on HTML formatting.
# ---------------------------------------------------------------------------
_BOLD_MAP = {}
for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _BOLD_MAP[c] = chr(0x1D5D4 + i)
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _BOLD_MAP[c] = chr(0x1D5EE + i)
for i, c in enumerate("0123456789"):
    _BOLD_MAP[c] = chr(0x1D7EC + i)


def bold_style(text):
    return "".join(_BOLD_MAP.get(ch, ch) for ch in text)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
mongo = pymongo.MongoClient(MONGO_URL)
db = mongo["admin_control_bot"]
channels_col = db["channels"]        # {_id: chat_id, name, type, added_at}
memberships_col = db["memberships"]  # {_id: "chatid_userid", chat_id, user_id, joined_at}
state_col = db["state"]              # {_id: key, value} - for multi-step flows
admins_col = db["admins"]            # {_id: user_id, added_at, added_by}


def is_bot_admin(user_id):
    return user_id == OWNER_ID or admins_col.find_one({"_id": user_id}) is not None


async def _admin_filter_func(_, __, message):
    if not message.from_user:
        return False
    return is_bot_admin(message.from_user.id)


admin_filter = filters.create(_admin_filter_func)


def get_state(key, default=None):
    doc = state_col.find_one({"_id": key})
    return doc["value"] if doc else default


def set_state(key, value):
    state_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


# ---------------------------------------------------------------------------
# /addchannel - link a channel or group by forwarding a message from it
# ---------------------------------------------------------------------------
@app.on_message(filters.command("addchannel") & filters.private & admin_filter)
async def admin_addchannel(client, message: Message):
    set_state("awaiting_channel_link", True)
    await message.reply_text(
        f"<b>{bold_style('Link a Channel or Group')}</b>\n\n"
        "Forward any message from the channel or group you'd like to link.\n\n"
        "Requirement: the bot must already be an admin there, with ban and "
        "delete-messages permissions.\n\n"
        "You can forward from multiple channels or groups one after another - "
        "I'll keep linking each one until you send /donechannels.",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("donechannels") & filters.private & admin_filter)
async def admin_donechannels(client, message: Message):
    set_state("awaiting_channel_link", False)
    count = channels_col.count_documents({})
    await message.reply_text(f"Done. Total linked channels/groups: {count}")


@app.on_message(filters.private & filters.forwarded & admin_filter)
async def admin_channel_link(client, message: Message):
    if not get_state("awaiting_channel_link"):
        return
    chat = message.forward_from_chat
    if not chat or chat.type not in (ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.GROUP):
        await message.reply_text("That doesn't look like a message from a channel or group. Please forward directly from it.")
        return
    channels_col.update_one(
        {"_id": chat.id},
        {"$set": {"name": chat.title or str(chat.id), "type": str(chat.type), "added_at": time.time()}},
        upsert=True,
    )
    await message.reply_text(
        f"Linked: {chat.title}\n\nForward another channel/group, or send /donechannels to finish."
    )


@app.on_message(filters.command("listchannels") & filters.private & admin_filter)
async def admin_listchannels(client, message: Message):
    chans = list(channels_col.find())
    if not chans:
        await message.reply_text("No channels or groups linked yet. Start with /addchannel.")
        return
    lines = [f"- {c['name']} (<code>{c['_id']}</code>)" for c in chans]
    await message.reply_text(
        f"<b>{bold_style('Linked Channels & Groups')}</b> ({len(chans)})\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Live membership tracking (only from the moment a chat is linked onward)
# ---------------------------------------------------------------------------
@app.on_chat_member_updated()
async def track_membership(client, update: ChatMemberUpdated):
    if not channels_col.find_one({"_id": update.chat.id}):
        return  # not a chat we're tracking

    user = update.new_chat_member.user if update.new_chat_member else (
        update.old_chat_member.user if update.old_chat_member else None
    )
    if not user:
        return

    key = f"{update.chat.id}_{user.id}"
    status = update.new_chat_member.status if update.new_chat_member else None

    if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        memberships_col.update_one(
            {"_id": key},
            {"$set": {
                "chat_id": update.chat.id, "user_id": user.id,
                "name": user.first_name or "", "username": user.username or "",
                "joined_at": time.time(),
            }},
            upsert=True,
        )
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, None):
        memberships_col.delete_one({"_id": key})


# ---------------------------------------------------------------------------
# /checkuser <id> - where is this user, since when, with a Remove-from-All button
# ---------------------------------------------------------------------------
@app.on_message(filters.command("checkuser") & filters.private & admin_filter)
async def admin_checkuser(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /checkuser <user_id>")
        return
    user_id = int(parts[1].strip())

    chans = list(channels_col.find())
    if not chans:
        await message.reply_text("No channels or groups linked yet. Start with /addchannel.")
        return

    status_msg = await message.reply_text("Checking...")
    found = []
    for c in chans:
        try:
            member = await client.get_chat_member(c["_id"], user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                continue
            rec = memberships_col.find_one({"_id": f"{c['_id']}_{user_id}"})
            joined = (
                time.strftime("%d %b %Y", time.localtime(rec["joined_at"]))
                if rec else "unknown (joined before this chat was linked)"
            )
            found.append((c["name"], c["_id"], joined))
        except Exception:
            continue  # not a member, or bot isn't admin there
        await asyncio.sleep(0.1)

    if not found:
        await status_msg.edit_text(f"User <code>{user_id}</code> was not found in any linked channel or group.", parse_mode=ParseMode.HTML)
        return

    lines = [f"- {name} - joined: {joined}" for name, _, joined in found]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Remove from ALL", callback_data=f"rmall_{user_id}")
    ]])
    await status_msg.edit_text(
        f"<b>{bold_style('User')} {user_id}</b> found in:\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^rmall_(\d+)$"))
async def remove_from_all(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer("Only the owner can do this.", show_alert=True)
        return
    user_id = int(query.data.split("_", 1)[1])
    await query.answer("Removing...")

    chans = list(channels_col.find())
    removed, failed = 0, 0
    for c in chans:
        try:
            await client.ban_chat_member(c["_id"], user_id)  # permanent
            memberships_col.delete_one({"_id": f"{c['_id']}_{user_id}"})
            removed += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await client.ban_chat_member(c["_id"], user_id)
                removed += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.5)

    await query.message.edit_text(
        f"User <code>{user_id}</code> permanently removed from {removed} chat(s). Failed: {failed}",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /kickall <chat_id> - permanently remove every member from one linked chat
# ---------------------------------------------------------------------------
@app.on_message(filters.command("kickall") & filters.private & admin_filter)
async def admin_kickall_start(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /kickall <chat_id>\n\nCopy the ID from /listchannels.")
        return
    chat_id = int(parts[1].strip())
    chat = channels_col.find_one({"_id": chat_id})
    if not chat:
        await message.reply_text("This chat isn't linked yet. Link it first with /addchannel.")
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, remove everyone PERMANENTLY", callback_data=f"kickall_confirm_{chat_id}"),
        InlineKeyboardButton("Cancel", callback_data="kickall_cancel"),
    ]])
    await message.reply_text(
        f"This will <b>permanently</b> remove every member of <b>{chat['name']}</b> "
        f"(admins and the owner excluded). This cannot be undone automatically - "
        f"you would need to manually unban anyone you want back. Proceed?",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^kickall_cancel$"))
async def kickall_cancel(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer()
        return
    await query.answer("Cancelled.")
    await query.message.edit_text("Cancelled - no members were removed.")


@app.on_callback_query(filters.regex(r"^kickall_confirm_(-?\d+)$"))
async def kickall_confirm(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer("Only the owner can do this.", show_alert=True)
        return
    chat_id = int(query.data.split("_", 2)[2])
    await query.answer()
    status_msg = await query.message.edit_text("Fetching member list...")

    try:
        members = []
        async for m in client.get_chat_members(chat_id):
            if m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                continue
            members.append(m.user.id)
    except Exception as e:
        await status_msg.edit_text(f"Couldn't fetch the member list: {e}")
        return

    if not members:
        await status_msg.edit_text("No removable members found (everyone here is an admin or the owner).")
        return

    removed, failed = 0, 0
    for i, uid in enumerate(members):
        try:
            await client.ban_chat_member(chat_id, uid)  # permanent - no unban
            memberships_col.delete_one({"_id": f"{chat_id}_{uid}"})
            removed += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await client.ban_chat_member(chat_id, uid)
                removed += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(1)
        if (i + 1) % 10 == 0:
            try:
                await status_msg.edit_text(f"Removing... {i + 1}/{len(members)}")
            except Exception:
                pass

    await status_msg.edit_text(f"Done.\nPermanently removed: {removed}\nFailed: {failed}")


# ---------------------------------------------------------------------------
# /purge <chat_id> - bulk-delete messages (media only, or everything)
# ---------------------------------------------------------------------------
@app.on_message(filters.command("purge") & filters.private & admin_filter)
async def admin_purge_start(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /purge <chat_id>\n\nCopy the ID from /listchannels.")
        return
    chat_id = int(parts[1].strip())
    chat = channels_col.find_one({"_id": chat_id})
    if not chat:
        await message.reply_text("This chat isn't linked yet. Link it first with /addchannel.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Media files only", callback_data=f"purge_media_{chat_id}")],
        [InlineKeyboardButton("Everything (all messages)", callback_data=f"purge_all_{chat_id}")],
        [InlineKeyboardButton("Cancel", callback_data="purge_cancel")],
    ])
    await message.reply_text(
        f"What should be deleted from <b>{chat['name']}</b>?\n\n"
        f"<b>Media files only</b> — video, document, photo, and audio messages (text left untouched).\n"
        f"<b>Everything</b> — every message in the chat, including text messages and system "
        f"messages like \"X joined the group\".\n\n"
        f"This action is irreversible. Proceed?",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^purge_cancel$"))
async def purge_cancel(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer()
        return
    await query.answer("Cancelled.")
    await query.message.edit_text("Cancelled - nothing was deleted.")


async def _run_purge(client, status_msg, chat_id, media_only):
    scan_label = "media messages" if media_only else "messages"
    await status_msg.edit_text(f"Scanning for {scan_label}...")

    try:
        ids_to_delete = []
        async for msg in client.get_chat_history(chat_id):
            if media_only:
                if msg.video or msg.document or msg.photo or msg.audio:
                    ids_to_delete.append(msg.id)
            else:
                ids_to_delete.append(msg.id)  # every message, text + media + service/join messages
    except Exception as e:
        await status_msg.edit_text(f"Couldn't fetch chat history: {e}")
        return

    if not ids_to_delete:
        await status_msg.edit_text("Nothing found to delete.")
        return

    deleted = 0
    batch_size = 100  # Telegram allows deleting up to 100 message IDs per call
    for i in range(0, len(ids_to_delete), batch_size):
        batch = ids_to_delete[i:i + batch_size]
        try:
            await client.delete_messages(chat_id, batch)
            deleted += len(batch)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await client.delete_messages(chat_id, batch)
                deleted += len(batch)
            except Exception as e2:
                logger.error(f"Purge batch failed: {e2}")
        except Exception as e:
            logger.error(f"Purge batch failed: {e}")
        await asyncio.sleep(0.5)
        try:
            await status_msg.edit_text(f"Deleting... {deleted}/{len(ids_to_delete)}")
        except Exception:
            pass

    await status_msg.edit_text(f"Done. Deleted {deleted}/{len(ids_to_delete)} message(s).")


@app.on_callback_query(filters.regex(r"^purge_media_(-?\d+)$"))
async def purge_media(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer("Only the owner can do this.", show_alert=True)
        return
    chat_id = int(query.data.split("_", 2)[2])
    await query.answer()
    status_msg = await query.message.edit_text("Starting...")
    await _run_purge(client, status_msg, chat_id, media_only=True)


@app.on_callback_query(filters.regex(r"^purge_all_(-?\d+)$"))
async def purge_all(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer("Only the owner can do this.", show_alert=True)
        return
    chat_id = int(query.data.split("_", 2)[2])
    await query.answer()
    status_msg = await query.message.edit_text("Starting...")
    await _run_purge(client, status_msg, chat_id, media_only=False)




# ---------------------------------------------------------------------------
# Admin management (owner only)
# ---------------------------------------------------------------------------
@app.on_message(filters.command("addadmin") & filters.private & filters.user(OWNER_ID))
async def owner_addadmin(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /addadmin <user_id>")
        return
    new_admin_id = int(parts[1].strip())
    if new_admin_id == OWNER_ID:
        await message.reply_text("That's you — you're already the owner.")
        return
    admins_col.update_one(
        {"_id": new_admin_id},
        {"$set": {"added_at": time.time(), "added_by": OWNER_ID}},
        upsert=True,
    )
    await message.reply_text(f"✅ User <code>{new_admin_id}</code> can now use all bot commands.", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("removeadmin") & filters.private & filters.user(OWNER_ID))
async def owner_removeadmin(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /removeadmin <user_id>")
        return
    removed_id = int(parts[1].strip())
    result = admins_col.delete_one({"_id": removed_id})
    if result.deleted_count:
        await message.reply_text(f"✅ Removed <code>{removed_id}</code> from bot admins.", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text("That user wasn't a bot admin.")


@app.on_message(filters.command("listadmins") & filters.private & filters.user(OWNER_ID))
async def owner_listadmins(client, message: Message):
    admins = list(admins_col.find())
    lines = [f"• <code>{OWNER_ID}</code> (owner)"]
    lines += [f"• <code>{a['_id']}</code>" for a in admins]
    await message.reply_text(
        f"<b>{bold_style('Bot Admins')}</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@app.on_message(filters.command("start") & filters.private & admin_filter)
async def admin_start(client, message: Message):
    text = (
        f"<b>{bold_style('Admin Control Bot')}</b>\n\n"
        f"<b>{bold_style('Setup')}</b>\n"
        "/addchannel - Link a channel/group (forward a message from it)\n"
        "/donechannels - Finish linking channels/groups\n"
        "/listchannels - Show all linked channels/groups\n\n"
        f"<b>{bold_style('User Lookup')}</b>\n"
        "/checkuser &lt;id&gt; - Find which linked chats a user is in, since when, with a Remove-from-All button\n\n"
        f"<b>{bold_style('Bulk Actions')}</b>\n"
        "/kickall &lt;chat_id&gt; - Permanently remove ALL members from one chat\n"
        "/purge &lt;chat_id&gt; - Delete messages from one chat (choose: media files only, or everything)"
    )
    if message.from_user.id == OWNER_ID:
        text += (
            f"\n\n<b>{bold_style('Admin Management')}</b> (owner only)\n"
            "/addadmin &lt;user_id&gt; - Let another user use this bot\n"
            "/removeadmin &lt;user_id&gt; - Revoke a user's access\n"
            "/listadmins - Show everyone with access"
        )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Admin Control Bot starting...")
    app.run()
