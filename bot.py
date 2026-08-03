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
# DB
# ---------------------------------------------------------------------------
mongo = pymongo.MongoClient(MONGO_URL)
db = mongo["admin_control_bot"]
channels_col = db["channels"]        # {_id: chat_id, name, type, added_at}
memberships_col = db["memberships"]  # {_id: "chatid_userid", chat_id, user_id, joined_at}
state_col = db["state"]              # {_id: owner_id, key: value} — for multi-step flows


def get_state(key, default=None):
    doc = state_col.find_one({"_id": key})
    return doc["value"] if doc else default


def set_state(key, value):
    state_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


# ---------------------------------------------------------------------------
# /addchannel — link a channel or group by forwarding a message from it
# ---------------------------------------------------------------------------
@app.on_message(filters.command("addchannel") & filters.private & filters.user(OWNER_ID))
async def admin_addchannel(client, message: Message):
    set_state("awaiting_channel_link", True)
    await message.reply_text(
        "📎 Ab jis bhi channel/group ko link karna hai, wahan se koi message "
        "yahan <b>forward</b> karo.\n\n"
        "Zaroori: bot us channel/group me <b>admin</b> ho, ban/delete-messages "
        "permission ke saath. Ek-ek karke jitne chahiye utne channels/groups "
        "forward kar sakte ho — jab tak /donechannels na bhejo, main link karta rahunga.",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("donechannels") & filters.private & filters.user(OWNER_ID))
async def admin_donechannels(client, message: Message):
    set_state("awaiting_channel_link", False)
    count = channels_col.count_documents({})
    await message.reply_text(f"✅ Done. Total linked channels/groups: {count}")


@app.on_message(filters.private & filters.forwarded & filters.user(OWNER_ID))
async def admin_channel_link(client, message: Message):
    if not get_state("awaiting_channel_link"):
        return
    chat = message.forward_from_chat
    if not chat or chat.type not in (ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.GROUP):
        await message.reply_text("❌ Ye kisi channel/group ka message nahi laga. Wahi se forward karo.")
        return
    channels_col.update_one(
        {"_id": chat.id},
        {"$set": {"name": chat.title or str(chat.id), "type": str(chat.type), "added_at": time.time()}},
        upsert=True,
    )
    await message.reply_text(
        f"✅ Linked: {chat.title}\n\nAur channel/group forward karo, ya /donechannels bhejo khatam karne ke liye."
    )


@app.on_message(filters.command("listchannels") & filters.private & filters.user(OWNER_ID))
async def admin_listchannels(client, message: Message):
    chans = list(channels_col.find())
    if not chans:
        await message.reply_text("Koi channel/group linked nahi hai. /addchannel se shuru karo.")
        return
    lines = [f"• {c['name']} (<code>{c['_id']}</code>)" for c in chans]
    await message.reply_text(
        f"<b>📋 Linked Channels/Groups ({len(chans)}):</b>\n\n" + "\n".join(lines),
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
# /checkuser <id> — where is this user, since when, with a Remove-from-All button
# ---------------------------------------------------------------------------
@app.on_message(filters.command("checkuser") & filters.private & filters.user(OWNER_ID))
async def admin_checkuser(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /checkuser <user_id>")
        return
    user_id = int(parts[1].strip())

    chans = list(channels_col.find())
    if not chans:
        await message.reply_text("Koi channel/group linked nahi hai. /addchannel se shuru karo.")
        return

    status_msg = await message.reply_text("🔎 Check kar raha hoon...")
    found = []
    for c in chans:
        try:
            member = await client.get_chat_member(c["_id"], user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                continue
            rec = memberships_col.find_one({"_id": f"{c['_id']}_{user_id}"})
            joined = (
                time.strftime("%d %b %Y", time.localtime(rec["joined_at"]))
                if rec else "unknown (bot se pehle join hua tha)"
            )
            found.append((c["name"], c["_id"], joined))
        except Exception:
            continue  # not a member, or bot can't see (not admin there)
        await asyncio.sleep(0.1)

    if not found:
        await status_msg.edit_text(f"User <code>{user_id}</code> kisi bhi linked channel/group me nahi mila.", parse_mode=ParseMode.HTML)
        return

    lines = [f"• {name} — joined: {joined}" for name, _, joined in found]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Remove from ALL", callback_data=f"rmall_{user_id}")
    ]])
    await status_msg.edit_text(
        f"<b>User {user_id} found in:</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^rmall_(\d+)$"))
async def remove_from_all(client, query: CallbackQuery):
    if query.from_user.id != OWNER_ID:
        await query.answer("Sirf owner kar sakta hai.", show_alert=True)
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
        f"✅ User <code>{user_id}</code> removed (permanent ban) from {removed} chats. Failed: {failed}",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /kickall <chat_id> — permanently remove every member from one linked chat
