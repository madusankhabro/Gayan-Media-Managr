import os
import re
import sqlite3
import asyncio
from contextlib import closing
from datetime import timedelta
from typing import Optional, Set, Dict, Any

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

DB_PATH = os.getenv("DB_PATH", "settings.db")

# TOKEN should be set in Railway Variables (Environment Variables)
TOKEN = os.getenv("TOKEN", "").strip()

# Default settings (used if a group has no saved settings yet)
DEFAULT_TTL = int(os.getenv("DEFAULT_TTL", "300"))  # seconds
DEFAULT_DELETE_ADMINS = os.getenv("DELETE_ADMINS", "false").lower() in ("1", "true", "yes")
DEFAULT_ENABLED = os.getenv("ENABLED", "true").lower() in ("1", "true", "yes")

# Comma-separated list: photo, video, document, voice, sticker, animation, video_note
DEFAULT_TYPES: Set[str] = set(
    t.strip().lower()
    for t in os.getenv("MEDIA_TYPES", "photo,video,document").split(",")
    if t.strip()
)

VALID_TYPES: Set[str] = {"photo", "video", "document", "voice", "sticker", "animation", "video_note"}


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                ttl_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                delete_admins INTEGER NOT NULL,
                media_types TEXT NOT NULL
            )
            """
        )
        conn.commit()


def get_settings(chat_id: int) -> Dict[str, Any]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT ttl_seconds, enabled, delete_admins, media_types FROM group_settings WHERE chat_id=?",
            (chat_id,),
        ).fetchone()

    if not row:
        return {
            "ttl": DEFAULT_TTL,
            "enabled": DEFAULT_ENABLED,
            "delete_admins": DEFAULT_DELETE_ADMINS,
            "types": set(DEFAULT_TYPES),
        }

    ttl, enabled, delete_admins, media_types = row
    types = set(t.strip() for t in (media_types or "").split(",") if t.strip())
    return {
        "ttl": int(ttl),
        "enabled": bool(enabled),
        "delete_admins": bool(delete_admins),
        "types": types,
    }


def save_settings(chat_id: int, ttl: int, enabled: bool, delete_admins: bool, types: Set[str]) -> None:
    types_str = ",".join(sorted(types))
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO group_settings (chat_id, ttl_seconds, enabled, delete_admins, media_types)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                ttl_seconds=excluded.ttl_seconds,
                enabled=excluded.enabled,
                delete_admins=excluded.delete_admins,
                media_types=excluded.media_types
            """,
            (chat_id, int(ttl), int(enabled), int(delete_admins), types_str),
        )
        conn.commit()


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await is_admin(update, context):
        if update.message:
            await update.message.reply_text("❌ මේ command එක admins ලට විතරයි.")
        return False
    return True


def parse_seconds(arg: str) -> int:
    """
    Accept:
      - pure seconds: 300
      - 10m, 2h, 1d (m=minutes, h=hours, d=days)
    """
    arg = arg.strip().lower()
    if re.fullmatch(r"\d+", arg):
        return int(arg)

    m = re.fullmatch(r"(\d+)([mhd])", arg)
    if not m:
        raise ValueError("bad format")

    val = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return val * 60
    if unit == "h":
        return val * 3600
    if unit == "d":
        return val * 86400
    raise ValueError("bad unit")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "✅ Auto-delete bot active.\n"
            "Admins commands:\n"
            "/setttl <seconds|10m|2h|1d>\n"
            "/types <photo,video,document,...>\n"
            "/pause | /resume\n"
            "/deleteadmins on|off\n"
            "/status"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    s = get_settings(chat.id)
    await update.message.reply_text(
        "📌 Current settings:\n"
        f"- Enabled: {s['enabled']}\n"
        f"- TTL: {s['ttl']} seconds ({str(timedelta(seconds=s['ttl']))})\n"
        f"- Delete admins: {s['delete_admins']}\n"
        f"- Media types: {', '.join(sorted(s['types'])) if s['types'] else '(none)'}"
    )


async def cmd_setttl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setttl 300  OR  /setttl 10m  OR  /setttl 2h")
        return
    try:
        ttl = parse_seconds(context.args[0])
        if ttl < 10 or ttl > 7 * 86400:
            await update.message.reply_text("TTL range: 10 seconds සිට 7 days දක්වා.")
            return
    except Exception:
        await update.message.reply_text("❌ Format වැරදි. Example: /setttl 300, /setttl 10m, /setttl 2h")
        return

    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    save_settings(chat_id, ttl, s["enabled"], s["delete_admins"], s["types"])
    await update.message.reply_text(f"✅ TTL set to {ttl} seconds.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    save_settings(chat_id, s["ttl"], False, s["delete_admins"], s["types"])
    await update.message.reply_text("⏸️ Auto-delete paused for this group.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    save_settings(chat_id, s["ttl"], True, s["delete_admins"], s["types"])
    await update.message.reply_text("▶️ Auto-delete resumed for this group.")


async def cmd_deleteadmins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /deleteadmins on  OR  /deleteadmins off")
        return
    val = context.args[0].lower() == "on"
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    save_settings(chat_id, s["ttl"], s["enabled"], val, s["types"])
    await update.message.reply_text(f"✅ Delete admins set to {val}.")


async def cmd_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /types photo,video,document\n"
            f"Valid: {', '.join(sorted(VALID_TYPES))}"
        )
        return

    raw = " ".join(context.args).replace(" ", "")
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    types = set(parts)

    bad = [t for t in types if t not in VALID_TYPES]
    if bad:
        await update.message.reply_text(
            f"❌ Invalid types: {', '.join(bad)}\n"
            f"Valid: {', '.join(sorted(VALID_TYPES))}"
        )
        return

    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    save_settings(chat_id, s["ttl"], s["enabled"], s["delete_admins"], types)
    await update.message.reply_text(
        f"✅ Media types set: {', '.join(sorted(types)) if types else '(none)'}"
    )


def detect_media_type(message) -> Optional[str]:
    # ✅ Python 3.8+ compatible typing (fix for "str | None" SyntaxError on older Python)
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.voice:
        return "voice"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "animation"
    if message.video_note:
        return "video_note"
    return None


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return

    s = get_settings(chat.id)
    if not s["enabled"]:
        return

    media_type = detect_media_type(msg)
    if not media_type or media_type not in s["types"]:
        return

    # If delete_admins is False, skip deleting messages from admins/owner
    if not s["delete_admins"] and msg.from_user:
        member = await context.bot.get_chat_member(chat.id, msg.from_user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return

    await asyncio.sleep(int(s["ttl"]))
    try:
        await msg.delete()
    except Exception:
        # Missing permissions / can't delete / message already gone / etc.
        pass


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN env var is missing. Set TOKEN in Railway Variables.")

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setttl", cmd_setttl))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("deleteadmins", cmd_deleteadmins))
    app.add_handler(CommandHandler("types", cmd_types))

    media_filter = (
        filters.PHOTO
        | filters.VIDEO
        | filters.Document.ALL
        | filters.VOICE
        | filters.Sticker.ALL
        | filters.ANIMATION
        | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(media_filter, handle_media))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
