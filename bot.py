import os
import re
import time
import random
import logging
import asyncio
import unicodedata
import pymongo
from datetime import datetime
from zoneinfo import ZoneInfo
from pyrogram import Client, filters
from pyrogram.raw import functions
from pyrogram.raw.types import InputChannel
from pyrogram.enums import ParseMode, ChatType, ChatMemberStatus
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from pyrogram.errors import FloodWait

IST = ZoneInfo("Asia/Kolkata")

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
forward_pairs_col = db["forward_pairs"]  # {_id: "target_dest", target, destination, dest_type, dest_name}
topic_map_col = db["topic_map"]      # {_id: "groupid_normalizedtopic", thread_id, title}
fwd_index_col = db["fwd_index"]      # {_id: auto, destination, topic, dest_msg_id, dest_chat_id, dest_username, thread_id, ts}
schedules_col = db["schedules"]      # {_id: auto, message, post_time, channels, channel_names, active, last_posted, last_posted_date}


def build_deep_link(chat_id, username, msg_id):
    if username:
        return f"https://t.me/{username}/{msg_id}"
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    elif cid.startswith("-"):
        cid = cid[1:]
    return f"https://t.me/c/{cid}/{msg_id}"


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
# Auto-Forward: copies posts from a Source channel to a Channel or a Group
# (in the matching Topic, auto-created from the caption's 'Topic:' line)
# ---------------------------------------------------------------------------
# Genuine Unicode "small-caps" letters (e.g. 'ᴛᴏᴘɪᴄ') are a different
# Unicode block than the "Mathematical Alphanumeric" styled fonts and are
# NOT covered by NFKC normalization — map them to plain ASCII separately.
_SMALL_CAPS_MAP = str.maketrans("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ", "abcdefghijklmnopqrstuvwxyz")


def extract_topic_name(caption):
    """Reads a 'Topic : X' line from the caption (case-insensitive). NFKC
    normalization handles 'Mathematical Alphanumeric' styled fonts (e.g.
    bold/sans), and a separate small-caps map handles fonts like 'ᴛᴏᴘɪᴄ:'
    — together these recognize the label regardless of font styling,
    without altering non-Latin scripts like Hindi."""
    if not caption or not caption.strip():
        return None
    for raw_line in caption.strip().split("\n"):
        line = unicodedata.normalize("NFKC", raw_line.strip())
        check_line = line.translate(_SMALL_CAPS_MAP)
        m = re.match(r"(?i)^(?:topic|विषय)\s*[:：]", check_line)
        if m:
            # str.translate() is a 1:1 character replacement, so the colon
            # sits at the same index in both strings — split the ORIGINAL
            # line there so the topic value keeps its original styling.
            return line[m.end():].strip()
    return None


async def get_or_create_forward_topic(client, group_id, topic_name):
    """Returns the forum-topic thread id for this topic name in this group,
    creating it the first time it's seen. Lookup key is normalized
    (trimmed + collapsed whitespace + casefolded) so minor formatting
    differences never create duplicate topics for the same name."""
    key = " ".join(topic_name.strip().split()).casefold()
    doc = topic_map_col.find_one({"_id": f"{group_id}_{key}"})
    if doc:
        return doc["thread_id"]

    display_title = " ".join(topic_name.strip().split())[:128]
    peer = await client.resolve_peer(int(group_id))
    channel = InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
    result = await client.invoke(
        functions.channels.CreateForumTopic(
            channel=channel,
            title=display_title,
            random_id=random.randint(0, 0x7FFFFFFFFFFFFFFF),
        )
    )
    thread_id = None
    for upd in getattr(result, "updates", []):
        msg = getattr(upd, "message", None)
        if msg is not None:
            thread_id = msg.id
            break
    if thread_id is None:
        raise RuntimeError(f"Could not determine new topic id from: {result}")

    topic_map_col.update_one(
        {"_id": f"{group_id}_{key}"},
        {"$set": {"thread_id": thread_id, "title": display_title}},
        upsert=True,
    )
    return thread_id


async def _resolve_chat_for_setup(client, message: Message):
    """Accepts either a forwarded message (preferred) or a typed numeric
    chat ID (fallback, for cases where a channel's settings strip forward
    metadata). Returns a Chat object, or None if neither worked."""
    if message.forward_from_chat:
        return message.forward_from_chat
    if message.text and not message.text.startswith("/"):
        text = message.text.strip()
        if text.lstrip("-").isdigit():
            try:
                return await client.get_chat(int(text))
            except Exception as e:
                logger.warning(f"Couldn't resolve typed chat ID {text}: {e}")
                return None
    return None