# ---------------------------------------------------------------------------
@app.on_message(filters.command("kickall") & filters.private & filters.user(OWNER_ID))
async def admin_kickall_start(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /kickall <chat_id>\n\n/listchannels se ID copy kar lo.")
        return
    chat_id = int(parts[1].strip())
    chat = channels_col.find_one({"_id": chat_id})
    if not chat:
        await message.reply_text("Ye chat linked nahi hai. Pehle /addchannel se link karo.")
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Haan, PERMANENTLY remove sabko", callback_data=f"kickall_confirm_{chat_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="kickall_cancel"),
    ]])
    await message.reply_text(
        f"⚠️ Ye <b>{chat['name']}</b> ke SABHI members (admins/owner ke alawa) "
        f"<b>permanently</b> ban kar dega. Ye undo nahi hoga khud-ba-khud "
        f"(aapko manually unban karna padega agar kisi ko wapas lena ho). Pakka?",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^kickall_cancel$"))
async def kickall_cancel(client, query: CallbackQuery):
    if query.from_user.id != OWNER_ID:
        await query.answer()
        return
    await query.answer("Cancel ho gaya.")
    await query.message.edit_text("❌ Cancel kar diya, koi member remove nahi hua.")


@app.on_callback_query(filters.regex(r"^kickall_confirm_(-?\d+)$"))
async def kickall_confirm(client, query: CallbackQuery):
    if query.from_user.id != OWNER_ID:
        await query.answer("Sirf owner kar sakta hai.", show_alert=True)
        return
    chat_id = int(query.data.split("_", 2)[2])
    await query.answer()
    status_msg = await query.message.edit_text("🔄 Members list nikaal raha hoon...")

    try:
        members = []
        async for m in client.get_chat_members(chat_id):
            if m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                continue
            members.append(m.user.id)
    except Exception as e:
        await status_msg.edit_text(f"❌ Members list nahi mil payi: {e}")
        return

    if not members:
        await status_msg.edit_text("Koi removable member nahi mila (sab admin/owner hain).")
        return

    removed, failed = 0, 0
    for i, uid in enumerate(members):
        try:
            await client.ban_chat_member(chat_id, uid)  # permanent — no unban
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
                await status_msg.edit_text(f"🔄 Removing... {i + 1}/{len(members)}")
            except Exception:
                pass

    await status_msg.edit_text(f"✅ Done.\nPermanently removed: {removed}\nFailed: {failed}")


# ---------------------------------------------------------------------------
# /purge <chat_id> — bulk-delete all video/document/photo/audio messages
# ---------------------------------------------------------------------------
@app.on_message(filters.command("purge") & filters.private & filters.user(OWNER_ID))
async def admin_purge_start(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /purge <chat_id>\n\n/listchannels se ID copy kar lo.")
        return
    chat_id = int(parts[1].strip())
    chat = channels_col.find_one({"_id": chat_id})
    if not chat:
        await message.reply_text("Ye chat linked nahi hai. Pehle /addchannel se link karo.")
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Haan, saari media files delete karo", callback_data=f"purge_confirm_{chat_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="purge_cancel"),
    ]])
    await message.reply_text(
        f"⚠️ Ye <b>{chat['name']}</b> se saari <b>video, document, photo, aur audio</b> "
        f"messages delete kar dega (text messages chhod ke). Ye undo NAHI ho sakta. Pakka?",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^purge_cancel$"))
async def purge_cancel(client, query: CallbackQuery):
    if query.from_user.id != OWNER_ID:
        await query.answer()
        return
    await query.answer("Cancel ho gaya.")
    await query.message.edit_text("❌ Cancel kar diya, koi file delete nahi hui.")


@app.on_callback_query(filters.regex(r"^purge_confirm_(-?\d+)$"))
async def purge_confirm(client, query: CallbackQuery):
    if query.from_user.id != OWNER_ID:
        await query.answer("Sirf owner kar sakta hai.", show_alert=True)
        return
    chat_id = int(query.data.split("_", 2)[2])
    await query.answer()
    status_msg = await query.message.edit_text("🔄 Media messages dhoond raha hoon...")

    try:
        ids_to_delete = []
        async for msg in client.get_chat_history(chat_id):
            if msg.video or msg.document or msg.photo or msg.audio:
                ids_to_delete.append(msg.id)
    except Exception as e:
        await status_msg.edit_text(f"❌ Chat history nahi mili: {e}")
        return

    if not ids_to_delete:
        await status_msg.edit_text("Koi video/document/photo/audio file nahi mili.")
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
            await status_msg.edit_text(f"🔄 Deleting... {deleted}/{len(ids_to_delete)}")
        except Exception:
            pass

    await status_msg.edit_text(f"✅ Done. Deleted {deleted}/{len(ids_to_delete)} media files.")


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@app.on_message(filters.command("start") & filters.private & filters.user(OWNER_ID))
async def admin_start(client, message: Message):
    await message.reply_text(
        "<b>🛡️ Admin Control Bot</b>\n\n"
        "<b>Setup:</b>\n"
        "/addchannel — Link a channel/group (forward a message from it)\n"
        "/donechannels — Finish linking channels/groups\n"
        "/listchannels — Show all linked channels/groups\n\n"
        "<b>User lookup:</b>\n"
        "/checkuser &lt;id&gt; — Find which linked chats a user is in, since when, with a Remove-from-All button\n\n"
        "<b>Bulk actions:</b>\n"
        "/kickall &lt;chat_id&gt; — Permanently remove ALL members from one chat\n"
        "/purge &lt;chat_id&gt; — Delete ALL video/document/photo/audio messages from one chat",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Admin Control Bot starting...")
    app.run()