@app.on_message(filters.command("addforward") & filters.private & admin_filter)
async def admin_addforward_start(client, message: Message):
    set_state("fwd_step", "target")
    set_state("fwd_pending_target", None)
    await message.reply_text(
        f"<b>{bold_style('Add Forward Pair')}</b>\n\n"
        "Step 1 of 2: where should content be delivered — the TARGET "
        "(this can be a channel or a Topics-enabled group)?\n\n"
        "Forward any message from it (recommended), or send its numeric "
        "chat ID directly."
    )


@app.on_message(filters.private & (filters.forwarded | filters.text) & admin_filter)
async def admin_forward_setup(client, message: Message):
    step = get_state("fwd_step")
    if not step:
        return  # not currently in the /addforward setup flow

    chat = await _resolve_chat_for_setup(client, message)
    if not chat or chat.type not in (ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.GROUP):
        await message.reply_text(
            "Couldn't recognize that as a channel/group. Forward a message from "
            "it directly, or double-check the numeric ID and try again."
        )
        return

    if step == "target":
        set_state("fwd_pending_target", chat.id)
        set_state("fwd_target_type", str(chat.type))
        set_state("fwd_pending_target_name", chat.title)
        set_state("fwd_step", "source")
        await message.reply_text(
            f"Target set: {chat.title}\n\n"
            f"Step 2 of 2: forward a message from (or send the numeric ID of) the "
            f"SOURCE channel — where you'll upload the original content."
        )
        return

    if step == "source":
        target_id = get_state("fwd_pending_target")
        target_type = get_state("fwd_target_type")
        dest_type = "group" if target_type in (str(ChatType.SUPERGROUP), str(ChatType.GROUP)) else "channel"
        pair_id = f"{chat.id}_{target_id}"
        forward_pairs_col.update_one(
            {"_id": pair_id},
            {"$set": {
                "source": chat.id, "destination": target_id,
                "dest_type": dest_type, "dest_name": get_state("fwd_pending_target_name") or str(target_id),
            }},
            upsert=True,
        )
        set_state("fwd_step", None)
        set_state("fwd_pending_target", None)
        mode_label = "Group — auto-topics from caption" if dest_type == "group" else "Channel"
        note = (
            "\n\nMake sure Topics is turned on in that group's settings, and "
            "the bot has Manage Topics permission there." if dest_type == "group" else ""
        )
        await message.reply_text(
            f"✅ Forward pair created.\n\nSource: {chat.title}\nMode: {mode_label}{note}"
        )
        return


@app.on_message(filters.command("listforwards") & filters.private & admin_filter)
async def admin_listforwards(client, message: Message):
    pairs = list(forward_pairs_col.find())
    if not pairs:
        await message.reply_text("No forward pairs set up yet. Use /addforward.")
        return
    lines = []
    for p in pairs:
        mode = "Group (topics)" if p["dest_type"] == "group" else "Channel"
        lines.append(f"• <code>{p['source']}</code> → {p.get('dest_name', p['destination'])} ({mode})")
    await message.reply_text(
        f"<b>{bold_style('Forward Pairs')}</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("removeforward") & filters.private & admin_filter)
async def admin_removeforward(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        await message.reply_text("Usage: /removeforward <source_id> <destination_id>\n\nSee IDs with /listforwards.")
        return
    ids = parts[1].split()
    if len(ids) != 2:
        await message.reply_text("Usage: /removeforward <source_id> <destination_id>")
        return
    pair_id = f"{ids[0]}_{ids[1]}"
    result = forward_pairs_col.delete_one({"_id": pair_id})
    await message.reply_text("✅ Removed." if result.deleted_count else "❌ Pair not found.")


@app.on_message(filters.channel & filters.incoming)
async def handle_forward_source_post(client, message: Message):
    pairs = list(forward_pairs_col.find({"source": message.chat.id}))
    if not pairs:
        return

    caption = message.caption or message.text or ""
    topic_name = extract_topic_name(caption)

    for pair in pairs:
        dest_id = pair["destination"]
        thread_id = None
        sent_msg = None
        try:
            if pair["dest_type"] == "channel":
                sent_msg = await client.copy_message(dest_id, message.chat.id, message.id)
            else:
                thread_id = await get_or_create_forward_topic(client, dest_id, topic_name or "General")
                sent_msg = await client.copy_message(dest_id, message.chat.id, message.id, reply_to_message_id=thread_id)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                if pair["dest_type"] == "channel":
                    sent_msg = await client.copy_message(dest_id, message.chat.id, message.id)
                else:
                    sent_msg = await client.copy_message(dest_id, message.chat.id, message.id, reply_to_message_id=thread_id)
            except Exception as e2:
                logger.error(f"Forward retry failed for pair {pair['_id']}: {e2}")
        except Exception as e:
            logger.error(f"Forward failed for pair {pair['_id']}: {e}")

        if sent_msg:
            fwd_index_col.insert_one({
                "destination": dest_id,
                "topic": topic_name or "Untitled",
                "dest_msg_id": sent_msg.id,
                "dest_chat_id": sent_msg.chat.id,
                "dest_username": sent_msg.chat.username,
                "thread_id": thread_id,
                "ts": time.time(),
            })


# ---------------------------------------------------------------------------
# /genindex <destination_id> - post an index of forwarded content
# Channel: ONE consolidated index at the end, listing every distinct topic
# Group: a separate index posted INSIDE each topic, listing that topic's items
# ---------------------------------------------------------------------------
def _norm(s):
    return " ".join((s or "").strip().split()).casefold()


@app.on_message(filters.command("genindex") & filters.private & admin_filter)
async def admin_genindex(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply_text("Usage: /genindex <destination_id>\n\nSee IDs with /listforwards.")
        return
    dest_id = int(parts[1].strip())
    entries = list(fwd_index_col.find({"destination": dest_id}).sort("ts", 1))
    if not entries:
        await message.reply_text("No forwarded items found for this destination yet.")
        return

    pair = forward_pairs_col.find_one({"destination": dest_id})
    dest_type = pair["dest_type"] if pair else "channel"

    if dest_type == "channel":
        lines = [f"<b>{bold_style('Topics Covered')}</b>"]
        counter = 0
        last_identity = None
        last_group_norm = None
        for e in entries:
            topic = e["topic"]
            group, child = (None, topic)
            if "→" in topic:
                group, child = [p.strip() for p in topic.split("→", 1)]
            identity = (_norm(group), _norm(child))
            if identity == last_identity:
                continue
            last_identity = identity
            url = build_deep_link(e["dest_chat_id"], e["dest_username"], e["dest_msg_id"])
            counter += 1
            if group:
                if _norm(group) != last_group_norm:
                    lines.append(f"\n<blockquote>{group}</blockquote>")
                lines.append(f"    {counter:02d}. <a href=\"{url}\">{child}</a>")
                last_group_norm = _norm(group)
            else:
                last_group_norm = None
                lines.append(f"\n{counter:02d}. <a href=\"{url}\">{child}</a>")
        try:
            await client.send_message(dest_id, "\n".join(lines), parse_mode=ParseMode.HTML)
            await message.reply_text("✅ Index posted at the end of the channel.")
        except Exception as e:
            await message.reply_text(f"❌ Failed to post index: {e}")
        return

    # Group mode: one index per topic, posted inside that topic
    by_topic = {}
    for e in entries:
        by_topic.setdefault(e["thread_id"], []).append(e)

    posted, failed = 0, 0
    for thread_id, items in by_topic.items():
        lines = [f"<b>{bold_style('Index')}</b>"]
        for i, e in enumerate(items, 1):
            url = build_deep_link(e["dest_chat_id"], e["dest_username"], e["dest_msg_id"])
            lines.append(f"{i:02d}. <a href=\"{url}\">{e['topic']}</a>")
        try:
            await client.send_message(
                dest_id, "\n".join(lines), parse_mode=ParseMode.HTML,
                reply_to_message_id=thread_id,
            )
            posted += 1
        except Exception as e:
            logger.error(f"Failed to post topic index for thread {thread_id}: {e}")
            failed += 1
        await asyncio.sleep(0.3)

    await message.reply_text(f"✅ Posted {posted} topic index message(s). Failed: {failed}")


# ---------------------------------------------------------------------------
# Daily Scheduler: step-by-step wizard (message -> time -> channels -> start)
# Posts the same message daily at a fixed time (IST), deleting yesterday's
# copy first so each channel only ever shows the latest one.
# ---------------------------------------------------------------------------
@app.on_message(filters.command("addschedule") & filters.private & admin_filter)
async def sched_start(client, message: Message):
    set_state("sched_step", "message")
    set_state("sched_pending_message", None)
    set_state("sched_pending_time", None)
    set_state("sched_pending_channels", [])
    set_state("sched_pending_channel_names", {})
    await message.reply_text(
        f"<b>{bold_style('New Daily Schedule')}</b>\n\n"
        "Step 1 of 3: send me the message you want posted daily."
    )


@app.on_message(filters.private & filters.text & admin_filter)
async def sched_text_steps(client, message: Message):
    step = get_state("sched_step")
    if step not in ("message", "time"):
        return  # not currently in this part of the wizard
    if message.text.startswith("/"):
        return  # let actual commands through

    if step == "message":
        set_state("sched_pending_message", message.text)
        set_state("sched_step", "time")
        await message.reply_text(
            "Step 2 of 3: what time should this post daily? Send it in 24-hour "
            "HH:MM format, IST — for example 09:00 or 18:30."
        )
        return

    if step == "time":
        text = message.text.strip()
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", text):
            await message.reply_text("That doesn't look like a valid time. Please send it as HH:MM (24-hour), e.g. 09:00.")
            return
        set_state("sched_pending_time", text)
        set_state("sched_step", "channels")
        await message.reply_text(
            f"Time set: {text} IST\n\n"
            f"Step 3 of 3: forward a message from each channel you want this "
            f"posted to (one at a time). Once at least one is added, a Start "
            f"button will appear."
        )
        return


@app.on_message(filters.private & filters.forwarded & admin_filter)
async def sched_channel_step(client, message: Message):
    if get_state("sched_step") != "channels":
        return  # not currently in this part of the wizard

    chat = message.forward_from_chat
    if not chat or chat.type not in (ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.GROUP):
        await message.reply_text("That doesn't look like a channel/group message. Please forward directly from it.")
        return

    channels = get_state("sched_pending_channels", [])
    names = get_state("sched_pending_channel_names", {})
    if chat.id not in channels:
        channels.append(chat.id)
        names[str(chat.id)] = chat.title
        set_state("sched_pending_channels", channels)
        set_state("sched_pending_channel_names", names)

    listed = "\n".join(f"• {names[str(c)]}" for c in channels)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start Schedule", callback_data="sched_go")]])
    await message.reply_text(
        f"Added: {chat.title}\n\n<b>Channels so far:</b>\n{listed}\n\n"
        f"Forward another channel, or press Start when you're done.",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^sched_go$"))
async def sched_go(client, query: CallbackQuery):
    if not is_bot_admin(query.from_user.id):
        await query.answer("Only an admin can do this.", show_alert=True)
        return

    msg_text = get_state("sched_pending_message")
    post_time = get_state("sched_pending_time")
    channels = get_state("sched_pending_channels", [])
    names = get_state("sched_pending_channel_names", {})

    if not msg_text or not post_time or not channels:
        await query.answer("Something's missing — please start over with /addschedule.", show_alert=True)
        return

    schedule_id = f"sched_{int(time.time())}"
    schedules_col.insert_one({
        "_id": schedule_id,
        "message": msg_text,
        "post_time": post_time,
        "channels": channels,
        "channel_names": names,
        "active": True,
        "last_posted": {},
        "last_posted_date": None,
    })

    set_state("sched_step", None)
    set_state("sched_pending_message", None)
    set_state("sched_pending_time", None)
    set_state("sched_pending_channels", [])
    set_state("sched_pending_channel_names", {})

    await query.answer("Schedule started!")
    await query.message.edit_text(
        f"✅ <b>Schedule active</b> (ID: <code>{schedule_id}</code>)\n\n"
        f"Posts daily at {post_time} IST to {len(channels)} channel(s).\n\n"
        f"Send /addschedule again to set up the next one.",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("listschedules") & filters.private & admin_filter)
async def sched_list(client, message: Message):
    schedules = list(schedules_col.find())
    if not schedules:
        await message.reply_text("No schedules yet. Use /addschedule to create one.")
        return
    lines = []
    for s in schedules:
        status = "🟢 active" if s.get("active") else "⏸️ paused"
        preview = s["message"][:40] + ("..." if len(s["message"]) > 40 else "")
        lines.append(
            f"<code>{s['_id']}</code> — {s['post_time']} IST — {status}\n"
            f"  \"{preview}\" → {len(s['channels'])} channel(s)"
        )
    await message.reply_text(
        f"<b>{bold_style('Daily Schedules')}</b>\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("editmessage") & filters.private & admin_filter)
async def sched_editmessage_start(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /editmessage <schedule_id>\n\nSee IDs with /listschedules.")
        return
    schedule_id = parts[1].strip()
    if not schedules_col.find_one({"_id": schedule_id}):
        await message.reply_text("No schedule found with that ID.")
        return
    set_state("sched_editing_id", schedule_id)
    await message.reply_text("Send me the new message text for this schedule.")


@app.on_message(filters.private & filters.text & admin_filter)
async def sched_editmessage_apply(client, message: Message):
    schedule_id = get_state("sched_editing_id")
    if not schedule_id or message.text.startswith("/"):
        return
    schedules_col.update_one({"_id": schedule_id}, {"$set": {"message": message.text}})
    set_state("sched_editing_id", None)
    await message.reply_text(f"✅ Message updated for <code>{schedule_id}</code>.", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("removeschedule") & filters.private & admin_filter)
async def sched_remove(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /removeschedule <schedule_id>")
        return
    result = schedules_col.delete_one({"_id": parts[1].strip()})
    await message.reply_text("✅ Removed." if result.deleted_count else "❌ Schedule not found.")


@app.on_message(filters.command("pauseschedule") & filters.private & admin_filter)
async def sched_pause(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /pauseschedule <schedule_id>")
        return
    result = schedules_col.update_one({"_id": parts[1].strip()}, {"$set": {"active": False}})
    await message.reply_text("✅ Paused." if result.matched_count else "❌ Schedule not found.")


@app.on_message(filters.command("resumeschedule") & filters.private & admin_filter)
async def sched_resume(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /resumeschedule <schedule_id>")
        return
    result = schedules_col.update_one({"_id": parts[1].strip()}, {"$set": {"active": True}})
    await message.reply_text("✅ Resumed." if result.matched_count else "❌ Schedule not found.")


async def scheduler_loop(client: Client):
    """Runs forever in the background, checking once a minute whether any
    active schedule's post time has arrived. When it has: delete yesterday's
    message in each of its channels (if any), then post today's."""
    while True:
        try:
            now = datetime.now(IST)
            current_hhmm = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")

            for sched in schedules_col.find({"active": True, "post_time": current_hhmm}):
                if sched.get("last_posted_date") == today_str:
                    continue  # already posted today, don't double-post

                last_posted = sched.get("last_posted", {})
                new_last_posted = {}
                for chat_id in sched["channels"]:
                    key = str(chat_id)
                    old_msg_id = last_posted.get(key)
                    if old_msg_id:
                        try:
                            await client.delete_messages(chat_id, old_msg_id)
                        except Exception as e:
                            logger.warning(f"Couldn't delete yesterday's message in {chat_id}: {e}")
                    try:
                        sent = await client.send_message(chat_id, sched["message"])
                        new_last_posted[key] = sent.id
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                        try:
                            sent = await client.send_message(chat_id, sched["message"])
                            new_last_posted[key] = sent.id
                        except Exception as e2:
                            logger.error(f"Scheduled post failed for {chat_id}: {e2}")
                    except Exception as e:
                        logger.error(f"Scheduled post failed for {chat_id}: {e}")

                schedules_col.update_one(
                    {"_id": sched["_id"]},
                    {"$set": {"last_posted": new_last_posted, "last_posted_date": today_str}},
                )
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(30)


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
        "/purge &lt;chat_id&gt; - Delete messages from one chat (choose: media files only, or everything)\n\n"
        f"<b>{bold_style('Auto-Forward')}</b>\n"
        "/addforward - Link a Source channel to a Channel or Topics-group destination\n"
        "/listforwards - Show all forward pairs\n"
        "/removeforward - Remove a forward pair\n"
        "/genindex &lt;destination_id&gt; - Post an index (channel: one summary at the end; group: one per topic)\n\n"
        f"<b>{bold_style('Daily Scheduler')}</b>\n"
        "/addschedule - Set up a new daily post (message, time, channels)\n"
        "/listschedules - Show all schedules\n"
        "/editmessage &lt;id&gt; - Change a schedule's message\n"
        "/pauseschedule &lt;id&gt; - Pause a schedule\n"
        "/resumeschedule &lt;id&gt; - Resume a paused schedule\n"
        "/removeschedule &lt;id&gt; - Delete a schedule"
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
from pyrogram import idle


async def main():
    await app.start()
    asyncio.create_task(scheduler_loop(app))
    logger.info("Admin Control Bot starting...")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
