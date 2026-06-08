import os
import json
import logging
import time
import datetime
import sqlite3
import signal
import sys
import threading
import requests
import telebot
import subprocess
from telebot import types
from PIL import Image

# ==========================================
# Environment Variables (Fly.io secrets)
# ==========================================
TOKEN = "8841147509:AAGGm0ydQptJCyQ19fOqjl4V2O14bijKRU8"  # ضع هنا توكن البوت الخاص بك
OWNER_ID = "7115401970"  # ضع هنا آيدي حسابك الشخصي (المشرف)
DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        DATA_DIR = "/data"
    else:
        DATA_DIR = "./data"

DATA_FILE = os.path.join(DATA_DIR, "business_bot_data.json")
DB_CACHE_FILE = os.path.join(DATA_DIR, "messages_cache.db")
MEDIA_ARCHIVE_DIR = os.path.join(DATA_DIR, "media_archive")

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_ARCHIVE_DIR, exist_ok=True)

# ==========================================
# Validation
# ==========================================
if not TOKEN:
    print("FATAL: BOT_TOKEN environment variable is not set!")
    sys.exit(1)

if not OWNER_ID:
    print("FATAL: OWNER_ID environment variable is not set!")
    sys.exit(1)

# ==========================================
# Bot Initialization (Long Polling - no Flask needed)
# ==========================================
bot = telebot.TeleBot(TOKEN, threaded=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, 'bot.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

REPLIED_CHATS = set()
MAX_FETCH_MESSAGES = 200

# Graceful shutdown flag
SHUTDOWN_REQUESTED = False

def signal_handler(sig, frame):
    global SHUTDOWN_REQUESTED
    logger.info(f"Received signal {sig}, initiating graceful shutdown...")
    SHUTDOWN_REQUESTED = True
    bot.stop_polling()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ==========================================
# SQLite Cache Management (Archive for deleted messages)
# ==========================================

def init_db():
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        cursor = conn.cursor()

        # Create table if it doesn't exist (preserves existing data)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS msg_cache (
                key TEXT PRIMARY KEY,
                text TEXT,
                timestamp REAL,
                sender_id INTEGER,
                sender_name TEXT,
                file_id TEXT,
                media_type TEXT,
                caption TEXT,
                local_path TEXT,
                send_date TEXT
            )
        """)

        # Schema migration: add missing columns from old schema without dropping data
        cursor.execute("PRAGMA table_info(msg_cache)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        required_columns = {
            "sender_id": "INTEGER",
            "sender_name": "TEXT",
            "file_id": "TEXT",
            "media_type": "TEXT",
            "caption": "TEXT",
            "local_path": "TEXT",
            "send_date": "TEXT"
        }

        for col_name, col_type in required_columns.items():
            if col_name not in existing_columns:
                logger.info(f"Migrating DB schema: adding column '{col_name}' ({col_type})")
                cursor.execute(f"ALTER TABLE msg_cache ADD COLUMN {col_name} {col_type}")

        conn.commit()
        conn.close()
        logger.info("SQLite DB initialized successfully (cache preserved)")
    except Exception as e:
        logger.error(f"Error initializing SQLite DB: {e}")

def cache_message(chat_id, message_id, text, sender_id=None, sender_name=None, file_id=None, media_type=None, caption=None, local_path=None, send_date=None):
    key = f"{chat_id}_{message_id}"
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO msg_cache 
            (key, text, timestamp, sender_id, sender_name, file_id, media_type, caption, local_path, send_date) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (key, text, time.time(), sender_id, sender_name, file_id, media_type, caption, local_path, send_date))
        
        # Keep 10000 messages inside cache
        cursor.execute("""
            DELETE FROM msg_cache
            WHERE rowid NOT IN (
                SELECT rowid FROM msg_cache ORDER BY timestamp DESC LIMIT 10000
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error caching message in SQLite: {e}")

def update_cached_local_path(chat_id, message_id, local_path):
    """Update the local_path for a cached message after background download completes"""
    key = f"{chat_id}_{message_id}"
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        cursor = conn.cursor()
        cursor.execute("UPDATE msg_cache SET local_path = ? WHERE key = ?", (local_path, key))
        conn.commit()
        conn.close()
        logger.info(f"[Cache] Updated local_path for {key}: {local_path}")
    except Exception as e:
        logger.error(f"Error updating cached local_path: {e}")

def get_cached_message(chat_id, message_id):
    key = f"{chat_id}_{message_id}"
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM msg_cache WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"Error retrieving message from SQLite: {e}")
        return None

def remove_cached_message(chat_id, message_id):
    key = f"{chat_id}_{message_id}"
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM msg_cache WHERE key = ?", (key,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error deleting message from SQLite: {e}")

def get_all_cached_messages_for_chat(chat_id):
    """Get all cached messages for a specific chat_id. Returns list of dicts."""
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM msg_cache WHERE key LIKE ?", (f"{chat_id}_%",))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error retrieving all cached messages for chat {chat_id}: {e}")
        return []

def _split_cache_key(cache_key):
    """Split msg_cache key into chat_id and message_id strings safely."""
    try:
        chat_id, message_id = str(cache_key).rsplit("_", 1)
        return chat_id, message_id
    except Exception:
        return "", ""

def get_cached_messages_for_person(person_id, limit):
    """
    Return latest cached messages for a Telegram user/chat id.

    The bot can only return messages already received through the linked Telegram
    Business connection and stored in msg_cache. It does not fetch private
    Telegram history directly from Telegram servers.

    Search rules:
    - match chat id through the key prefix: <chat_id>_<message_id>
    - match sender_id for messages where this person was the sender
    """
    try:
        person_id = str(person_id).strip()
        safe_limit = max(1, min(int(limit), MAX_FETCH_MESSAGES))

        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM msg_cache
            WHERE key LIKE ? OR CAST(sender_id AS TEXT) = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (f"{person_id}_%", person_id, safe_limit))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        # Send oldest first so the owner reads the conversation in order.
        rows.reverse()
        for row in rows:
            chat_id, message_id = _split_cache_key(row.get("key", ""))
            row["chat_id"] = chat_id
            row["message_id"] = message_id
        return rows
    except Exception as e:
        logger.error(f"Error fetching cached messages for person {person_id}: {e}")
        return []

def _message_row_plain_text(row):
    text = row.get("text") or ""
    caption = row.get("caption") or ""
    media_type = row.get("media_type") or ""
    if text:
        return text
    if caption:
        return caption
    if media_type:
        return f"[{media_type}]"
    return "[رسالة بدون نص]"

def _format_cached_message_row(row, index=None):
    sender_name = row.get("sender_name") or "غير معروف"
    sender_id = row.get("sender_id") or "غير معروف"
    chat_id = row.get("chat_id") or "غير معروف"
    message_id = row.get("message_id") or "غير معروف"
    send_date = row.get("send_date") or "غير متوفر"
    media_type = row.get("media_type") or ""
    body = _message_row_plain_text(row)

    title = f"📩 <b>رسالة {index}</b>" if index is not None else "📩 <b>رسالة</b>"
    parts = [
        title,
        f"👤 <b>المرسل:</b> {escape_html(sender_name)}",
        f"🆔 <b>آيدي المرسل:</b> <code>{escape_html(sender_id)}</code>",
        f"💬 <b>آيدي المحادثة:</b> <code>{escape_html(chat_id)}</code>",
        f"🔢 <b>رقم الرسالة:</b> <code>{escape_html(message_id)}</code>",
        f"📅 <b>التاريخ:</b> <code>{escape_html(send_date)}</code>",
    ]
    if media_type:
        parts.append(f"📦 <b>نوع الوسائط:</b> <code>{escape_html(media_type)}</code>")
    parts.append(f"\n📝 <b>المحتوى:</b>\n{escape_html(body)}")
    return "\n".join(parts)

def _chunk_text_blocks(blocks, max_chars=3500):
    chunks = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n━━━━━━━━━━━━\n\n" + block
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks

def send_cached_messages_to_owner(owner_chat_id, rows):
    """Send fetched cached messages to the owner with media when available."""
    if not rows:
        return

    blocks = []
    media_rows = []
    for idx, row in enumerate(rows, 1):
        blocks.append(_format_cached_message_row(row, idx))
        if row.get("media_type") and (row.get("file_id") or row.get("local_path")):
            media_rows.append((idx, row))

    for chunk in _chunk_text_blocks(blocks):
        safe_bot_call(bot.send_message, owner_chat_id, chunk, parse_mode="HTML")

    if media_rows:
        safe_bot_call(
            bot.send_message, owner_chat_id,
            f"📦 <b>تم العثور على {len(media_rows)} رسالة تحتوي وسائط. سأرسل الوسائط المتاحة الآن.</b>",
            parse_mode="HTML"
        )
        for idx, row in media_rows[:MAX_FETCH_MESSAGES]:
            try:
                sender_display = escape_html(row.get("sender_name") or "غير معروف")
                date_display = escape_html(row.get("send_date") or "غير متوفر")
                header = (
                    f"📎 <b>وسائط الرسالة {idx}</b>\n"
                    f"👤 <b>المرسل:</b> {sender_display}\n"
                    f"📅 <b>التاريخ:</b> <code>{date_display}</code>"
                )
                safe_bot_call(bot.send_message, owner_chat_id, header, parse_mode="HTML")
                send_saved_media(
                    owner_chat_id,
                    row.get("file_id"),
                    row.get("media_type"),
                    row.get("caption") or None,
                    row.get("local_path")
                )
                time.sleep(0.35)
            except Exception as e:
                logger.error(f"Error sending fetched media row {idx}: {e}")
                safe_bot_call(
                    bot.send_message, owner_chat_id,
                    f"⚠️ تعذر إرسال وسائط الرسالة {idx}: <code>{escape_html(str(e))}</code>",
                    parse_mode="HTML"
                )

def remove_all_cached_messages_for_chat(chat_id):
    """Remove all cached messages for a specific chat_id."""
    try:
        conn = sqlite3.connect(DB_CACHE_FILE, timeout=20)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM msg_cache WHERE key LIKE ?", (f"{chat_id}_%",))
        conn.commit()
        conn.close()
        logger.info(f"[Cache] Removed all cached messages for chat {chat_id}")
    except Exception as e:
        logger.error(f"Error removing all cached messages for chat {chat_id}: {e}")

init_db()

# ==========================================
# Utility Functions
# ==========================================

def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def safe_request(url, method="post", payload=None, files=None, timeout=30, stream=False):
    """HTTP request with retry logic - increased timeout for Fly.io"""
    retries = [3, 6, 10]
    attempt = 0
    while True:
        try:
            if attempt > 0 and files:
                for f_val in files.values():
                    if isinstance(f_val, tuple) and len(f_val) >= 2:
                        f_obj = f_val[1]
                        if hasattr(f_obj, 'seek'):
                            try:
                                f_obj.seek(0)
                            except Exception:
                                pass

            if method.lower() == "get":
                response = requests.get(url, params=payload, timeout=timeout, stream=stream)
            else:
                if files:
                    logger.info(f"[Safe Request] POST with files to {url}. Timeout: {timeout}s")
                    response = requests.post(url, data=payload, files=files, timeout=timeout)
                else:
                    response = requests.post(url, json=payload, timeout=timeout)
            if stream:
                return {"ok": True, "response_obj": response}
            return response.json()
        except requests.exceptions.Timeout as e:
            logger.error(f"[Safe Request - TIMEOUT] Attempt {attempt + 1}. Error: {e}")
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                return {"ok": False, "description": "Connection timed out. Try again with a smaller file."}
        except requests.exceptions.ProxyError as e:
            logger.error(f"[Safe Request - PROXY ERROR] Attempt {attempt + 1}. Error: {e}")
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                return {"ok": False, "description": "Proxy connection failed. Please try again later."}
        except (requests.exceptions.RequestException, Exception) as e:
            logger.warning(f"[Safe Request - ERROR] Attempt {attempt + 1}. Error: {e}")
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                logger.error(f"[Safe Request] All retries failed for: {url}")
                return {"ok": False, "description": f"Connection error: {str(e)}"}

def safe_bot_call(func, *args, **kwargs):
    """Bot API call with retry - no proxy issues on Fly.io"""
    retries = [3, 6, 10]
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Bot API call attempt {attempt + 1} failed. Error: {e}")
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                logger.error(f"All Bot API call retries failed for function: {func.__name__}")
                raise e

def delete_messages_sequentially(chat_id, message_ids):
    """Delete messages with shorter delay - Fly.io has no rate limits like PythonAnywhere"""
    for msg_id in message_ids:
        if msg_id:
            try:
                safe_bot_call(bot.delete_message, chat_id, msg_id)
                time.sleep(0.3)  # Faster deletion on Fly.io
            except Exception:
                pass

def download_media_safely(file_id, suffix="", retries=3):
    """Downloads files under 20MB safely with retry logic for deleted message recovery"""
    for attempt in range(retries):
        try:
            logger.info(f"[Media Download] Attempt {attempt + 1}/{retries} for file_id: {file_id[:30]}...")
            file_info = bot.get_file(file_id)
            
            if file_info.file_size and file_info.file_size > 20 * 1024 * 1024:
                logger.info(f"[Media Download] File {file_id[:30]} is too large ({file_info.file_size} bytes > 20MB)")
                return None
            
            file_path = file_info.file_path
            logger.info(f"[Media Download] Got file_path: {file_path}, size: {file_info.file_size} bytes")
            
            downloaded_file = bot.download_file(file_path)
            
            if not downloaded_file:
                logger.warning(f"[Media Download] download_file returned empty for {file_id[:30]}")
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return None
            
            local_filename = f"{file_id}{suffix}"
            local_path = os.path.join(MEDIA_ARCHIVE_DIR, local_filename)
            
            with open(local_path, 'wb') as f:
                f.write(downloaded_file)
            
            file_size_on_disk = os.path.getsize(local_path)
            logger.info(f"[Media Download] SUCCESS! Saved to {local_path} ({file_size_on_disk} bytes)")
            return local_path
            
        except Exception as e:
            logger.error(f"[Media Download] Attempt {attempt + 1}/{retries} FAILED for {file_id[:30]}: {e}")
            if attempt < retries - 1:
                time.sleep(3)
            else:
                logger.error(f"[Media Download] ALL {retries} attempts failed for file_id: {file_id[:30]}")
                return None
    return None

def send_saved_media(owner_user_id, file_id, media_type, caption=None, local_path=None):
    """Sends saved media back to the owner using cached file_id, falling back to local files.
    If caption is provided, uses it as-is (the sender's original caption).
    If caption is None, sends with no caption (the sender didn't include one)."""
    media_labels = {
        'photo': 'صورة', 'video': 'فيديو', 'document': 'ملف',
        'voice': 'رسالة صوتية', 'audio': 'صوت', 'sticker': 'ملصق',
        'animation': 'متحركة', 'video_note': 'ملاحظة فيديو',
        'photo_spoiler': 'صورة مخفية (سبويلر)', 'paid_photo': 'صورة مدفوعة',
        'paid_video': 'فيديو مدفوع', 'paid_media': 'وسائط مدفوعة'
    }
    media_label = media_labels.get(media_type, media_type)
    # Caption handling: use the original caption if it exists, otherwise no caption
    caption_str = caption if caption else None

    logger.info(f"[Media Recovery] Attempting to send {media_type} - file_id: {str(file_id)[:30] if file_id else 'None'} - local_path: {local_path}")

    # Try using Telegram File ID first (extremely fast, no download needed)
    if file_id:
        try:
            if media_type in ("photo", "photo_spoiler", "paid_photo"):
                bot.send_photo(owner_user_id, file_id, caption=caption_str)
            elif media_type in ("video", "paid_video"):
                bot.send_video(owner_user_id, file_id, caption=caption_str)
            elif media_type == "document":
                bot.send_document(owner_user_id, file_id, caption=caption_str)
            elif media_type == "voice":
                bot.send_voice(owner_user_id, file_id, caption=caption_str)
            elif media_type == "audio":
                bot.send_audio(owner_user_id, file_id, caption=caption_str)
            elif media_type == "sticker":
                bot.send_sticker(owner_user_id, file_id)
            elif media_type == "animation":
                bot.send_animation(owner_user_id, file_id, caption=caption_str)
            elif media_type == "video_note":
                bot.send_video_note(owner_user_id, file_id)
            elif media_type == "paid_media":
                # Try sending as document for generic paid media
                bot.send_document(owner_user_id, file_id, caption=caption_str)
            logger.info(f"[Media Recovery] SUCCESS via file_id: {str(file_id)[:30]}")
            return True
        except Exception as e:
            logger.warning(f"[Media Recovery] file_id failed ({e}). Trying local file backup: {local_path}")
    else:
        logger.warning(f"[Media Recovery] No file_id available for {media_type}, trying local file: {local_path}")

    # Fallback to locally archived file on disk
    if local_path and os.path.exists(local_path):
        file_size = os.path.getsize(local_path)
        logger.info(f"[Media Recovery] Local file exists: {local_path} ({file_size} bytes)")
        try:
            with open(local_path, "rb") as f_obj:
                if media_type in ("photo", "photo_spoiler", "paid_photo"):
                    bot.send_photo(owner_user_id, f_obj, caption=caption_str)
                elif media_type in ("video", "paid_video"):
                    bot.send_video(owner_user_id, f_obj, caption=caption_str)
                elif media_type == "document":
                    bot.send_document(owner_user_id, f_obj, caption=caption_str)
                elif media_type == "voice":
                    bot.send_voice(owner_user_id, f_obj, caption=caption_str)
                elif media_type == "audio":
                    bot.send_audio(owner_user_id, f_obj, caption=caption_str)
                elif media_type == "sticker":
                    bot.send_sticker(owner_user_id, f_obj)
                elif media_type == "animation":
                    bot.send_animation(owner_user_id, f_obj, caption=caption_str)
                elif media_type == "video_note":
                    bot.send_video_note(owner_user_id, f_obj)
                elif media_type == "paid_media":
                    bot.send_document(owner_user_id, f_obj, caption=caption_str)
            logger.info(f"[Media Recovery] SUCCESS via local file: {local_path}")
            return True
        except Exception as ex:
            logger.error(f"[Media Recovery] Local file FAILED: {ex}")
            try:
                bot.send_message(owner_user_id, f"⚠️ <b>تعذر استرجاع ملف الوسائط المحذوف.</b>\nالنوع: {media_type}\nالسبب: {escape_html(str(ex))}", parse_mode="HTML")
            except Exception:
                pass
    else:
        logger.warning(f"[Media Recovery] No local backup available. file_id={bool(file_id)}, local_path={local_path}, exists={os.path.exists(local_path) if local_path else False}")
        try:
            bot.send_message(owner_user_id, f"⚠️ <b>لم يتم استرجاع الوسائط المحذوفة.</b>\nالنوع: <code>{media_type}</code>\nالسبب: لم يتم تحميل الملف مسبقاً ولا يوجد نسخة احتياطية.", parse_mode="HTML")
        except Exception:
            pass
    return False

# ==========================================
# Data Persistence (JSON on Fly.io Volume)
# ==========================================

def load_data():
    default_data = {
        "connections": {},
        "connection_details": {},
        "states": {},
        "auto_read": {},
        "auto_reply_enabled": {},
        "auto_reply_text": {},
        "custom_commands": {},
        "activated_users": [OWNER_ID],
        "pending_requests": [],
        "last_story_ids": {},
        "last_bios": {},
        "prompt_message_ids": {},
        "menu_message_ids": {},
        "notes": {},
        "shortcuts": {},
        "temp_triggers": {},
        "fetch_message_targets": {},
        "notify_deletions": {},
        "notify_edits": {},
        "story_bg_type": {},
        "story_active_period": {}
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return default_data
                for key, val in default_data.items():
                    if key not in data:
                        data[key] = val
                return data
        except Exception as e:
            logger.error(f"Error loading JSON data: {e}")
            return default_data
    return default_data

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Error saving JSON data: {e}")

# ==========================================
# User Permission System
# ==========================================

def is_user_allowed(message_or_call):
    user_id = str(message_or_call.from_user.id)
    data = load_data()

    if OWNER_ID not in data["activated_users"]:
        data["activated_users"].append(OWNER_ID)
        save_data(data)

    if user_id in data["activated_users"]:
        return True

    if isinstance(message_or_call, types.Message):
        chat_id = message_or_call.chat.id
        pending = data.get("pending_requests", [])

        if user_id not in pending:
            data["pending_requests"].append(user_id)
            save_data(data)

            safe_bot_call(
                bot.send_message, chat_id,
                "⏳ <b>تم إرسال طلب تفعيل البوت الخاص بك للمشرف بنجاح. يرجى الانتظار حتى يتم قبول طلبك من قبله.</b>",
                parse_mode="HTML"
            )

            name = message_or_call.from_user.first_name
            if message_or_call.from_user.last_name:
                name += f" {message_or_call.from_user.last_name}"
            username = message_or_call.from_user.username
            username_text = f"@{username}" if username else "لا يوجد"
            user_link = f"tg://user?id={user_id}"

            escaped_name = escape_html(name)
            escaped_username = escape_html(username_text)

            notify_text = (
                "🔔 <b>طلب تفعيل جديد للبوت!</b>\n\n"
                f"👤 <b>الاسم:</b> <a href=\"{user_link}\">{escaped_name}</a>\n"
                f"🔗 <b>اليوزر:</b> {escaped_username}\n"
                f"🆔 <b>الآيدي:</b> <code>{user_id}</code>\n\n"
                "هل ترغب في تفعيل البوت لهذا العضو?"
            )

            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_approve = types.InlineKeyboardButton("✅ قبول", callback_data=f"approve_{user_id}")
            btn_decline = types.InlineKeyboardButton("❌ رفض", callback_data=f"decline_{user_id}")
            markup.add(btn_approve, btn_decline)

            safe_bot_call(bot.send_message, OWNER_ID, notify_text, parse_mode="HTML", reply_markup=markup)
    else:
        safe_bot_call(bot.answer_callback_query, message_or_call.id, "⚠️ طلبك قيد الانتظار لموافقة المشرف.", show_alert=True)

    return False

def get_user_commands(data, user_id):
    cmds = data.get("custom_commands", {}).get(user_id, {})
    if "delete" not in cmds: cmds["delete"] = "!delete"
    if "pin" not in cmds: cmds["pin"] = "!pin"
    if "unpin" not in cmds: cmds["unpin"] = "!unpin"
    if "id" not in cmds: cmds["id"] = "!id"
    if "note" not in cmds: cmds["note"] = "!note"
    if "help" not in cmds: cmds["help"] = "!help"
    return cmds

# ==========================================
# Telegram Business API Wrappers
# ==========================================

def api_set_business_name(token, connection_id, first_name, last_name=None):
    url = f"https://api.telegram.org/bot{token}/setBusinessAccountName"
    payload = {"business_connection_id": connection_id, "first_name": first_name}
    if last_name:
        payload["last_name"] = last_name
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_set_business_bio(token, connection_id, bio):
    url = f"https://api.telegram.org/bot{token}/setBusinessAccountBio"
    payload = {"business_connection_id": connection_id, "bio": bio}
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_set_business_username(token, connection_id, username):
    url = f"https://api.telegram.org/bot{token}/setBusinessAccountUsername"
    payload = {"business_connection_id": connection_id, "username": username}
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_get_business_connection(token, connection_id):
    url = f"https://api.telegram.org/bot{token}/getBusinessConnection"
    payload = {"business_connection_id": connection_id}
    return safe_request(url, method="post", payload=payload, timeout=15)

# ==========================================
# Story Media Processing (fix landscape distortion)
# ==========================================
# Telegram Stories require 9:16 aspect ratio (1080x1920).
# Landscape photos/videos get distorted because Telegram stretches them.
# We resize/crop them to the correct 9:16 ratio before uploading.

STORY_WIDTH = 1080
STORY_HEIGHT = 1920

def process_story_photo(input_path, output_path, bg_type="blurred"):
    """Resize and crop a photo to 9:16 aspect ratio for Telegram Stories.
    - If already 9:16, just resize to 1080x1920
    - If landscape or other ratio, crop from center then resize
    - bg_type: "blurred" = blurred background (default), "black" = solid black background"""
    try:
        img = Image.open(input_path)
        img = img.convert("RGBA")
        orig_w, orig_h = img.size
        
        # Target aspect ratio 9:16 = 0.5625
        target_ratio = STORY_WIDTH / STORY_HEIGHT  # 0.5625
        current_ratio = orig_w / orig_h
        
        if abs(current_ratio - target_ratio) < 0.05:
            # Already close to 9:16, just resize
            result = img.resize((STORY_WIDTH, STORY_HEIGHT), Image.LANCZOS)
            result = result.convert("RGB")
            result.save(output_path, "JPEG", quality=95)
            logger.info(f"[Story Photo] Already 9:16 ratio ({orig_w}x{orig_h}), resized to {STORY_WIDTH}x{STORY_HEIGHT}")
            return True
        
        # Landscape or different ratio - need to fit with background
        if bg_type == "black":
            # Solid black background
            bg = Image.new("RGBA", (STORY_WIDTH, STORY_HEIGHT), (0, 0, 0, 255))
            logger.info(f"[Story Photo] Using solid black background")
        elif bg_type == "white":
            # Solid white background
            bg = Image.new("RGBA", (STORY_WIDTH, STORY_HEIGHT), (255, 255, 255, 255))
            logger.info(f"[Story Photo] Using solid white background")
        else:
            # Blurred background (default)
            bg = img.resize((STORY_WIDTH, STORY_HEIGHT), Image.BILINEAR)
            from PIL import ImageFilter
            bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
            bg = bg.convert("RGBA")
            logger.info(f"[Story Photo] Using blurred background")
        
        # Calculate scale to fit the original image within 9:16 while maintaining aspect
        scale_w = STORY_WIDTH / orig_w
        scale_h = STORY_HEIGHT / orig_h
        scale = min(scale_w, scale_h)
        
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        
        # Ensure minimum dimensions
        if new_w < STORY_WIDTH:
            scale = STORY_WIDTH / orig_w
            new_w = STORY_WIDTH
            new_h = int(orig_h * scale)
        if new_h > STORY_HEIGHT:
            scale = STORY_HEIGHT / orig_h
            new_h = STORY_HEIGHT
            new_w = int(orig_w * scale)
        
        # Resize the original image maintaining aspect ratio
        foreground = img.resize((new_w, new_h), Image.LANCZOS)
        
        # Center the foreground on the background
        paste_x = (STORY_WIDTH - new_w) // 2
        paste_y = (STORY_HEIGHT - new_h) // 2
        bg.paste(foreground, (paste_x, paste_y), foreground if foreground.mode == 'RGBA' else None)
        
        result = bg.convert("RGB")
        result.save(output_path, "JPEG", quality=95)
        bg_label = "black" if bg_type == "black" else ("white" if bg_type == "white" else "blurred")
        logger.info(f"[Story Photo] Processed {orig_w}x{orig_h} -> {STORY_WIDTH}x{STORY_HEIGHT} with {bg_label} background")
        return True
    except Exception as e:
        logger.error(f"[Story Photo] Error processing photo: {e}")
        return False

def get_video_metadata(path):
    """Return (width, height, duration_seconds) for a video file, or None values on failure."""
    try:
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "json",
            path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=20)
        if probe_result.returncode != 0:
            logger.warning(f"[Story Video] ffprobe metadata failed: {probe_result.stderr[:300]}")
            return None, None, None
        info = json.loads(probe_result.stdout or "{}")
        streams = info.get("streams") or []
        if not streams:
            return None, None, None
        stream = streams[0]
        width = int(stream.get("width")) if stream.get("width") else None
        height = int(stream.get("height")) if stream.get("height") else None
        duration = None
        if stream.get("duration"):
            try:
                duration = int(round(float(stream.get("duration"))))
            except Exception:
                duration = None
        return width, height, duration
    except Exception as e:
        logger.warning(f"[Story Video] Could not read metadata: {e}")
        return None, None, None

def make_safe_temp_path(prefix, file_id, extension):
    """Create a safe temporary media path under DATA_DIR."""
    safe_id = ''.join(ch for ch in str(file_id) if ch.isalnum() or ch in ('_', '-'))[:80] or 'media'
    stamp = int(time.time() * 1000)
    if not extension.startswith('.'):
        extension = f'.{extension}'
    return os.path.join(DATA_DIR, f"{prefix}_{safe_id}_{stamp}{extension}")

def process_story_video(input_path, output_path, bg_type="blurred"):
    """Prepare video for Telegram Story exactly as a manual upload style:
    - output is always 1080x1920 (9:16), so Telegram will not stretch it
    - original video stays centered with its real aspect ratio
    - bg_type: "blurred" = empty space filled by blurred copy of the same video (default)
    - bg_type: "black" = empty space filled by solid black background
    - encoded as streamable MP4 compatible with Telegram
    """
    try:
        orig_w, orig_h, orig_duration = get_video_metadata(input_path)
        if orig_w and orig_h:
            logger.info(f"[Story Video] Original: {orig_w}x{orig_h}, duration={orig_duration}")
        else:
            logger.info("[Story Video] Original dimensions unavailable, processing anyway")

        if bg_type == "black":
            # Solid black background - use color source instead of blurred copy
            filter_complex = (
                f"color=c=black:s={STORY_WIDTH}x{STORY_HEIGHT}:d=999[black_bg];"
                f"[0:v]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"setsar=1[scaled_fg];"
                f"[black_bg][scaled_fg]overlay=(W-w)/2:(H-h)/2:format=auto:"
                f"eof_action=repeat,"
                f"format=yuv420p[v]"
            )
            logger.info("[Story Video] Using solid black background")
        elif bg_type == "white":
            # Solid white background
            filter_complex = (
                f"color=c=white:s={STORY_WIDTH}x{STORY_HEIGHT}:d=999[white_bg];"
                f"[0:v]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"setsar=1[scaled_fg];"
                f"[white_bg][scaled_fg]overlay=(W-w)/2:(H-h)/2:format=auto:"
                f"eof_action=repeat,"
                f"format=yuv420p[v]"
            )
            logger.info("[Story Video] Using solid white background")
        else:
            # Blurred background (default)
            filter_complex = (
                f"[0:v]split=2[bg][fg];"
                f"[bg]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={STORY_WIDTH}:{STORY_HEIGHT},"
                f"boxblur=luma_radius=20:luma_power=1:chroma_radius=20:chroma_power=1,"
                f"setsar=1[blurred_bg];"
                f"[fg]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"setsar=1[scaled_fg];"
                f"[blurred_bg][scaled_fg]overlay=(W-w)/2:(H-h)/2:format=auto,"
                f"format=yuv420p[v]"
            )
            logger.info("[Story Video] Using blurred background")

        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "1024",
            "-aspect", "9:16",
            "-shortest",
            output_path
        ]

        logger.info(f"[Story Video] Running ffmpeg story normalization (bg={bg_type})...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            logger.error(f"[Story Video] ffmpeg failed: {result.stderr[:1200]}")
            return False

        out_w, out_h, out_duration = get_video_metadata(output_path)
        logger.info(f"[Story Video] Processed -> {out_w}x{out_h}, duration={out_duration}")
        return True
    except subprocess.TimeoutExpired:
        logger.error("[Story Video] ffmpeg timed out")
        return False
    except Exception as e:
        logger.error(f"[Story Video] Error processing video: {e}")
        return False

def api_post_story_with_file(token, connection_id, media_type, file_obj, caption=None, video_width=None, video_height=None, video_duration=None, active_period=86400):
    url = f"https://api.telegram.org/bot{token}/postStory"
    attach_name = "story_media"

    if media_type == "photo":
        content = {"type": "photo", "photo": f"attach://{attach_name}"}
        mime_type = "image/jpeg"
        file_name = "photo.jpg"
        timeout_setting = 60
    else:
        content = {"type": "video", "video": f"attach://{attach_name}"}
        # Include actual video dimensions so Telegram displays it correctly
        # Without width/height, Telegram may stretch the video to fill 9:16
        if video_width and video_height:
            content["width"] = int(video_width)
            content["height"] = int(video_height)
        if video_duration:
            content["duration"] = int(video_duration)
        mime_type = "video/mp4"
        file_name = "video.mp4"
        timeout_setting = 900

    payload = {
        "business_connection_id": connection_id,
        "content": json.dumps(content),
        "active_period": active_period,
        "post_to_chat_page": True
    }
    if caption:
        payload["caption"] = caption

    files = {attach_name: (file_name, file_obj, mime_type)}
    return safe_request(url, method="post", payload=payload, files=files, timeout=timeout_setting)

def api_delete_story(token, connection_id, story_id):
    url = f"https://api.telegram.org/bot{token}/deleteStory"
    payload = {
        "business_connection_id": connection_id,
        "story_id": story_id
    }
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_set_business_profile_photo(token, connection_id, file_bytes, is_video=False):
    url = f"https://api.telegram.org/bot{token}/setBusinessAccountProfilePhoto"
    attach_name = "photo_file"
    if is_video:
        photo_json = {
            "type": "animated",
            "animation": f"attach://{attach_name}"
        }
        files = {
            attach_name: ("video.mp4", file_bytes, "video/mp4")
        }
    else:
        photo_json = {
            "type": "static",
            "photo": f"attach://{attach_name}"
        }
        files = {
            attach_name: ("photo.jpg", file_bytes, "image/jpeg")
        }
    payload = {
        "business_connection_id": connection_id,
        "photo": json.dumps(photo_json)
    }
    return safe_request(url, method="post", payload=payload, files=files, timeout=60)

def api_remove_business_profile_photo(token, connection_id):
    url = f"https://api.telegram.org/bot{token}/removeBusinessAccountProfilePhoto"
    payload = {
        "business_connection_id": connection_id
    }
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_read_business_message(token, connection_id, chat_id, message_id):
    url = f"https://api.telegram.org/bot{token}/readBusinessMessage"
    payload = {"business_connection_id": connection_id, "chat_id": chat_id, "message_id": message_id}
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_delete_business_messages(token, connection_id, message_ids):
    url = f"https://api.telegram.org/bot{token}/deleteBusinessMessages"
    payload = {"business_connection_id": connection_id, "message_ids": message_ids}
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_send_message_as_business(token, connection_id, chat_id, text, parse_mode=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "text": text
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_edit_business_message(token, connection_id, chat_id, message_id, text, parse_mode=None):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_send_chat_action_as_business(token, connection_id, chat_id, action="typing"):
    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    payload = {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "action": action
    }
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_pin_message_as_business(token, connection_id, chat_id, message_id):
    url = f"https://api.telegram.org/bot{token}/pinChatMessage"
    payload = {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "message_id": message_id
    }
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_unpin_message_as_business(token, connection_id, chat_id, message_id):
    url = f"https://api.telegram.org/bot{token}/unpinChatMessage"
    payload = {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "message_id": message_id
    }
    return safe_request(url, method="post", payload=payload, timeout=15)

def api_send_sticker_as_business(token, connection_id, chat_id, sticker):
    url = f"https://api.telegram.org/bot{token}/sendSticker"
    payload = {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "sticker": sticker
    }
    return safe_request(url, method="post", payload=payload, timeout=15)

# ==========================================
# Keyboard Builders
# ==========================================

def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_reply = types.InlineKeyboardButton("🤖 قسم الرد التلقائي", callback_data="menu_auto_reply")
    btn_profile = types.InlineKeyboardButton("👤 قسم الملف الشخصي", callback_data="menu_profile")
    btn_commands = types.InlineKeyboardButton("⚙️ قسم الأوامر", callback_data="menu_commands")
    btn_notes = types.InlineKeyboardButton("📝 ملاحظاتي", callback_data="menu_notes")
    btn_shortcuts = types.InlineKeyboardButton("⚡ اختصارات النصوص", callback_data="menu_shortcuts")
    btn_notify = types.InlineKeyboardButton("🔔 قسم الإشعارات", callback_data="menu_notifications")
    btn_fetch_messages = types.InlineKeyboardButton("📥 جلب رسائل", callback_data="fetch_messages")
    markup.add(btn_reply, btn_profile)
    markup.add(btn_commands, btn_notes)
    markup.add(btn_shortcuts, btn_notify)
    markup.add(btn_fetch_messages)
    return markup

def get_autoreply_keyboard(user_id):
    data = load_data()
    auto_read = data.get("auto_read", {}).get(user_id, False)
    auto_read_status = "🟢 القراءة: مفعلة" if auto_read else "🔴 القراءة: معطلة"

    auto_reply = data.get("auto_reply_enabled", {}).get(user_id, False)
    auto_reply_status = "🟢 الرد: مفعل" if auto_reply else "🔴 الرد: معطل"

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_autoread = types.InlineKeyboardButton(auto_read_status, callback_data="toggle_autoread")
    btn_autoreply = types.InlineKeyboardButton(auto_reply_status, callback_data="toggle_autoreply")
    btn_set_text = types.InlineKeyboardButton("💬 نص الرد التلقائي", callback_data="set_reply_text")
    btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
    markup.add(btn_autoread, btn_autoreply)
    markup.add(btn_set_text, btn_back)
    return markup

def get_profile_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_name = types.InlineKeyboardButton("📝 تعديل الاسم", callback_data="change_name")
    btn_add_photo = types.InlineKeyboardButton("🖼️ إضافة صورة/فيديو", callback_data="change_photo")
    btn_del_photo = types.InlineKeyboardButton("🗑️ حذف الصورة", callback_data="delete_photo")
    btn_add_bio = types.InlineKeyboardButton("✍️ إضافة بايو", callback_data="change_bio")
    btn_del_bio = types.InlineKeyboardButton("🗑️ حذف البايو", callback_data="delete_bio")
    btn_add_user = types.InlineKeyboardButton("🔗 إضافة يوزر", callback_data="change_username")
    btn_del_user = types.InlineKeyboardButton("🗑️ حذف اليوزر", callback_data="delete_username")
    btn_add_story = types.InlineKeyboardButton("📸 نشر استوري", callback_data="post_story")
    btn_del_story = types.InlineKeyboardButton("🗑️ حذف ستوري", callback_data="delete_story")
    btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
    markup.add(btn_name)
    markup.add(btn_add_photo, btn_del_photo)
    markup.add(btn_add_bio, btn_del_bio)
    markup.add(btn_add_user, btn_del_user)
    markup.add(btn_add_story, btn_del_story)
    markup.add(btn_back)
    return markup

def get_commands_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_custom = types.InlineKeyboardButton("⚙️ تخصيص الأوامر", callback_data="custom_cmds_menu")
    btn_help = types.InlineKeyboardButton("📖 شرح الأوامر", callback_data="chat_commands_help")
    btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
    markup.add(btn_custom, btn_help)
    markup.add(btn_back)
    return markup

def get_custom_cmds_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_del = types.InlineKeyboardButton("❌ أمر الحذف", callback_data="set_cmd_delete")
    btn_pin = types.InlineKeyboardButton("📌 أمر التثبيت", callback_data="set_cmd_pin")
    btn_unpin = types.InlineKeyboardButton("🔓 إلغاء التثبيت", callback_data="set_cmd_unpin")
    btn_id = types.InlineKeyboardButton("🆔 أمر الآيدي", callback_data="set_cmd_id")
    btn_note = types.InlineKeyboardButton("📝 أمر الملاحظة", callback_data="set_cmd_note")
    btn_help = types.InlineKeyboardButton("❓ أمر المساعدة", callback_data="set_cmd_help")
    btn_back = types.InlineKeyboardButton("🔙 رجوع لقسم الأوامر", callback_data="menu_commands")
    markup.add(btn_del, btn_pin)
    markup.add(btn_unpin, btn_id)
    markup.add(btn_note, btn_help)
    markup.add(btn_back)
    return markup

def get_notes_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_list = types.InlineKeyboardButton("📋 عرض الملاحظات", callback_data="list_notes")
    btn_clear = types.InlineKeyboardButton("🗑️ مسح كل الملاحظات", callback_data="clear_notes")
    btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
    markup.add(btn_list, btn_clear)
    markup.add(btn_back)
    return markup

def get_shortcuts_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_add = types.InlineKeyboardButton("➕ إضافة اختصار", callback_data="add_shortcut")
    btn_list = types.InlineKeyboardButton("📋 عرض الاختصارات", callback_data="list_shortcuts")
    btn_clear = types.InlineKeyboardButton("🗑️ مسح الاختصارات", callback_data="clear_shortcuts")
    btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
    markup.add(btn_add, btn_list)
    markup.add(btn_clear)
    markup.add(btn_back)
    return markup

def get_notifications_keyboard(user_id):
    data = load_data()
    del_notify = data.get("notify_deletions", {}).get(user_id, True)
    edit_notify = data.get("notify_edits", {}).get(user_id, True)

    del_status = "🟢 إشعارات الحذف: مفعلة" if del_notify else "🔴 إشعارات الحذف: معطلة"
    edit_status = "🟢 إشعارات التعديل: مفعلة" if edit_notify else "🔴 إشعارات التعديل: معطلة"

    markup = types.InlineKeyboardMarkup(row_width=1)
    btn_del = types.InlineKeyboardButton(del_status, callback_data="toggle_notify_del")
    btn_edit = types.InlineKeyboardButton(edit_status, callback_data="toggle_notify_edit")
    btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
    markup.add(btn_del, btn_edit, btn_back)
    return markup

def get_admin_keyboard(activated_list):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for uid in activated_list:
        if uid == OWNER_ID:
            continue
        btn = types.InlineKeyboardButton(f"🔴 إلغاء {uid}", callback_data=f"deactivate_{uid}")
        buttons.append(btn)
    for i in range(0, len(buttons), 2):
        markup.add(*buttons[i:i+2])
    return markup

def show_custom_cmds_menu(chat_id, user_id):
    data = load_data()
    cmds = get_user_commands(data, user_id)
    msg_text = (
        "⚙️ <b>لوحة تخصيص أوامر الدردشة السريعة:</b>\n\n"
        "يمكنك تغيير كلمات أو رموز الأوامر الافتراضية إلى أي كلمات تفضلها بالكامل.\n\n"
        f"❌ <b>أمر الحذف:</b> <code>{escape_html(cmds['delete'])}</code>\n"
        f"📌 <b>أمر التثبيت:</b> <code>{escape_html(cmds['pin'])}</code>\n"
        f"🔓 <b>أمر إلغاء التثبيت:</b> <code>{escape_html(cmds['unpin'])}</code>\n"
        f"🆔 <b>أمر الآيدي:</b> <code>{escape_html(cmds['id'])}</code>\n"
        f"📝 <b>أمر الملاحظة:</b> <code>{escape_html(cmds['note'])}</code>\n"
        f"❓ <b>أمر المساعدة:</b> <code>{escape_html(cmds['help'])}</code>\n\n"
        "اضغط على الأزرار أدناه لتغيير الكلمة الخاصة بأي أمر:"
    )
    safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_custom_cmds_keyboard(user_id))

# ==========================================
# Command Handlers
# ==========================================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_user_allowed(message):
        return

    user_id = str(message.from_user.id)
    data = load_data()
    conn_id = data["connections"].get(user_id)
    conn_details = data.get("connection_details", {}).get(user_id, {})

    if conn_id:
        can_reply = conn_details.get("can_reply", True)
        reply_warning = ""
        if not can_reply:
            reply_warning = (
                "⚠️ <b>تحذير هام من المساعد الذكي:</b> البوت لا يملك صلاحية الرد بالنيابة عنك حالياً.\n"
                "يرجى الانتقال إلى إعدادات تليجرام بيزنس > روبوتات الدردشة، وتفعيل خيار 'الرد على الرسائل' ليتمكن البوت من العمل والرد التلقائي بنجاح.\n\n"
            )
        welcome_text = (
            f"مرحباً بك {escape_html(message.from_user.first_name)}! 👋\n\n"
            f"{reply_warning}"
            "🏆 <b>القائمة الرئيسية لخدمات البوت المساعد الذكي والشخصي.</b>\n\n"
            "يرجى اختيار القسم الذي ترغب في إدارته من الأزرار المتجاورة أدناه:"
        )
        safe_bot_call(bot.send_message, message.chat.id, welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())
    else:
        info_text = (
            f"مرحباً بك {escape_html(message.from_user.first_name)}! 👋\n\n"
            "أنا بوت المساعد الذكي والشخصي لحساب تليجرام بيزنس الخاص بك.\n\n"
            "⚠️ <b>لبدء الاستخدام، يرجى ربط البوت باتباع الخطوات التالية:</b>\n"
            "1️⃣ افتح إعدادات تليجرام في هاتفك ⚙️.\n"
            "2️⃣ اذهب إلى <b>تليجرام بيزنس (Telegram Business)</b> ثم <b>روبوتات الدردشة (Chatbots)</b>.\n"
            "3️⃣ ابحث عن هذا البوت وقم بربطه.\n"
            "4️⃣ <b>هام جداً:</b> امنح البوت كافة الصلاحيات المتاحة.\n\n"
            "بمجرد الربط، سأرسل لك رسالة تأكيد هنا لبدء استخدام اللوحة التفاعلية! 🚀"
        )
        safe_bot_call(bot.send_message, message.chat.id, info_text, parse_mode="HTML")

@bot.message_handler(commands=['info'])
def send_info(message):
    if not is_user_allowed(message):
        return

    user_id = str(message.from_user.id)
    data = load_data()
    cmds = get_user_commands(data, user_id)

    part1 = (
        "📖 <b>دليل المساعد الذكي والشخصي (الجزء الأول):</b>\n\n"
        "أهلاً بك في دليل استخدام البوت الشامل. تم تزويد هذا البوت بأحدث ميزات Telegram Bot API "
        "التي تتيح التحكم بحساب البيزنس المرتبط به.\n\n"
        "🔧 <b>أولاً: إدارة الملف الشخصي (Profile Management):</b>\n"
        "• <b>تغيير الاسم:</b> يتيح للبوت تغيير اسم حسابك الشخصي الأول والأخير. يتطلب صلاحية can_change_name.\n"
        "• <b>تغيير البايو:</b> يمكنك تعديل النبذة التعريفية لحسابك في أي وقت بحد أقصى 70 حرفاً. يتطلب صلاحية can_change_bio.\n"
        "• <b>تغيير اليوزر (اسم المستخدم):</b> لتغيير معرّف حسابك الشخصي برمجياً. يتطلب صلاحية can_change_username."
    )

    part2 = (
        "📖 <b>دليل المساعد الذكي والشخصي (الجزء الثاني):</b>\n\n"
        "🚀 <b>ثانياً: النشر والتفاعل التلقائي الذكي:</b>\n"
        "• <b>رفع القصص (Stories):</b> عند الضغط على زر القصص وإرسال صورة أو مقطع فيديو للبوت، سيقوم البوت فوراً بتحميله وإعادة نشره كقصة (Story) "
        "على حسابك تظهر لجميع جهات اتصالك لمدة 24 ساعة. يتطلب صلاحية can_manage_stories.\n\n"
        "• <b>تعديل صورة الحساب الشخصية:</b> يمكنك تغيير صورة بروفايل حسابك أو حذفها مباشرة من البوت.\n\n"
        "• <b>القراءة التلقائية الذكية (Auto-Read):</b> عند تفعيلها من لوحة التحكم، أي رسالة يرسلها أي شخص "
        "إليك في الدردشات الخاصة سيقوم البوت فوراً بالتعليم عليها كـ 'مقروءة' دون الحاجة لدخولك للتطبيق. يتطلب صلاحية can_read_messages.\n\n"
        "• <b>الرد التلقائي المخصص (Auto-Responder):</b> عند تفعيله، سيقوم البوت بالرد تلقائياً على أي شخص يراسلك بنص مخصص تحدده أنت، "
        "مع إظهار حالة 'جاري الكتابة...' لإعطاء انطباع طبيعي وبشري تماماً."
    )

    part3 = (
        "📖 <b>دليل المساعد الذكي والشخصي (الجزء الثالث):</b>\n\n"
        "🛡️ <b>ثالثاً: إدارة الدردشات السريعة عبر الأوامر (Chat Commands):</b>\n"
        "يمكنك إدارة شات البيزنس بشكل مباشر عبر الرد على رسائل العملاء بكلماتك المخصصة التالية:\n\n"
        f"• <b><code>{escape_html(cmds['delete'])}</code></b>: للرد على أي رسالة وحذفها فوراً من الشات.\n"
        f"• <b><code>{escape_html(cmds['pin'])}</code></b>: للرد على أي رسالة وتثبيتها فوراً أعلى المحادثة.\n"
        f"• <b><code>{escape_html(cmds['unpin'])}</code></b>: للرد على الرسالة المثبتة وإلغاء تثبيتها فوراً.\n"
        f"• <b><code>{escape_html(cmds['id'])}</code></b>: لجلب آيديك الشخصي إذا أرسلته بدون رد، أو آيدي الشخص الذي رددت على رسالته.\n"
        f"• <b><code>{escape_html(cmds['note'])} [عنوان]</code></b>: للرد على الرسالة وحفظها كملاحظة تتبع هذا العنوان، أو كتابة الأمر بمفرده لحفظ الرسالة مباشرة.\n"
        f"• <b><code>{escape_html(cmds['help'])}</code></b>: لإرسال قائمة بجميع الأوامر المتاحة مباشرة في المحادثة.\n\n"
        "💡 تعمل هذه الأوامر في الدردشات الخاصة عبر حساب البيزنس.\n"
        "💡 يمكنك تغيير هذه الأوامر لأي كلمات تناسبك من خلال خيار '⚙️ تخصيص الأوامر'."
    )

    safe_bot_call(bot.send_message, message.chat.id, part1, parse_mode="HTML")
    safe_bot_call(bot.send_message, message.chat.id, part2, parse_mode="HTML")
    safe_bot_call(bot.send_message, message.chat.id, part3, parse_mode="HTML")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    user_id = str(message.from_user.id)
    if user_id != OWNER_ID:
        safe_bot_call(bot.send_message, message.chat.id, "⚠️ <b>هذا الأمر مخصص للمشرف والمسؤول الأساسي للبوت فقط.</b>", parse_mode="HTML")
        return

    data = load_data()
    activated = data.get("activated_users", [])
    users_list = [u for u in activated if u != OWNER_ID]

    if not users_list:
        safe_bot_call(bot.send_message, message.chat.id, "👥 <b>لا يوجد مستخدمين مفعلين في البوت حالياً.</b>", parse_mode="HTML")
        return

    text = "👥 <b>قائمة المستخدمين المفعلين للبوت حالياً:</b>\n\n"
    for u in users_list:
        text += f"• الآيدي: <code>{u}</code>\n"
    text += "\nيمكنك إلغاء تفعيل أي مستخدم بالضغط على الأزرار المتجاورة أدناه:"

    safe_bot_call(bot.send_message, message.chat.id, text, parse_mode="HTML", reply_markup=get_admin_keyboard(users_list))

# ==========================================
# Business Connection Handler
# ==========================================

@bot.business_connection_handler()
def handle_business_connection(connection):
    user_id = str(connection.user.id)
    user_chat_id = connection.user_chat_id
    data = load_data()

    if user_id not in data.get("activated_users", []):
        return

    if connection.is_enabled:
        data["connections"][user_id] = connection.id

        if "connection_details" not in data:
            data["connection_details"] = {}
        data["connection_details"][user_id] = {
            "can_reply": connection.can_reply,
            "user_chat_id": user_chat_id,
            "username": connection.user.username or "",
            "first_name": connection.user.first_name or "",
            "last_name": connection.user.last_name or ""
        }
        save_data(data)

        reply_status = "🟢 مفعلة بالكامل" if connection.can_reply else "🔴 معطلة (يرجى تفعيل خيار الرد على الرسائل في إعدادات البيزنس)"
        success_text = (
            "🎉 <b>رائع! تم ربط البوت بحسابك البيزنس بنجاح.</b>\n\n"
            f"🛡️ <b>صلاحية الرد بالنيابة عنك:</b> {reply_status}\n\n"
            "لوحة التحكم التفاعلية جاهزة الآن لإدارة حسابك ومحادثاتك بالكامل. استخدم الأزرار أدناه:"
        )
        try:
            safe_bot_call(bot.send_message, user_chat_id, success_text, parse_mode="HTML", reply_markup=get_main_keyboard())
        except Exception as e:
            logger.error(f"Error sending main connection message: {e}")
    else:
        if user_id in data["connections"]:
            del data["connections"][user_id]
        if user_id in data["states"]:
            del data["states"][user_id]
        if "connection_details" in data and user_id in data["connection_details"]:
            del data["connection_details"][user_id]
        save_data(data)
        try:
            safe_bot_call(bot.send_message, user_chat_id, "⚠️ <b>تم إلغاء ربط البوت بحسابك البيزنس.</b>", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error sending disconnect message: {e}")

# ==========================================
# Callback Query Handler
# ==========================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = str(call.from_user.id)
    chat_id = call.message.chat.id
    data = load_data()

    if call.data.startswith(("approve_", "decline_", "deactivate_")):
        if user_id != OWNER_ID:
            safe_bot_call(bot.answer_callback_query, call.id, "⚠️ هذا الإجراء متاح فقط للمشرف الأساسي للبوت.", show_alert=True)
            return

        target_uid = call.data.split("_")[1]

        if call.data.startswith("approve_"):
            if "activated_users" not in data:
                data["activated_users"] = []
            if target_uid not in data["activated_users"]:
                data["activated_users"].append(target_uid)
            if "pending_requests" in data and target_uid in data["pending_requests"]:
                data["pending_requests"].remove(target_uid)
            save_data(data)

            safe_bot_call(bot.answer_callback_query, call.id, "✅ تم قبول المستخدم وتفعيله")
            try:
                safe_bot_call(bot.edit_message_text, f"✅ تم قبول تفعيل المستخدم <code>{target_uid}</code> بنجاح.", chat_id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass
            try:
                safe_bot_call(bot.send_message, target_uid, "🎉 <b>مبروك! تم تفعيل البوت لك بواسطة المشرف.</b>\n\nيمكنك الآن إرسال /start للبدء في استخدام كافة مميزات البوت لخدمتك! 🚀", parse_mode="HTML")
            except Exception:
                pass

        elif call.data.startswith("decline_"):
            if "pending_requests" in data and target_uid in data["pending_requests"]:
                data["pending_requests"].remove(target_uid)
            save_data(data)
            safe_bot_call(bot.answer_callback_query, call.id, "❌ تم رفض تفعيل العضو")
            try:
                safe_bot_call(bot.edit_message_text, f"❌ تم رفض تفعيل المستخدم <code>{target_uid}</code>.", chat_id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass

        elif call.data.startswith("deactivate_"):
            if "activated_users" in data and target_uid in data["activated_users"]:
                data["activated_users"].remove(target_uid)
            save_data(data)
            safe_bot_call(bot.answer_callback_query, call.id, "🔴 تم إلغاء تفعيله")
            try:
                safe_bot_call(bot.edit_message_text, f"🔴 تم إلغاء تفعيل المستخدم <code>{target_uid}</code> بنجاح.", chat_id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass
        return

    if not is_user_allowed(call):
        return

    conn_id = data["connections"].get(user_id)
    if not conn_id:
        safe_bot_call(bot.answer_callback_query, call.id, "لم يتم ربط البوت بحساب بيزنس بعد!", show_alert=True)
        return

    if call.data == "menu_auto_reply":
        safe_bot_call(bot.answer_callback_query, call.id)
        msg_text = "🤖 <b>قسم الرد والقراءة التلقائية:</b>\n\nيمكنك التحكم في تشغيل الرد التلقائي المخصص، تعديل نص الرد، أو إدارة ميزة القراءة التلقائية للمحادثات الواردة:"
        try:
            safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_autoreply_keyboard(user_id))
        except Exception:
            safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_autoreply_keyboard(user_id))

    elif call.data == "menu_profile":
        safe_bot_call(bot.answer_callback_query, call.id)
        msg_text = "👤 <b>قسم إدارة الملف الشخصي (Profile Settings):</b>\n\nيمكنك تعديل بيانات حساب البيزنس الخاص بك بالكامل، تحديث صورة بروفايل الحساب أو حذفها، تعديل البايو، اسم المستخدم، أو نشر وحذف الاستوري:"
        try:
            safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_profile_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_profile_keyboard())

    elif call.data == "menu_commands":
        safe_bot_call(bot.answer_callback_query, call.id)
        msg_text = "⚙️ <b>قسم تخصيص وإدارة الأوامر السريعة:</b>\n\nيمكنك قراءة الدليل الكامل لكيفية استخدام الأوامر السريعة لحذف وتثبيت رسائل العملاء، أو إعادة تخصيص الكلمات المفتاحية لتلك الأوامر:"
        try:
            safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_commands_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_commands_keyboard())

    elif call.data == "menu_notes":
        safe_bot_call(bot.answer_callback_query, call.id)
        notes = data.get("notes", {}).get(user_id, [])
        if not notes:
            msg_text = "📝 <b>ملاحظاتي الشخصية:</b>\n\nلا توجد ملاحظات محفوظة بعد.\n\nيمكنك إضافة ملاحظات سريعة من داخل أي محادثة بيزنس بأمر الملاحظة، وستُحفظ هنا:"
            try:
                safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_notes_keyboard())
            except Exception:
                safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_notes_keyboard())
        else:
            text = "📝 <b>ملاحظاتك المحفوظة (اضغط على عرض لقراءة المحتوى بالكامل):</b>\n\n"
            markup = types.InlineKeyboardMarkup(row_width=2)
            for note in notes:
                n_id = note["id"]
                title = note.get("title", "ملاحظة عامة")
                text += f"<b>• {n_id}.</b> {escape_html(title)} (📅 {note.get('date')})\n"
                btn_view = types.InlineKeyboardButton(f"👁️ عرض {n_id}", callback_data=f"view_note_{n_id}")
                btn_del = types.InlineKeyboardButton(f"🗑️ حذف {n_id}", callback_data=f"del_note_{n_id}")
                markup.add(btn_view, btn_del)
            btn_clear = types.InlineKeyboardButton("🗑️ مسح كل الملاحظات", callback_data="clear_notes")
            btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
            markup.add(btn_clear)
            markup.add(btn_back)
            try:
                safe_bot_call(bot.edit_message_text, text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass

    elif call.data.startswith("view_note_"):
        safe_bot_call(bot.answer_callback_query, call.id)
        note_id = int(call.data.split("_")[2])
        notes = data.get("notes", {}).get(user_id, [])
        note = next((n for n in notes if n["id"] == note_id), None)
        if note:
            msg_text = (
                f"📝 <b>تفاصيل الملاحظة رقم {note_id}:</b>\n\n"
                f"📌 <b>العنوان:</b> {escape_html(note.get('title'))}\n"
                f"📅 <b>التاريخ:</b> {note.get('date')}\n\n"
                f"💬 <b>المحتوى:</b>\n{escape_html(note.get('text'))}"
            )
            markup = types.InlineKeyboardMarkup()
            btn_delete_this = types.InlineKeyboardButton(f"🗑️ حذف هذه الملاحظة ({note_id})", callback_data=f"del_note_{note_id}")
            btn_back_notes = types.InlineKeyboardButton("🔙 رجوع للملاحظات", callback_data="menu_notes")
            markup.add(btn_delete_this)
            markup.add(btn_back_notes)
            try:
                safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
        else:
            safe_bot_call(bot.answer_callback_query, call.id, "⚠️ الملاحظة المطلوبة غير متوفرة.", show_alert=True)

    elif call.data.startswith("del_note_"):
        note_id = int(call.data.split("_")[2])
        if "notes" in data and user_id in data["notes"]:
            notes = data["notes"][user_id]
            new_notes = [n for n in notes if n["id"] != note_id]
            for i, n in enumerate(new_notes, 1):
                n["id"] = i
            data["notes"][user_id] = new_notes
            save_data(data)
            safe_bot_call(bot.answer_callback_query, call.id, f"✅ تم حذف الملاحظة رقم {note_id}")

            notes = data.get("notes", {}).get(user_id, [])
            if not notes:
                msg_text = "📝 <b>ملاحظاتي الشخصية:</b>\n\nلا توجد ملاحظات محفوظة بعد.\n\nيمكنك إضافة ملاحظات سريعة من داخل أي محادثة بيزنس بأمر الملاحظة، وستُحفظ هنا:"
                try:
                    safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_notes_keyboard())
                except Exception:
                    pass
            else:
                text = "📝 <b>ملاحظاتك المحفوظة (اضغط على عرض لقراءة المحتوى بالكامل):</b>\n\n"
                markup = types.InlineKeyboardMarkup(row_width=2)
                for note in notes:
                    n_id = note["id"]
                    title = note.get("title", "ملاحظة عامة")
                    text += f"<b>• {n_id}.</b> {escape_html(title)} (📅 {note.get('date')})\n"
                    btn_view = types.InlineKeyboardButton(f"👁️ عرض {n_id}", callback_data=f"view_note_{n_id}")
                    btn_del = types.InlineKeyboardButton(f"🗑️ حذف {n_id}", callback_data=f"del_note_{n_id}")
                    markup.add(btn_view, btn_del)
                btn_clear = types.InlineKeyboardButton("🗑️ مسح كل الملاحظات", callback_data="clear_notes")
                btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
                markup.add(btn_clear)
                markup.add(btn_back)
                try:
                    safe_bot_call(bot.edit_message_text, text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
                except Exception:
                    pass

    elif call.data == "clear_notes":
        safe_bot_call(bot.answer_callback_query, call.id)
        if "notes" not in data:
            data["notes"] = {}
        data["notes"][user_id] = []
        save_data(data)
        safe_bot_call(bot.send_message, chat_id, "✅ <b>تم مسح جميع الملاحظات بنجاح.</b>", parse_mode="HTML", reply_markup=get_notes_keyboard())

    elif call.data == "fetch_messages":
        data["states"][user_id] = "AWAITING_FETCH_PERSON_ID"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        if "fetch_message_targets" not in data: data["fetch_message_targets"] = {}
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(
            bot.send_message, chat_id,
            "📥 <b>جلب رسائل محفوظة</b>\n\n"
            "أرسل آيدي الشخص أو آيدي المحادثة الآن.\n"
            "سيتم البحث داخل الرسائل التي وصلت للبوت وتم حفظها في الكاش فقط.",
            parse_mode="HTML"
        )
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "back_to_main":
        safe_bot_call(bot.answer_callback_query, call.id)
        welcome_text = "🏆 <b>القائمة الرئيسية لخدمات البوت المساعد الشخصي.</b>\n\nيرجى اختيار القسم الذي ترغب في إدارته من الأزرار المتجاورة أدناه:"
        try:
            safe_bot_call(bot.edit_message_text, welcome_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_main_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())

    elif call.data == "change_name":
        data["states"][user_id] = "AWAITING_NAME"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "📝 <b>أرسل الاسم الجديد الآن:</b>\n\n• كلمة واحدة: تعديل الاسم الأول.\n• كلمتان (بينهما مسافة): الكلمة الأولى اسم أول، والثانية العائلة.", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "change_bio":
        data["states"][user_id] = "AWAITING_BIO"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "✍️ <b>أرسل البايو (النبذة التعريفية) الجديد:</b>\n\n⚠️ الحد الأقصى 70 حرفاً.", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "delete_bio":
        safe_bot_call(bot.answer_callback_query, call.id)
        old_bio = data.get("last_bios", {}).get(user_id, "غير معروف")
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري حذف النبذة التعريفية (البايو) من حسابك...</b>", parse_mode="HTML")
        res = api_set_business_bio(TOKEN, conn_id, "")
        msg_ids = []
        if call.message.message_id: msg_ids.append(call.message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            if "last_bios" not in data: data["last_bios"] = {}
            data["last_bios"][user_id] = "لا يوجد"
            save_data(data)
            safe_bot_call(bot.send_message, chat_id, "<b>تم حذف البايو بنجاح</b>\n\n" + f"البايو القديم: <code>{escape_html(old_bio)}</code>\nالبايو الجديد: <code>لا يوجد</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل حذف البايو.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())

    elif call.data == "change_username":
        data["states"][user_id] = "AWAITING_USERNAME"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "🔗 <b>أرسل معرّف المستخدم (اليوزر) الجديد الآن:</b>\n\n⚠️ دون وضع علامة @ في البداية.", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "delete_username":
        safe_bot_call(bot.answer_callback_query, call.id)
        old_username = "لا يوجد"
        conn_res = api_get_business_connection(TOKEN, conn_id)
        if conn_res.get("ok"):
            u = conn_res["result"]["user"]
            old_username = f"@{u.get('username')}" if u.get('username') else "لا يوجد"
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري إزالة اسم المستخدم (اليوزر) من حسابك...</b>", parse_mode="HTML")
        res = api_set_business_username(TOKEN, conn_id, "")
        msg_ids = []
        if call.message.message_id: msg_ids.append(call.message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            safe_bot_call(bot.send_message, chat_id, "<b>تم حذف اليوزر بنجاح</b>\n\n" + f"اليوزر القديم: <code>{escape_html(old_username)}</code>\nاليوزر الجديد: <code>لا يوجد</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل حذف اليوزر.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())

    elif call.data == "post_story":
        # Show background choice first before asking for media
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_blurred = types.InlineKeyboardButton("🌫️ خلفية ضبابية", callback_data="story_bg_blurred")
        btn_black = types.InlineKeyboardButton("⬛ خلفية سوداء", callback_data="story_bg_black")
        btn_white = types.InlineKeyboardButton("⬜ خلفية بيضاء", callback_data="story_bg_white")
        btn_cancel = types.InlineKeyboardButton("❌ إلغاء", callback_data="menu_profile")
        markup.add(btn_blurred, btn_black)
        markup.add(btn_white)
        markup.add(btn_cancel)
        safe_bot_call(
            bot.send_message, chat_id,
            "🎨 <b>اختر نوع الخلفية للاستوري:</b>\n\n"
            "🌫️ <b>ضبابية:</b> خلفية ضبابية من نفس الوسائط\n"
            "⬛ <b>سوداء:</b> خلفية سوداء بالكامل\n"
            "⬜ <b>بيضاء:</b> خلفية بيضاء بالكامل",
            parse_mode="HTML",
            reply_markup=markup
        )

    elif call.data == "story_bg_blurred":
        if "story_bg_type" not in data: data["story_bg_type"] = {}
        data["story_bg_type"][user_id] = "blurred"
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id, "✅ تم اختيار خلفية ضبابية")
        # حذف رسالة اختيار الخلفية
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        # عرض أزرار اختيار مدة الاستوري
        dur_markup = types.InlineKeyboardMarkup(row_width=2)
        d6h = types.InlineKeyboardButton("6 ساعات ⏱️", callback_data="story_dur_21600")
        d12h = types.InlineKeyboardButton("12 ساعة ⏱️", callback_data="story_dur_43200")
        d24h = types.InlineKeyboardButton("24 ساعة ⏱️", callback_data="story_dur_86400")
        d48h = types.InlineKeyboardButton("48 ساعة ⏱️", callback_data="story_dur_172800")
        d_cancel = types.InlineKeyboardButton("❌ إلغاء", callback_data="menu_profile")
        dur_markup.add(d6h, d12h, d24h, d48h)
        dur_markup.add(d_cancel)
        safe_bot_call(
            bot.send_message, chat_id,
            "⏱️ <b>اختر مدة بقاء الاستوري:</b>\n\n"
            "⚠️ المدد بخلاف 24 ساعة تتطلب اشتراك تليجرام بريميوم للحساب التجاري.",
            parse_mode="HTML",
            reply_markup=dur_markup
        )

    elif call.data == "story_bg_black":
        if "story_bg_type" not in data: data["story_bg_type"] = {}
        data["story_bg_type"][user_id] = "black"
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id, "✅ تم اختيار خلفية سوداء")
        # حذف رسالة اختيار الخلفية
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        # عرض أزرار اختيار مدة الاستوري
        dur_markup = types.InlineKeyboardMarkup(row_width=2)
        d6h = types.InlineKeyboardButton("6 ساعات ⏱️", callback_data="story_dur_21600")
        d12h = types.InlineKeyboardButton("12 ساعة ⏱️", callback_data="story_dur_43200")
        d24h = types.InlineKeyboardButton("24 ساعة ⏱️", callback_data="story_dur_86400")
        d48h = types.InlineKeyboardButton("48 ساعة ⏱️", callback_data="story_dur_172800")
        d_cancel = types.InlineKeyboardButton("❌ إلغاء", callback_data="menu_profile")
        dur_markup.add(d6h, d12h, d24h, d48h)
        dur_markup.add(d_cancel)
        safe_bot_call(
            bot.send_message, chat_id,
            "⏱️ <b>اختر مدة بقاء الاستوري:</b>\n\n"
            "⚠️ المدد بخلاف 24 ساعة تتطلب اشتراك تليجرام بريميوم للحساب التجاري.",
            parse_mode="HTML",
            reply_markup=dur_markup
        )

    elif call.data == "story_bg_white":
        if "story_bg_type" not in data: data["story_bg_type"] = {}
        data["story_bg_type"][user_id] = "white"
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id, "✅ تم اختيار خلفية بيضاء")
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        dur_markup = types.InlineKeyboardMarkup(row_width=2)
        d6h = types.InlineKeyboardButton("6 ساعات ⏱️", callback_data="story_dur_21600")
        d12h = types.InlineKeyboardButton("12 ساعة ⏱️", callback_data="story_dur_43200")
        d24h = types.InlineKeyboardButton("24 ساعة ⏱️", callback_data="story_dur_86400")
        d48h = types.InlineKeyboardButton("48 ساعة ⏱️", callback_data="story_dur_172800")
        d_cancel = types.InlineKeyboardButton("❌ إلغاء", callback_data="menu_profile")
        dur_markup.add(d6h, d12h, d24h, d48h)
        dur_markup.add(d_cancel)
        safe_bot_call(
            bot.send_message, chat_id,
            "⏱️ <b>اختر مدة بقاء الاستوري:</b>\n\n"
            "⚠️ المدد بخلاف 24 ساعة تتطلب اشتراك تليجرام بريميوم للحساب التجاري.",
            parse_mode="HTML",
            reply_markup=dur_markup
        )

    elif call.data.startswith("story_dur_"):
        duration = int(call.data.split("_")[2])
        if "story_active_period" not in data: data["story_active_period"] = {}
        data["story_active_period"][user_id] = duration
        data["states"][user_id] = "AWAITING_STORY"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id, "✅ تم تحديد المدة")
        # حذف رسالة اختيار المدة
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        # إرسال طلب الوسائط
        story_bg = data.get("story_bg_type", {}).get(user_id, "blurred")
        bg_label = "ضبابية" if story_bg == "blurred" else ("بيضاء" if story_bg == "white" else "سوداء")
        hours = duration // 3600
        prompt = safe_bot_call(
            bot.send_message, chat_id,
            f"📸 <b>أرسل صورة أو فيديو الآن.</b>\n\n"
            f"🎨 الخلفية: <b>{bg_label}</b>\n"
            f"⏱️ المدة: <b>{hours} ساعة</b>\n\n"
            "سيجهز البوت الوسائط تلقائياً بنسبة 9:16 ثم ينشره كاستوري.",
            parse_mode="HTML"
        )
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "delete_story":
        safe_bot_call(bot.answer_callback_query, call.id)
        last_story_id = data.get("last_story_ids", {}).get(user_id)
        if not last_story_id:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>لم يتم العثور على قصة (Story) مسجلة تم نشرها مؤخراً عبر هذا البوت لحذفها.</b>", parse_mode="HTML", reply_markup=get_profile_keyboard())
            return
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري حذف القصة (Story) من حسابك الشخصي...</b>", parse_mode="HTML")
        res = api_delete_story(TOKEN, conn_id, last_story_id)
        msg_ids = []
        if call.message.message_id: msg_ids.append(call.message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            if "last_story_ids" in data and user_id in data["last_story_ids"]: del data["last_story_ids"][user_id]
            save_data(data)
            safe_bot_call(bot.send_message, chat_id, "✅ <b>تم حذف القصة (Story) بنجاح!</b>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل حذف القصة.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())

    elif call.data == "change_photo":
        data["states"][user_id] = "AWAITING_PHOTO"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "🖼️ <b>يرجى إرسال الصورة المربعة أو الفيديو القصير لتعيينها كصورة شخصية لملفك التجاري الآن:</b>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "delete_photo":
        safe_bot_call(bot.answer_callback_query, call.id)
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري إزالة صورة الملف الشخصي من حسابك...</b>", parse_mode="HTML")
        res = api_remove_business_profile_photo(TOKEN, conn_id)
        msg_ids = []
        if call.message.message_id: msg_ids.append(call.message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            safe_bot_call(bot.send_message, chat_id, "✅ <b>تم مسح وحذف صورة ملفك الشخصي بنجاح!</b>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل حذف الصورة.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())

    elif call.data == "toggle_autoread":
        current_status = data.get("auto_read", {}).get(user_id, False)
        if "auto_read" not in data: data["auto_read"] = {}
        data["auto_read"][user_id] = not current_status
        save_data(data)
        status_msg = "🟢 تم تفعيل القراءة التلقائية للرسائل الواردة." if not current_status else "🔴 تم إيقاف القراءة التلقائية للرسائل."
        safe_bot_call(bot.answer_callback_query, call.id, status_msg, show_alert=True)
        try: safe_bot_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=get_autoreply_keyboard(user_id))
        except Exception: pass

    elif call.data == "toggle_autoreply":
        current_status = data.get("auto_reply_enabled", {}).get(user_id, False)
        if "auto_reply_enabled" not in data: data["auto_reply_enabled"] = {}
        data["auto_reply_enabled"][user_id] = not current_status
        save_data(data)
        status_msg = "🟢 تم تفعيل الرد التلقائي للرسائل." if not current_status else "🔴 تم إيقاف الرد التلقائي."
        safe_bot_call(bot.answer_callback_query, call.id, status_msg, show_alert=True)
        try: safe_bot_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=get_autoreply_keyboard(user_id))
        except Exception: pass

    elif call.data == "set_reply_text":
        data["states"][user_id] = "AWAITING_REPLY_TEXT"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        current_text = data.get("auto_reply_text", {}).get(user_id, "لم يتم التحديد بعد.")
        prompt = safe_bot_call(bot.send_message, chat_id, f"💬 <b>نص الرد التلقائي الحالي:</b>\n« {escape_html(current_text)} »\n\nيرجى إرسال نص الرد التلقائي الجديد الآن:", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "custom_cmds_menu":
        safe_bot_call(bot.answer_callback_query, call.id)
        show_custom_cmds_menu(chat_id, user_id)

    elif call.data == "set_cmd_delete":
        data["states"][user_id] = "AWAITING_CUSTOM_DELETE"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "❌ <b>يرجى إرسال الكلمة أو الرمز الجديد لأمر الحذف:</b>\n\nمثال: <code>حذف</code> أو <code>مسح</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "set_cmd_pin":
        data["states"][user_id] = "AWAITING_CUSTOM_PIN"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "📌 <b>يرجى إرسال الكلمة أو الرمز الجديد لأمر التثبيت:</b>\n\nمثال: <code>تثبيت</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "set_cmd_unpin":
        data["states"][user_id] = "AWAITING_CUSTOM_UNPIN"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "🔓 <b>يرجى إرسال الكلمة أو الرمز الجديد لأمر إلغاء التثبيت:</b>\n\nمثال: <code>الغاء</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "set_cmd_id":
        data["states"][user_id] = "AWAITING_CUSTOM_ID"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "🆔 <b>يرجى إرسال الكلمة أو الرمز الجديد لأمر الآيدي:</b>\n\nمثال: <code>ايدي</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "set_cmd_note":
        data["states"][user_id] = "AWAITING_CUSTOM_NOTE"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "📝 <b>يرجى إرسال الكلمة أو الرمز الجديد لأمر الملاحظة:</b>\n\nمثال: <code>ملاحظة</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "set_cmd_help":
        data["states"][user_id] = "AWAITING_CUSTOM_HELP"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "❓ <b>يرجى إرسال الكلمة أو الرمز الجديد لأمر المساعدة:</b>\n\nمثال: <code>مساعدة</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "chat_commands_help":
        safe_bot_call(bot.answer_callback_query, call.id)
        cmds = get_user_commands(data, user_id)
        safe_bot_call(bot.send_message, chat_id,
            "⚙️ <b>قائمة جميع أوامر شات البيزنس السريعة المتاحة:</b>\n\n"
            "اكتب الأمر بالرد (Reply) على رسالة العميل أو بدون رد حسب الأمر:\n\n"
            f"🗑️ <code>{escape_html(cmds['delete'])}</code> — حذف الرسالة المردود عليها فوراً.\n"
            f"📌 <code>{escape_html(cmds['pin'])}</code> — تثبيت الرسالة في أعلى الشات.\n"
            f"🔓 <code>{escape_html(cmds['unpin'])}</code> — إلغاء تثبيت الرسالة.\n"
            f"🆔 <code>{escape_html(cmds['id'])}</code> — عرض آيديك أو آيدي صاحب الرسالة.\n"
            f"📝 <code>{escape_html(cmds['note'])} [عنوان]</code> — حفظ ملاحظة شخصية مخصصة تتبع هذا العنوان.\n"
            f"❓ <code>{escape_html(cmds['help'])}</code> — عرض هذه القائمة مباشرة في الشات.\n\n"
            "💡 <i>يمكنك تغيير أي أمر من قسم '⚙️ تخصيص الأوامر'.</i>",
            parse_mode="HTML")

    # ---- Notifications ----
    elif call.data == "menu_notifications":
        safe_bot_call(bot.answer_callback_query, call.id)
        msg_text = "🔔 <b>إعدادات الإشعارات للمساعد الذكي:</b>\n\nيمكنك هنا التحكم في تلقي إشعارات حذف الرسائل (عندما يقوم العميل بحذف رسائله أو رسائلك) وإشعارات تعديل الرسائل (عندما يقوم بتعديل رسائله):"
        try:
            safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_notifications_keyboard(user_id))
        except Exception:
            safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_notifications_keyboard(user_id))

    elif call.data == "toggle_notify_del":
        current_status = data.get("notify_deletions", {}).get(user_id, True)
        if "notify_deletions" not in data: data["notify_deletions"] = {}
        data["notify_deletions"][user_id] = not current_status
        save_data(data)
        status_msg = "🟢 تم تفعيل إشعارات حذف الرسائل." if not current_status else "🔴 تم إيقاف إشعارات حذف الرسائل."
        safe_bot_call(bot.answer_callback_query, call.id, status_msg, show_alert=True)
        try: safe_bot_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=get_notifications_keyboard(user_id))
        except Exception: pass

    elif call.data == "toggle_notify_edit":
        current_status = data.get("notify_edits", {}).get(user_id, True)
        if "notify_edits" not in data: data["notify_edits"] = {}
        data["notify_edits"][user_id] = not current_status
        save_data(data)
        status_msg = "🟢 تم تفعيل إشعارات تعديل الرسائل." if not current_status else "🔴 تم إيقاف إشعارات تعديل الرسائل."
        safe_bot_call(bot.answer_callback_query, call.id, status_msg, show_alert=True)
        try: safe_bot_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=get_notifications_keyboard(user_id))
        except Exception: pass

    # ---- Shortcuts ----
    elif call.data == "menu_shortcuts":
        safe_bot_call(bot.answer_callback_query, call.id)
        msg_text = "⚡ <b>قسم اختصارات النصوص السريعة:</b>\n\nيمكنك تعيين رمز أو كلمة مختصرة (مثل <code>.</code>) وبمجرد إرسالها في شات البيزنس سيقوم البوت تلقائياً بتعديلها إلى النص الكامل المخصص لها!"
        try:
            safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=get_shortcuts_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=get_shortcuts_keyboard())

    elif call.data == "add_shortcut":
        data["states"][user_id] = "AWAITING_SHORTCUT_TRIGGER"
        if "menu_message_ids" not in data: data["menu_message_ids"] = {}
        data["menu_message_ids"][user_id] = call.message.message_id
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        prompt = safe_bot_call(bot.send_message, chat_id, "⌨️ <b>أرسل رمز أو كلمة الاختصار الآن:</b>\n\nمثال: <code>.</code> أو <code>سلام</code>", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif call.data == "list_shortcuts" or call.data.startswith("delsh_"):
        if call.data.startswith("delsh_"):
            trigger_to_delete = call.data[6:]
            if "shortcuts" in data and user_id in data["shortcuts"]:
                if trigger_to_delete in data["shortcuts"][user_id]:
                    del data["shortcuts"][user_id][trigger_to_delete]
                    save_data(data)
                    safe_bot_call(bot.answer_callback_query, call.id, f"✅ تم حذف الاختصار: {trigger_to_delete}", show_alert=False)
        else:
            safe_bot_call(bot.answer_callback_query, call.id)

        shortcuts = data.get("shortcuts", {}).get(user_id, {})
        if not shortcuts:
            msg_text = "📋 <b>لا توجد أي اختصارات مسجلة حالياً.</b>"
            markup = get_shortcuts_keyboard()
        else:
            msg_text = "📋 <b>قائمة اختصارات النصوص المسجلة لديك:\n\nاضغط على زر الحذف بجانب أي اختصار لإزالته:</b>\n\n"
            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_list = []
            for trigger, expanded in shortcuts.items():
                msg_text += f"• <code>{escape_html(trigger)}</code> ➔ <i>{escape_html(expanded)}</i>\n"
                btn_del = types.InlineKeyboardButton(f"🗑️ {trigger}", callback_data=f"delsh_{trigger}")
                btn_list.append(btn_del)
            for i in range(0, len(btn_list), 2):
                markup.add(*btn_list[i:i+2])
            btn_add = types.InlineKeyboardButton("➕ إضافة اختصار", callback_data="add_shortcut")
            btn_clear = types.InlineKeyboardButton("🗑️ مسح الكل", callback_data="clear_shortcuts")
            btn_back = types.InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")
            markup.add(btn_add)
            markup.add(btn_clear, btn_back)
        try:
            safe_bot_call(bot.edit_message_text, msg_text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            safe_bot_call(bot.send_message, chat_id, msg_text, parse_mode="HTML", reply_markup=markup)

    elif call.data == "clear_shortcuts":
        safe_bot_call(bot.answer_callback_query, call.id)
        if "shortcuts" not in data: data["shortcuts"] = {}
        data["shortcuts"][user_id] = {}
        save_data(data)
        safe_bot_call(bot.send_message, chat_id, "✅ <b>تم مسح جميع اختصارات النصوص بنجاح.</b>", parse_mode="HTML", reply_markup=get_shortcuts_keyboard())

    # ---- Notification Inline Button Callbacks ----
    elif call.data.startswith("view_del_"):
        safe_bot_call(bot.answer_callback_query, call.id)
        try:
            notif_id = int(call.data.split("_")[2])
            with _notification_data_lock:
                ndata = _notification_data.get(notif_id)
            if not ndata or ndata.get('type') != 'delete':
                safe_bot_call(bot.edit_message_text, "⚠️ بيانات الإشعار غير متوفرة بعد الآن.", chat_id, call.message.message_id)
                return

            # Build full detail text
            detail_parts = [
                f"🗑️ <b>تفاصيل الرسالة المحذوفة</b>\n",
                f"👤 <b>العميل:</b> {escape_html(ndata.get('customer_name', 'غير معروف'))}",
                f"🆔 <b>آيدي المحادثة:</b> <code>{ndata.get('chat_id', '')}</code>",
                f"📅 <b>وقت الإرسال:</b> <code>{escape_html(ndata.get('send_date') or 'غير متوفر')}</code>",
                f"🗑️ <b>وقت الحذف:</b> <code>{ndata.get('delete_time', '')}</code>",
            ]

            if ndata.get('is_sender_customer'):
                detail_parts.append(f"🚨 قام {escape_html(ndata.get('customer_name', 'العميل'))} بحذف رسالته.")
            else:
                detail_parts.append(f"🚨 قام {escape_html(ndata.get('customer_name', 'العميل'))} بحذف رسالتك.")

            if ndata.get('text'):
                detail_parts.append(f"\n📝 <b>المحتوى النصي:</b>\n« {escape_html(ndata['text'])} »")
            if ndata.get('caption'):
                detail_parts.append(f"📝 <b>التعليق:</b>\n« {escape_html(ndata['caption'])} »")
            if ndata.get('media_type'):
                _, media_label = _get_media_emoji_label(ndata['media_type'])
                detail_parts.append(f"📦 <b>نوع الوسائط:</b> {media_label} (<code>{ndata['media_type']}</code>)")

            detail_text = "\n".join(detail_parts)
            safe_bot_call(bot.edit_message_text, detail_text, chat_id, call.message.message_id, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error handling view_del callback: {e}")

    elif call.data.startswith("dl_media_"):
        safe_bot_call(bot.answer_callback_query, call.id, "📥 جاري إرسال الوسائط...")
        try:
            notif_id = int(call.data.split("_")[2])
            with _notification_data_lock:
                ndata = _notification_data.get(notif_id)
            if not ndata or ndata.get('type') != 'delete':
                safe_bot_call(bot.send_message, chat_id, "⚠️ بيانات الإشعار غير متوفرة بعد الآن.", parse_mode="HTML")
                return

            file_id = ndata.get('file_id')
            media_type = ndata.get('media_type')
            local_path = ndata.get('local_path')
            owner_user_id = ndata.get('owner_user_id')
            # Use original caption if it exists, otherwise no caption
            original_caption = ndata.get('caption') or None

            if file_id and media_type:
                send_saved_media(owner_user_id, file_id, media_type, original_caption, local_path)
            else:
                safe_bot_call(bot.send_message, chat_id, "⚠️ لا توجد وسائط متاحة للتحميل.", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error handling dl_media callback: {e}")

    # ---- Edit Notification Inline Button Callbacks ----
    elif call.data.startswith("view_edit_"):
        safe_bot_call(bot.answer_callback_query, call.id)
        try:
            notif_id = int(call.data.split("_")[2])
            with _notification_data_lock:
                ndata = _notification_data.get(notif_id)
            if not ndata or ndata.get('type') != 'edit':
                safe_bot_call(bot.edit_message_text, "⚠️ بيانات الإشعار غير متوفرة بعد الآن.", chat_id, call.message.message_id)
                return

            # Build full detail text showing both old and new text
            detail_parts = [
                f"✏️ <b>تفاصيل الرسالة المعدلة</b>\n",
                f"👤 <b>العميل:</b> {escape_html(ndata.get('customer_name', 'غير معروف'))}",
                f"🆔 <b>آيدي المحادثة:</b> <code>{ndata.get('chat_id', '')}</code>",
                f"✏️ <b>وقت التعديل:</b> <code>{ndata.get('edit_time', '')}</code>",
            ]

            # Show old text
            old_text = ndata.get('old_text', '')
            old_caption = ndata.get('old_caption', '')
            if old_text:
                detail_parts.append(f"\n📝 <b>النص قبل التعديل:</b>\n« {escape_html(old_text)} »")
            if old_caption:
                detail_parts.append(f"📝 <b>التعليق قبل التعديل:</b>\n« {escape_html(old_caption)} »")

            # Show new text
            new_text = ndata.get('new_text', '')
            if new_text:
                detail_parts.append(f"\n📝 <b>النص بعد التعديل:</b>\n« {escape_html(new_text)} »")

            if ndata.get('media_type'):
                _, media_label = _get_media_emoji_label(ndata['media_type'])
                detail_parts.append(f"\n📦 <b>نوع الوسائط:</b> {media_label} (<code>{ndata['media_type']}</code>)")

            detail_text = "\n".join(detail_parts)
            safe_bot_call(bot.edit_message_text, detail_text, chat_id, call.message.message_id, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error handling view_edit callback: {e}")

    elif call.data.startswith("dl_edit_"):
        safe_bot_call(bot.answer_callback_query, call.id, "📥 جاري إرسال الوسائط...")
        try:
            notif_id = int(call.data.split("_")[2])
            with _notification_data_lock:
                ndata = _notification_data.get(notif_id)
            if not ndata or ndata.get('type') != 'edit':
                safe_bot_call(bot.send_message, chat_id, "⚠️ بيانات الإشعار غير متوفرة بعد الآن.", parse_mode="HTML")
                return

            file_id = ndata.get('file_id')
            media_type = ndata.get('media_type')
            local_path = ndata.get('local_path')
            owner_user_id = ndata.get('owner_user_id')
            # Use original caption if it exists, otherwise no caption
            original_caption = ndata.get('old_caption') or None

            if file_id and media_type:
                send_saved_media(owner_user_id, file_id, media_type, original_caption, local_path)
            else:
                safe_bot_call(bot.send_message, chat_id, "⚠️ لا توجد وسائط متاحة للتحميل.", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error handling dl_edit callback: {e}")

    # dl_chat_ callback removed - full chat deletion export feature removed

# ==========================================
# User Input & Media Handlers
# ==========================================

@bot.message_handler(func=lambda msg: msg.chat.type == "private", content_types=['text', 'photo', 'video', 'document'])
def handle_user_inputs(message):
    if not is_user_allowed(message):
        return

    user_id = str(message.from_user.id)
    chat_id = message.chat.id
    data = load_data()

    state = data["states"].get(user_id)
    conn_id = data["connections"].get(user_id)

    if not state:
        if conn_id:
            safe_bot_call(bot.send_message, chat_id, "يرجى استخدام الأزرار المتاحة لإدارة الميزات:", reply_markup=get_main_keyboard())
        else:
            send_welcome(message)
        return

    if not conn_id:
        safe_bot_call(bot.send_message, chat_id, "⚠️ <b>خطأ: البوت غير مربوط بحساب بيزنس حالياً.</b>", parse_mode="HTML")
        data["states"][user_id] = None
        save_data(data)
        return

    menu_msg_id = data.get("menu_message_ids", {}).get(user_id)
    prompt_msg_id = data.get("prompt_message_ids", {}).get(user_id)

    should_delete_user_msg = True
    if message.photo or message.video or message.document:
        should_delete_user_msg = False

    if "menu_message_ids" in data and user_id in data["menu_message_ids"]:
        del data["menu_message_ids"][user_id]
    if "prompt_message_ids" in data and user_id in data["prompt_message_ids"]:
        del data["prompt_message_ids"][user_id]
    save_data(data)

    text = message.text.strip() if message.text else ""

    if state == "AWAITING_FETCH_PERSON_ID":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال آيدي صالح كنص.</b>", parse_mode="HTML")
            return
        target_id = text.replace(" ", "").replace("`", "")
        if not target_id.lstrip("-").isdigit():
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>الآيدي يجب أن يكون رقماً فقط.</b>\nمثال: <code>123456789</code>", parse_mode="HTML")
            return

        if "fetch_message_targets" not in data: data["fetch_message_targets"] = {}
        data["fetch_message_targets"][user_id] = target_id
        data["states"][user_id] = "AWAITING_FETCH_COUNT"
        save_data(data)

        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        delete_messages_sequentially(chat_id, msg_ids)

        prompt = safe_bot_call(
            bot.send_message, chat_id,
            f"✅ <b>تم حفظ الآيدي:</b> <code>{escape_html(target_id)}</code>\n\n"
            f"أرسل الآن عدد الرسائل المطلوب جلبها.\n"
            f"الحد الأقصى في الطلب الواحد: <code>{MAX_FETCH_MESSAGES}</code>",
            parse_mode="HTML"
        )
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = prompt.message_id
        save_data(data)

    elif state == "AWAITING_FETCH_COUNT":
        if not text or not text.isdigit():
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال عدد صحيح فقط.</b>\nمثال: <code>20</code>", parse_mode="HTML")
            return

        requested_count = int(text)
        if requested_count <= 0:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>العدد يجب أن يكون أكبر من صفر.</b>", parse_mode="HTML")
            return

        target_id = data.get("fetch_message_targets", {}).get(user_id)
        if not target_id:
            data["states"][user_id] = None
            save_data(data)
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>لم يتم العثور على الآيدي المطلوب. ابدأ من زر جلب رسائل مرة ثانية.</b>", parse_mode="HTML", reply_markup=get_main_keyboard())
            return

        safe_count = min(requested_count, MAX_FETCH_MESSAGES)
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري البحث في الرسائل المحفوظة...</b>", parse_mode="HTML")

        msg_ids = []
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)

        rows = get_cached_messages_for_person(target_id, safe_count)
        data["states"][user_id] = None
        if "fetch_message_targets" in data and user_id in data["fetch_message_targets"]:
            del data["fetch_message_targets"][user_id]
        save_data(data)

        if not rows:
            safe_bot_call(
                bot.send_message, chat_id,
                "⚠️ <b>لم أجد رسائل محفوظة لهذا الآيدي داخل كاش البوت.</b>\n\n"
                f"🆔 الآيدي: <code>{escape_html(target_id)}</code>\n"
                "ملاحظة: يمكن جلب الرسائل التي وصلت للبوت سابقاً فقط، ولا يمكن للبوت سحب سجل محادثة كامل من تيليجرام إذا لم يكن محفوظاً لديه.",
                parse_mode="HTML", reply_markup=get_main_keyboard()
            )
            return

        summary = (
            "✅ <b>تم العثور على الرسائل المطلوبة.</b>\n\n"
            f"🆔 <b>الآيدي:</b> <code>{escape_html(target_id)}</code>\n"
            f"🔢 <b>المطلوب:</b> <code>{requested_count}</code>\n"
            f"📦 <b>المتاح/المرسل:</b> <code>{len(rows)}</code>"
        )
        if requested_count > MAX_FETCH_MESSAGES:
            summary += f"\n⚠️ تم تقليل العدد إلى الحد الأقصى: <code>{MAX_FETCH_MESSAGES}</code>"
        safe_bot_call(bot.send_message, chat_id, summary, parse_mode="HTML")
        send_cached_messages_to_owner(chat_id, rows)
        safe_bot_call(bot.send_message, chat_id, "🏆 <b>القائمة الرئيسية:</b>", parse_mode="HTML", reply_markup=get_main_keyboard())

    elif state == "AWAITING_NAME":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال نص اسم صالح.</b>", parse_mode="HTML")
            return
        parts = text.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
        old_name = "غير معروف"
        conn_res = api_get_business_connection(TOKEN, conn_id)
        if conn_res.get("ok"):
            u = conn_res["result"]["user"]
            old_name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري تعديل الاسم...</b>", parse_mode="HTML")
        res = api_set_business_name(TOKEN, conn_id, first_name, last_name)
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            new_name = f"{first_name} {last_name}".strip()
            safe_bot_call(bot.send_message, chat_id, "<b>تم تغيير الاسم بنجاح</b>\n\n" + f"الاسم القديم: <code>{escape_html(old_name)}</code>\nالاسم الجديد: <code>{escape_html(new_name)}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل تحديث الاسم.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        data["states"][user_id] = None
        save_data(data)

    elif state == "AWAITING_BIO":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال نص بايو صالح.</b>", parse_mode="HTML")
            return
        if len(text) > 70:
            safe_bot_call(bot.send_message, chat_id, f"⚠️ <b>النص طويل جداً ({len(text)} حرف). يرجى إرسال بايو لا يتخطى 70 حرفاً.</b>", parse_mode="HTML")
            return
        old_bio = data.get("last_bios", {}).get(user_id, "غير معروف")
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري تعديل البايو...</b>", parse_mode="HTML")
        res = api_set_business_bio(TOKEN, conn_id, text)
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            if "last_bios" not in data: data["last_bios"] = {}
            data["last_bios"][user_id] = text
            save_data(data)
            safe_bot_call(bot.send_message, chat_id, "<b>تم تغيير البايو بنجاح</b>\n\n" + f"البايو القديم: <code>{escape_html(old_bio)}</code>\nالبايو الجديد: <code>{escape_html(text)}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل التحديث.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        data["states"][user_id] = None
        save_data(data)

    elif state == "AWAITING_USERNAME":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال يوزر صالح.</b>", parse_mode="HTML")
            return
        new_user = text.replace("@", "")
        old_username = "لا يوجد"
        conn_res = api_get_business_connection(TOKEN, conn_id)
        if conn_res.get("ok"):
            u = conn_res["result"]["user"]
            old_username = f"@{u.get('username')}" if u.get('username') else "لا يوجد"
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري تغيير اليوزر...</b>", parse_mode="HTML")
        res = api_set_business_username(TOKEN, conn_id, new_user)
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if res.get("ok"):
            new_user_display = f"@{new_user}" if new_user else "لا يوجد"
            safe_bot_call(bot.send_message, chat_id, "<b>تم تغيير اليوزر بنجاح</b>\n\n" + f"اليوزر القديم: <code>{escape_html(old_username)}</code>\nاليوزر الجديد: <code>{escape_html(new_user_display)}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل التغيير.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        data["states"][user_id] = None
        save_data(data)

    elif state == "AWAITING_REPLY_TEXT":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال نص صالح.</b>", parse_mode="HTML")
            return
        if "auto_reply_text" not in data: data["auto_reply_text"] = {}
        data["auto_reply_text"][user_id] = text
        save_data(data)
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري حفظ نص الرد التلقائي الجديد...</b>", parse_mode="HTML")
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        safe_bot_call(bot.send_message, chat_id, "✅ <b>تم حفظ نص الرد التلقائي الجديد بنجاح!</b>", parse_mode="HTML", reply_markup=get_autoreply_keyboard(user_id))
        data["states"][user_id] = None
        save_data(data)

    elif state in ["AWAITING_CUSTOM_DELETE", "AWAITING_CUSTOM_PIN", "AWAITING_CUSTOM_UNPIN", "AWAITING_CUSTOM_ID", "AWAITING_CUSTOM_NOTE", "AWAITING_CUSTOM_HELP"]:
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال نص صالح لتعيينه كأمر.</b>", parse_mode="HTML")
            return
        if len(text) > 20:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>الكلمة طويلة جداً، يرجى اختيار كلمة أو رمز قصير لا يتعدى 20 حرفاً.</b>", parse_mode="HTML")
            return
        if "custom_commands" not in data: data["custom_commands"] = {}
        if user_id not in data["custom_commands"]: data["custom_commands"][user_id] = {}
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري حفظ الأمر الجديد...</b>", parse_mode="HTML")
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        if msg.message_id: msg_ids.append(msg.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        if state == "AWAITING_CUSTOM_DELETE":
            data["custom_commands"][user_id]["delete"] = text
            safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم تغيير أمر الحذف بنجاح إلى:</b> <code>{escape_html(text)}</code>", parse_mode="HTML")
        elif state == "AWAITING_CUSTOM_PIN":
            data["custom_commands"][user_id]["pin"] = text
            safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم تغيير أمر التثبيت بنجاح إلى:</b> <code>{escape_html(text)}</code>", parse_mode="HTML")
        elif state == "AWAITING_CUSTOM_UNPIN":
            data["custom_commands"][user_id]["unpin"] = text
            safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم تغيير أمر إلغاء التثبيت بنجاح إلى:</b> <code>{escape_html(text)}</code>", parse_mode="HTML")
        elif state == "AWAITING_CUSTOM_ID":
            data["custom_commands"][user_id]["id"] = text
            safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم تغيير أمر الآيدي بنجاح إلى:</b> <code>{escape_html(text)}</code>", parse_mode="HTML")
        elif state == "AWAITING_CUSTOM_NOTE":
            data["custom_commands"][user_id]["note"] = text
            safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم تغيير أمر الملاحظة بنجاح إلى:</b> <code>{escape_html(text)}</code>", parse_mode="HTML")
        elif state == "AWAITING_CUSTOM_HELP":
            data["custom_commands"][user_id]["help"] = text
            safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم تغيير أمر المساعدة بنجاح إلى:</b> <code>{escape_html(text)}</code>", parse_mode="HTML")
        data["states"][user_id] = None
        save_data(data)
        show_custom_cmds_menu(chat_id, user_id)

    elif state == "AWAITING_STORY":
        # Get the user's chosen background type (default to blurred for backward compat)
        story_bg = data.get("story_bg_type", {}).get(user_id, "blurred")
        # Get the user's chosen active period (default to 86400 = 24h)
        story_active_period = data.get("story_active_period", {}).get(user_id, 86400)
        
        caption = message.caption if message.caption else None
        file_id = None
        media_type = None
        video_width = None
        video_height = None
        video_duration = None

        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
            video_duration = message.video.duration if message.video.duration else None
        elif message.document:
            file_id = message.document.file_id
            mime = message.document.mime_type or ""
            if "video" in mime:
                media_type = "video"
            elif "image" in mime:
                media_type = "photo"
            else:
                safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال صورة أو مقطع فيديو فقط لنشره كقصة.</b>", parse_mode="HTML")
                return
        else:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال صورة أو مقطع فيديو فقط لنشره كقصة.</b>", parse_mode="HTML")
            return

        bg_label = "ضبابية" if story_bg == "blurred" else ("بيضاء" if story_bg == "white" else "سوداء")
        msg = safe_bot_call(
            bot.send_message,
            chat_id,
            f"⏳ <b>جاري تحميل وتجهيز الاستوري بنسبة 9:16 مع خلفية {bg_label} وبدون تمديد أو تخريب للأبعاد...</b>",
            parse_mode="HTML"
        )

        input_ext = ".mp4" if media_type == "video" else ".jpg"
        output_ext = ".mp4" if media_type == "video" else ".jpg"
        temp_filepath = make_safe_temp_path("temp_story", file_id, input_ext)
        processed_filepath = make_safe_temp_path("processed_story", file_id, output_ext)

        try:
            file_info = safe_bot_call(bot.get_file, file_id)
            file_path = file_info.file_path
            downloaded_file = safe_bot_call(bot.download_file, file_path)
            if not downloaded_file or len(downloaded_file) == 0:
                raise Exception("فشل تحميل محتوى الملف من سيرفرات تليجرام.")
            with open(temp_filepath, 'wb') as new_file:
                new_file.write(downloaded_file)

            # Process media with the chosen background type
            if media_type == "video":
                if not process_story_video(temp_filepath, processed_filepath, bg_type=story_bg):
                    raise Exception("فشل تجهيز الفيديو بنسبة 9:16. تأكد من تثبيت FFmpeg وأن الملف صالح.")
                upload_filepath = processed_filepath
                video_width = STORY_WIDTH
                video_height = STORY_HEIGHT
                _, _, detected_duration = get_video_metadata(processed_filepath)
                if detected_duration:
                    video_duration = detected_duration
            else:
                if not process_story_photo(temp_filepath, processed_filepath, bg_type=story_bg):
                    raise Exception("فشل تجهيز الصورة بنسبة 9:16.")
                upload_filepath = processed_filepath

            logger.info(f"[Story] Uploading processed {media_type} (bg={story_bg}) - dimensions: {video_width}x{video_height}, duration: {video_duration}")

            with open(upload_filepath, 'rb') as f_obj:
                res = api_post_story_with_file(
                    TOKEN, conn_id, media_type, f_obj, caption,
                    video_width=video_width, video_height=video_height,
                    video_duration=video_duration,
                    active_period=story_active_period
                )

            msg_ids = []
            if menu_msg_id: msg_ids.append(menu_msg_id)
            if prompt_msg_id: msg_ids.append(prompt_msg_id)
            if should_delete_user_msg: msg_ids.append(message.message_id)
            if msg.message_id: msg_ids.append(msg.message_id)
            delete_messages_sequentially(chat_id, msg_ids)

            if res.get("ok"):
                story_id = res.get("result", {}).get("id")
                if story_id:
                    if "last_story_ids" not in data: data["last_story_ids"] = {}
                    data["last_story_ids"][user_id] = story_id
                    save_data(data)
                safe_bot_call(
                    bot.send_message,
                    chat_id,
                    f"✅ <b>تم نشر القصة بنجاح بعد تجهيزها 9:16 مع خلفية {bg_label} وبدون تمديد للفيديو.</b>",
                    parse_mode="HTML",
                    reply_markup=get_profile_keyboard()
                )
            else:
                safe_bot_call(
                    bot.send_message,
                    chat_id,
                    f"❌ <b>فشل نشر القصة.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>",
                    parse_mode="HTML",
                    reply_markup=get_profile_keyboard()
                )
        except Exception as e:
            msg_ids = []
            if menu_msg_id: msg_ids.append(menu_msg_id)
            if prompt_msg_id: msg_ids.append(prompt_msg_id)
            if should_delete_user_msg: msg_ids.append(message.message_id)
            if msg.message_id: msg_ids.append(msg.message_id)
            delete_messages_sequentially(chat_id, msg_ids)
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>حدث خطأ أثناء معالجة القصة:</b>\n<code>{escape_html(str(e))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
            logger.error(f"Story processing error: {e}")
        finally:
            for cleanup_path in (temp_filepath, processed_filepath):
                if cleanup_path and os.path.exists(cleanup_path):
                    try:
                        os.remove(cleanup_path)
                    except Exception as ex:
                        logger.error(f"Failed to delete temp file {cleanup_path}: {ex}")
        data["states"][user_id] = None
        # Clean up the bg type and active period choices after use
        if "story_bg_type" in data and user_id in data["story_bg_type"]:
            del data["story_bg_type"][user_id]
        if "story_active_period" in data and user_id in data["story_active_period"]:
            del data["story_active_period"][user_id]
        save_data(data)

    elif state == "AWAITING_PHOTO":
        is_video_photo = False
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.video:
            file_id = message.video.file_id
            is_video_photo = True
        else:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال صورة مربعة أو مقطع فيديو صالحة لتغيير البروفايل.</b>", parse_mode="HTML")
            return
        msg = safe_bot_call(bot.send_message, chat_id, "⏳ <b>جاري سحب الصورة وإعادة تهيئتها لتغيير بروفايل البيزنس...</b>", parse_mode="HTML")
        try:
            file_info = safe_bot_call(bot.get_file, file_id)
            file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
            res_get = safe_request(file_url, method="get", stream=True, timeout=120)
            msg_ids = []
            if menu_msg_id: msg_ids.append(menu_msg_id)
            if prompt_msg_id: msg_ids.append(prompt_msg_id)
            if should_delete_user_msg: msg_ids.append(message.message_id)
            if msg.message_id: msg_ids.append(msg.message_id)
            delete_messages_sequentially(chat_id, msg_ids)
            if not res_get.get("ok"):
                safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل جلب الصورة.</b>\nالسبب: <code>{escape_html(res_get.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
                return
            r_obj = res_get["response_obj"]
            file_bytes = r_obj.content
            res = api_set_business_profile_photo(TOKEN, conn_id, file_bytes, is_video_photo)
            if res.get("ok"):
                safe_bot_call(bot.send_message, chat_id, "✅ <b>تم تغيير صورة بروفايل البيزنس بنجاح!</b>", parse_mode="HTML", reply_markup=get_profile_keyboard())
            else:
                safe_bot_call(bot.send_message, chat_id, f"❌ <b>فشل تعديل الصورة.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
        except Exception as e:
            msg_ids = []
            if menu_msg_id: msg_ids.append(menu_msg_id)
            if prompt_msg_id: msg_ids.append(prompt_msg_id)
            if should_delete_user_msg: msg_ids.append(message.message_id)
            if msg.message_id: msg_ids.append(msg.message_id)
            delete_messages_sequentially(chat_id, msg_ids)
            safe_bot_call(bot.send_message, chat_id, f"❌ <b>حدث خطأ برمجى أثناء التعديل:</b>\n<code>{escape_html(str(e))}</code>", parse_mode="HTML", reply_markup=get_profile_keyboard())
            logger.error(f"Photo processing error: {e}")
        data["states"][user_id] = None
        save_data(data)

    elif state == "AWAITING_SHORTCUT_TRIGGER":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال رمز أو كلمة اختصار صالحة.</b>", parse_mode="HTML")
            return
        if "temp_triggers" not in data: data["temp_triggers"] = {}
        data["temp_triggers"][user_id] = text
        data["states"][user_id] = "AWAITING_SHORTCUT_TEXT"
        save_data(data)
        msg = safe_bot_call(bot.send_message, chat_id, f"📝 <b>تم حفظ الاختصار [ <code>{escape_html(text)}</code> ] بنجاح.</b>\n\nأرسل الآن النص الكامل الذي ترغب في أن يظهر مكانه عند الإرسال:", parse_mode="HTML")
        if "prompt_message_ids" not in data: data["prompt_message_ids"] = {}
        data["prompt_message_ids"][user_id] = msg.message_id
        save_data(data)
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        delete_messages_sequentially(chat_id, msg_ids)

    elif state == "AWAITING_SHORTCUT_TEXT":
        if not text:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>يرجى إرسال نص صالح لتعيينه.</b>", parse_mode="HTML")
            return
        trigger = data.get("temp_triggers", {}).get(user_id)
        if not trigger:
            safe_bot_call(bot.send_message, chat_id, "⚠️ <b>حدث خطأ غير متوقع، لم يتم العثور على رمز الاختصار. يرجى البدء من جديد.</b>", parse_mode="HTML")
            data["states"][user_id] = None
            save_data(data)
            return
        if "shortcuts" not in data: data["shortcuts"] = {}
        if user_id not in data["shortcuts"]: data["shortcuts"][user_id] = {}
        data["shortcuts"][user_id][trigger] = text
        data["states"][user_id] = None
        if "temp_triggers" in data and user_id in data["temp_triggers"]:
            del data["temp_triggers"][user_id]
        save_data(data)
        msg_ids = []
        if menu_msg_id: msg_ids.append(menu_msg_id)
        if prompt_msg_id: msg_ids.append(prompt_msg_id)
        if should_delete_user_msg: msg_ids.append(message.message_id)
        delete_messages_sequentially(chat_id, msg_ids)
        safe_bot_call(bot.send_message, chat_id, f"✅ <b>تم حفظ الاختصار بنجاح!</b>\n\nعند إرسال: <code>{escape_html(trigger)}</code>\nسيقوم البوت بتعديلها إلى: <i>{escape_html(text)}</i>", parse_mode="HTML")
        safe_bot_call(bot.send_message, chat_id, "⚡ <b>قسم اختصارات النصوص السريعة:</b>", parse_mode="HTML", reply_markup=get_shortcuts_keyboard())

# ==========================================
# Business Message Processing
# ==========================================

def process_business_message(message, is_edit=False):
    data = load_data()
    owner_user_id = None

    for uid, conn_id in data["connections"].items():
        if conn_id == message.business_connection_id:
            owner_user_id = uid
            break

    if not owner_user_id:
        return

    if owner_user_id not in data.get("activated_users", []):
        return

    is_incoming = str(message.from_user.id) != owner_user_id

    if is_edit and is_incoming:
        notify_edits = data.get("notify_edits", {}).get(owner_user_id, True)
        if not notify_edits:
            logger.info(f"Edit notification disabled for owner {owner_user_id}")
            return

        name = message.from_user.first_name or ""
        if message.from_user.last_name:
            name += f" {message.from_user.last_name}"
        name = name.strip() or "غير معروف"
        username_text = f"@{message.from_user.username}" if message.from_user.username else "لا يوجد"

        cached_row = get_cached_message(message.chat.id, message.message_id)

        # Retrieve the OLD text/caption that was saved by pre-cache BEFORE overwriting
        edit_cache_key = f"{message.chat.id}_{message.message_id}"
        old_text = ""
        old_caption = ""
        with _edit_old_data_lock:
            old_data = _edit_old_data.pop(edit_cache_key, None)
        if old_data:
            old_text = old_data.get("old_text", "")
            old_caption = old_data.get("old_caption", "")
            logger.info(f"[EDIT] Retrieved old text from _edit_old_data for msg_id={message.message_id}: '{old_text[:50]}...'")
        elif cached_row:
            # Fallback: try getting old text from cache (may already be overwritten)
            old_text = cached_row.get("text") or ""
            old_caption = cached_row.get("caption") or ""
            logger.warning(f"[EDIT] _edit_old_data was empty for msg_id={message.message_id}, using cache (may be same as new text)")

        new_text = message.text or message.caption or "[وسائط أو ملف]"
        sender_id = message.from_user.id
        sender_name = name

        cache_message(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=message.text or "",
            sender_id=sender_id,
            sender_name=sender_name,
            file_id=cached_row.get("file_id") if cached_row else None,
            media_type=cached_row.get("media_type") if cached_row else None,
            caption=message.caption or "",
            local_path=cached_row.get("local_path") if cached_row else None,
            send_date=cached_row.get("send_date") if cached_row else None
        )

        # Precise timestamps
        edit_time_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Get media info from cached_row
        file_id = cached_row.get("file_id") if cached_row else None
        media_type = cached_row.get("media_type") if cached_row else None
        local_path = cached_row.get("local_path") if cached_row else None
        send_date_str = cached_row.get("send_date") if cached_row else None

        # Send compact edit notification with inline buttons (shows both old and new text)
        send_compact_edit_notification(
            owner_user_id=owner_user_id,
            customer_name=name,
            chat_id=message.chat.id,
            msg_id=message.message_id,
            old_text=old_text,
            old_caption=old_caption,
            new_text=new_text,
            sender_name=sender_name,
            edit_time_str=edit_time_str,
            file_id=file_id,
            media_type=media_type,
            local_path=local_path,
            send_date_str=send_date_str
        )
        return

    # Extract media properties to cache them locally
    text = message.text or ""
    caption = message.caption or ""
    file_id = None
    media_type = None
    local_path = None
    suffix = ""

    # Log the message content_type and available attributes for debugging
    msg_content_type = getattr(message, 'content_type', 'unknown')
    msg_json_debug = {}
    try:
        msg_json_debug = message.json if hasattr(message, 'json') and message.json else {}
        if not msg_json_debug and hasattr(message, '_json'):
            msg_json_debug = message._json
    except Exception:
        pass
    json_keys = list(msg_json_debug.keys()) if msg_json_debug else []
    # Identify media-related keys in JSON (exclude common text keys)
    media_keys = [k for k in json_keys if k not in ('message_id', 'from', 'chat', 'date', 'text', 'caption', 'business_connection_id', 'reply_to_message', 'forward_from', 'forward_date', 'edit_date', 'entities', 'caption_entities')]
    logger.info(f"[Debug] msg_id={message.message_id} | content_type={msg_content_type} | json_media_keys={media_keys}")

    # If content_type is unknown/unusual, log it prominently for debugging
    known_types = {'text', 'photo', 'video', 'document', 'voice', 'audio', 'sticker', 'animation',
                   'video_note', 'contact', 'location', 'venue', 'poll', 'dice', 'game', 'story',
                   'invoice', 'successful_payment', 'connected_website', 'passport_data', 'web_app_data',
                   'new_chat_members', 'left_chat_member', 'new_chat_title', 'new_chat_photo',
                   'delete_chat_photo', 'group_chat_created', 'supergroup_chat_created',
                   'channel_chat_created', 'pinned_message', 'migrate_from_chat_id', 'migrate_to_chat_id',
                   'message_auto_delete_timer_changed', 'proximity_alert_triggered', 'forum_topic_created',
                   'forum_topic_edited', 'forum_topic_closed', 'forum_topic_reopened',
                   'general_forum_topic_hidden', 'general_forum_topic_unhidden', 'write_access_allowed',
                   'user_shared', 'chat_shared', 'video_chat_scheduled', 'video_chat_started',
                   'video_chat_ended', 'video_chat_participants_invited', 'giveaway_created', 'giveaway',
                   'giveaway_winners', 'giveaway_completed'}
    if msg_content_type not in known_types:
        logger.warning(f"[UNKNOWN TYPE] msg_id={message.message_id} | content_type={msg_content_type} | ALL keys={json_keys}")
        logger.warning(f"[UNKNOWN TYPE] Full JSON for msg_id={message.message_id}: {json.dumps(msg_json_debug, default=str)[:1000] if msg_json_debug else 'N/A'}")

    # Primary: Try structured message attributes first
    # Check for paid_media FIRST - paid media messages may not have regular photo/video fields
    if hasattr(message, 'paid_media') and message.paid_media:
        paid_info = message.paid_media
        if hasattr(paid_info, 'paid_media') and paid_info.paid_media:
            first_media = paid_info.paid_media[0] if len(paid_info.paid_media) > 0 else None
            if first_media:
                if hasattr(first_media, 'photo') and first_media.photo:
                    file_id = first_media.photo[-1].file_id
                    media_type = "paid_photo"
                    suffix = ".jpg"
                elif hasattr(first_media, 'video') and first_media.video:
                    file_id = first_media.video.file_id
                    media_type = "paid_video"
                    suffix = ".mp4"
                else:
                    media_type = "paid_media"
                    logger.info(f"[Paid Media] msg_id={message.message_id} | Unknown paid media type: {type(first_media)}")
    
    if not file_id and not media_type:
        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
            suffix = ".jpg"
            # Check if photo has spoiler
            if getattr(message, 'has_media_spoiler', False):
                media_type = "photo_spoiler"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
            suffix = ".mp4"
        elif message.document:
            file_id = message.document.file_id
            media_type = "document"
            if message.document.file_name:
                _, ext = os.path.splitext(message.document.file_name)
                suffix = ext
            else:
                suffix = ".doc"
        elif message.voice:
            file_id = message.voice.file_id
            media_type = "voice"
            suffix = ".ogg"
        elif message.audio:
            file_id = message.audio.file_id
            media_type = "audio"
            suffix = ".mp3"
        elif message.sticker:
            file_id = message.sticker.file_id
            media_type = "sticker"
            suffix = ".webp"
        elif message.animation:
            file_id = message.animation.file_id
            media_type = "animation"
            suffix = ".mp4"
        elif message.video_note:
            file_id = message.video_note.file_id
            media_type = "video_note"
            suffix = ".mp4"

    # Fallback: Extract from raw JSON if structured attributes didn't work
    # This catches: paid_media, photos with spoiler, and any future unknown types
    if not file_id and not media_type and msg_json_debug:
        try:
            # Check for paid_media first (Telegram Stars paid content - new type)
            if 'paid_media' in msg_json_debug and isinstance(msg_json_debug['paid_media'], dict):
                paid_info = msg_json_debug['paid_media']
                paid_list = paid_info.get('paid_media', [])
                if paid_list and isinstance(paid_list, list) and len(paid_list) > 0:
                    first_media = paid_list[0]
                    if 'photo' in first_media and isinstance(first_media['photo'], list):
                        photos = first_media['photo']
                        file_id = photos[-1].get('file_id')
                        media_type = "paid_photo"
                        suffix = ".jpg"
                    elif 'video' in first_media and isinstance(first_media['video'], dict):
                        file_id = first_media['video'].get('file_id')
                        media_type = "paid_video"
                        suffix = ".mp4"
                    else:
                        # Generic paid media
                        media_type = "paid_media"
                        logger.info(f"[Paid Media] msg_id={message.message_id} | paid_media structure: {json.dumps(first_media, default=str)[:500]}")
            # Check each media type in the raw JSON
            elif 'photo' in msg_json_debug and isinstance(msg_json_debug['photo'], list) and len(msg_json_debug['photo']) > 0:
                photos = msg_json_debug['photo']
                file_id = photos[-1].get('file_id', photos[-1].get('fileId'))
                media_type = "photo"
                suffix = ".jpg"
                if msg_json_debug.get('has_media_spoiler'):
                    media_type = "photo_spoiler"
            elif 'video' in msg_json_debug and isinstance(msg_json_debug['video'], dict):
                file_id = msg_json_debug['video'].get('file_id', msg_json_debug['video'].get('fileId'))
                media_type = "video"
                suffix = ".mp4"
            elif 'document' in msg_json_debug and isinstance(msg_json_debug['document'], dict):
                file_id = msg_json_debug['document'].get('file_id', msg_json_debug['document'].get('fileId'))
                media_type = "document"
                fname = msg_json_debug['document'].get('file_name', msg_json_debug['document'].get('fileName', ''))
                if fname:
                    _, ext = os.path.splitext(fname)
                    suffix = ext if ext else ".doc"
                else:
                    suffix = ".doc"
            elif 'voice' in msg_json_debug and isinstance(msg_json_debug['voice'], dict):
                file_id = msg_json_debug['voice'].get('file_id', msg_json_debug['voice'].get('fileId'))
                media_type = "voice"
                suffix = ".ogg"
            elif 'audio' in msg_json_debug and isinstance(msg_json_debug['audio'], dict):
                file_id = msg_json_debug['audio'].get('file_id', msg_json_debug['audio'].get('fileId'))
                media_type = "audio"
                suffix = ".mp3"
            elif 'sticker' in msg_json_debug and isinstance(msg_json_debug['sticker'], dict):
                file_id = msg_json_debug['sticker'].get('file_id', msg_json_debug['sticker'].get('fileId'))
                media_type = "sticker"
                suffix = ".webp"
            elif 'animation' in msg_json_debug and isinstance(msg_json_debug['animation'], dict):
                file_id = msg_json_debug['animation'].get('file_id', msg_json_debug['animation'].get('fileId'))
                media_type = "animation"
                suffix = ".mp4"
            elif 'video_note' in msg_json_debug and isinstance(msg_json_debug['video_note'], dict):
                file_id = msg_json_debug['video_note'].get('file_id', msg_json_debug['video_note'].get('fileId'))
                media_type = "video_note"
                suffix = ".mp4"
            else:
                # Unknown media type - log all keys for debugging
                logger.warning(f"[Unknown Media] msg_id={message.message_id} | content_type={msg_content_type} | Could not extract media. All keys: {json_keys}")

            # Also get caption from JSON if not already set
            if not caption and 'caption' in msg_json_debug:
                caption = msg_json_debug['caption']
        except Exception as e:
            logger.error(f"[Media] JSON fallback extraction failed: {e}")

    logger.info(f"[Media] msg_id={message.message_id} | media_type={media_type} | file_id={bool(file_id)} | "
                f"caption={bool(caption)} | text={bool(text)}")

    # Cache the message FIRST (with file_id and media_type even before download)
    # This ensures the message is always archived immediately
    sender_id = message.from_user.id
    sender_name = message.from_user.first_name or ""
    if message.from_user.last_name:
        sender_name += f" {message.from_user.last_name}"
    sender_name = sender_name.strip() or "غير معروف"

    # Record the original send date from Telegram message
    send_date_str = ""
    if message.date:
        send_date_str = datetime.datetime.fromtimestamp(message.date, tz=datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    cache_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        sender_id=sender_id,
        sender_name=sender_name,
        file_id=file_id,
        media_type=media_type,
        caption=caption,
        local_path=None,  # Will be updated by background download
        send_date=send_date_str
    )
    logger.info(f"[Cache] Cached message {message.message_id} in chat {message.chat.id} | media_type={media_type} | file_id={bool(file_id)}")

    # Download media in background thread so it doesn't block the message handler
    # The cache is updated with local_path after download completes
    if file_id and media_type:
        def _bg_download():
            try:
                logger.info(f"[BG Download] Starting background download for {media_type} msg_id={message.message_id}")
                result_path = download_media_safely(file_id, suffix)
                if result_path:
                    update_cached_local_path(message.chat.id, message.message_id, result_path)
                    logger.info(f"[BG Download] Completed for msg_id={message.message_id}: {result_path}")
                else:
                    logger.warning(f"[BG Download] Failed for msg_id={message.message_id} - no local backup will be available")
            except Exception as e:
                logger.error(f"[BG Download] Unexpected error for msg_id={message.message_id}: {e}")

        bg_thread = threading.Thread(target=_bg_download, daemon=True)
        bg_thread.start()

    # Shortcut expansion for outgoing messages
    if not is_incoming and not is_edit:
        msg_text = message.text.strip() if message.text else ""
        user_shortcuts = data.get("shortcuts", {}).get(owner_user_id, {})
        if msg_text in user_shortcuts:
            expanded_text = user_shortcuts[msg_text]
            api_edit_business_message(TOKEN, message.business_connection_id, message.chat.id, message.message_id, expanded_text)
            return

    if not is_edit:
        auto_read_enabled = data.get("auto_read", {}).get(owner_user_id, False)
        if auto_read_enabled and is_incoming:
            api_read_business_message(TOKEN, message.business_connection_id, message.chat.id, message.message_id)
            logger.info(f"Marked message {message.message_id} as read in chat {message.chat.id}")

        auto_reply_enabled = data.get("auto_reply_enabled", {}).get(owner_user_id, False)
        if auto_reply_enabled and is_incoming and message.chat.type == "private":
            chat_key = f"{owner_user_id}_{message.chat.id}"
            if chat_key not in REPLIED_CHATS:
                api_send_chat_action_as_business(TOKEN, message.business_connection_id, message.chat.id, "typing")
                time.sleep(1.5)
                reply_text = data.get("auto_reply_text", {}).get(owner_user_id, "مرحباً! أنا مشغول حالياً، سأقوم بالرد عليك في أقرب وقت ممكن. ✉️")
                api_send_message_as_business(TOKEN, message.business_connection_id, message.chat.id, reply_text)
                REPLIED_CHATS.add(chat_key)
                logger.info(f"Sent auto-reply to chat {message.chat.id}")

    # Chat commands via reply
    if message.text and message.reply_to_message:
        if str(message.from_user.id) == owner_user_id:
            cmd_text = message.text.strip().lower()
            cmd_parts = message.text.strip().split(None, 1)
            cmd_word = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
            target_msg_id = message.reply_to_message.message_id
            target_chat_id = message.chat.id
            conn_id = message.business_connection_id
            cmds = get_user_commands(data, owner_user_id)
            delete_cmd = cmds.get("delete", "!delete").strip().lower()
            pin_cmd = cmds.get("pin", "!pin").strip().lower()
            unpin_cmd = cmds.get("unpin", "!unpin").strip().lower()
            id_cmd = cmds.get("id", "!id").strip().lower()
            note_cmd = cmds.get("note", "!note").strip().lower()
            help_cmd = cmds.get("help", "!help").strip().lower()

            if cmd_text == delete_cmd:
                api_delete_business_messages(TOKEN, conn_id, [target_msg_id, message.message_id])
            elif cmd_text == pin_cmd:
                api_pin_message_as_business(TOKEN, conn_id, target_chat_id, target_msg_id)
                api_delete_business_messages(TOKEN, conn_id, [message.message_id])
            elif cmd_text == unpin_cmd:
                api_unpin_message_as_business(TOKEN, conn_id, target_chat_id, target_msg_id)
                api_delete_business_messages(TOKEN, conn_id, [message.message_id])
            elif cmd_text == id_cmd:
                target_user = message.reply_to_message.from_user
                target_id = target_user.id
                target_name = target_user.first_name or ""
                if target_user.last_name: target_name += f" {target_user.last_name}"
                target_username = f"@{target_user.username}" if target_user.username else "لا يوجد"
                api_edit_business_message(TOKEN, conn_id, target_chat_id, message.message_id,
                    f"🆔 <b>معلومات المستخدم:</b>\n\n👤 الاسم: <b>{escape_html(target_name)}</b>\n🔗 اليوزر: <b>{escape_html(target_username)}</b>\n🪪 الآيدي: <code>{target_id}</code>", parse_mode="HTML")
            elif cmd_word == note_cmd:
                replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""
                if not cmd_arg:
                    body_text = replied_text if replied_text else "[وسائط]"
                    title_text = body_text[:25] + "..." if len(body_text) > 25 else body_text
                else:
                    title_text = cmd_arg
                    body_text = replied_text if replied_text else "[وسائط]"
                if "notes" not in data: data["notes"] = {}
                if owner_user_id not in data["notes"]: data["notes"][owner_user_id] = []
                note_id = len(data["notes"][owner_user_id]) + 1
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                data["notes"][owner_user_id].append({"id": note_id, "title": title_text, "text": body_text, "date": now_str})
                save_data(data)
                api_edit_business_message(TOKEN, conn_id, target_chat_id, message.message_id,
                    f"✅ <b>تم حفظ الملاحظة بنجاح:</b>\n\n📌 <b>العنوان:</b> {escape_html(title_text)}\n📅 <b>التاريخ:</b> {now_str}", parse_mode="HTML")
            elif cmd_text == help_cmd:
                api_edit_business_message(TOKEN, conn_id, target_chat_id, message.message_id,
                    "❓ <b>الأوامر المتاحة في هذا الشات:</b>\n\n"
                    f"🗑️ <code>{escape_html(cmds['delete'])}</code> — حذف الرسالة\n"
                    f"📌 <code>{escape_html(cmds['pin'])}</code> — تثبيت الرسالة\n"
                    f"🔓 <code>{escape_html(cmds['unpin'])}</code> — إلغاء التثبيت\n"
                    f"🆔 <code>{escape_html(cmds['id'])}</code> — عرض الآيدي\n"
                    f"📝 <code>{escape_html(cmds['note'])} [عنوان]</code> — حفظ ملاحظة\n"
                    f"❓ <code>{escape_html(cmds['help'])}</code> — عرض المساعدة", parse_mode="HTML")

    elif message.text and not message.reply_to_message:
        if str(message.from_user.id) == owner_user_id:
            cmd_text = message.text.strip().lower()
            cmd_parts = message.text.strip().split(None, 1)
            cmd_word = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
            conn_id = message.business_connection_id
            cmds = get_user_commands(data, owner_user_id)
            id_cmd = cmds.get("id", "!id").strip().lower()
            note_cmd = cmds.get("note", "!note").strip().lower()
            help_cmd = cmds.get("help", "!help").strip().lower()

            if cmd_text == id_cmd:
                owner_name = message.from_user.first_name or ""
                if message.from_user.last_name: owner_name += f" {message.from_user.last_name}"
                owner_username = f"@{message.from_user.username}" if message.from_user.username else "لا يوجد"
                api_edit_business_message(TOKEN, conn_id, message.chat.id, message.message_id,
                    f"🆔 <b>معلوماتك الشخصية:</b>\n\n👤 الاسم: <b>{escape_html(owner_name)}</b>\n🔗 اليوزر: <b>{escape_html(owner_username)}</b>\n🪪 الآيدي: <code>{owner_user_id}</code>", parse_mode="HTML")
            elif cmd_word == note_cmd:
                if not cmd_arg:
                    api_edit_business_message(TOKEN, conn_id, message.chat.id, message.message_id, "⚠️ <b>يرجى كتابة نص الملاحظة بجانب الأمر لحفظها.</b>", parse_mode="HTML")
                    return
                body_text = cmd_arg
                title_text = body_text[:25] + "..." if len(body_text) > 25 else body_text
                if "notes" not in data: data["notes"] = {}
                if owner_user_id not in data["notes"]: data["notes"][owner_user_id] = []
                note_id = len(data["notes"][owner_user_id]) + 1
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                data["notes"][owner_user_id].append({"id": note_id, "title": title_text, "text": body_text, "date": now_str})
                save_data(data)
                api_edit_business_message(TOKEN, conn_id, message.chat.id, message.message_id,
                    f"✅ <b>تم حفظ الملاحظة بنجاح:</b>\n\n📌 <b>العنوان:</b> {escape_html(title_text)}\n📅 <b>التاريخ:</b> {now_str}", parse_mode="HTML")
            elif cmd_text == help_cmd:
                api_edit_business_message(TOKEN, conn_id, message.chat.id, message.message_id,
                    "❓ <b>الأوامر المتاحة في شات البيزنس:</b>\n\n"
                    f"🗑️ <code>{escape_html(cmds.get('delete','!delete'))}</code> — حذف الرسالة (مع رد)\n"
                    f"📌 <code>{escape_html(cmds.get('pin','!pin'))}</code> — تثبيت الرسالة (مع رد)\n"
                    f"🔓 <code>{escape_html(cmds.get('unpin','!unpin'))}</code> — إلغاء التثبيت (مع رد)\n"
                    f"🆔 <code>{escape_html(cmds.get('id','!id'))}</code> — عرض الآيدي\n"
                    f"📝 <code>{escape_html(cmds.get('note','!note'))} [نص]</code> — حفظ ملاحظة\n"
                    f"❓ <code>{escape_html(cmds.get('help','!help'))}</code> — هذه المساعدة", parse_mode="HTML")

# ==========================================
# Business Message Handlers
# ==========================================
# IMPORTANT: We register handlers WITHOUT content_types filter so that ALL message types
# are captured - including photos with spoiler, paid_media, and any future Telegram types.
# The default content_types=['text'] would silently drop non-text messages!

def handle_business_message_wrapper(message):
    try:
        # LOG FIRST - before any condition checks - to track every message that reaches the handler
        msg_ct = getattr(message, 'content_type', 'unknown')
        msg_bid = getattr(message, 'business_connection_id', 'N/A')
        logger.info(f"[BUSINESS MSG RECEIVED] msg_id={message.message_id} | chat={message.chat.id} | content_type={msg_ct} | business_conn={msg_bid}")
        process_business_message(message, is_edit=False)
    except Exception as e:
        logger.error(f"[CRITICAL ERROR] Exception in business message handler for msg_id={getattr(message, 'message_id', '?')}: {e}", exc_info=True)

def handle_edited_business_message_wrapper(message):
    try:
        msg_ct = getattr(message, 'content_type', 'unknown')
        logger.info(f"[EDITED BUSINESS MSG RECEIVED] msg_id={message.message_id} | chat={message.chat.id} | content_type={msg_ct}")
        process_business_message(message, is_edit=True)
    except Exception as e:
        logger.error(f"[CRITICAL ERROR] Exception in edited business message handler for msg_id={getattr(message, 'message_id', '?')}: {e}", exc_info=True)

# Register handlers directly without content_type filtering (catches ALL types)
_new_msg_handler = bot._build_handler_dict(handle_business_message_wrapper, func=lambda message: True)
# Remove content_types filter entirely so no message type is ever rejected
_new_msg_handler['filters'].pop('content_types', None)
bot.add_business_message_handler(_new_msg_handler)

_edit_msg_handler = bot._build_handler_dict(handle_edited_business_message_wrapper, func=lambda message: True)
_edit_msg_handler['filters'].pop('content_types', None)
bot.add_edited_business_message_handler(_edit_msg_handler)

# ==========================================
# RAW JSON PRE-CACHING + PENDING DELETIONS QUEUE
# ==========================================
# Multi-layer approach:
# 1. Pre-cache business_messages from raw JSON BEFORE parsing
# 2. Track all received message IDs for diagnostic
# 3. Pending deletions queue: when a deleted_business_messages arrives for
#    an unarchived message, queue it for background retry (up to 30s)
# 4. Extreme logging for debugging missing messages

import types as _types

# Track all business_message IDs we've ever seen (for diagnostic)
_seen_business_msg_ids = {}  # {f"{chat_id}_{msg_id}": timestamp}

# Pending deletions queue: messages that were deleted but not yet in cache
_pending_deletions = {}  # {f"{chat_id}_{msg_id}": {"timestamp": float, "retry_count": int, "max_retries": int, "data": dict}}
_pending_deletions_lock = threading.Lock()

# Store notification data for inline button callbacks
_notification_data = {}  # {notif_id: {text, caption, media_type, file_id, local_path, sender_name, chat_id, msg_id, send_date, delete_time, is_sender_customer, customer_name, old_text, new_text, type: 'delete'|'edit'}}
_notification_data_lock = threading.Lock()
_notif_counter = 0

# Store old text/caption for edited messages (keyed by "chat_id_msg_id")
# This is populated by pre-cache BEFORE the cache is overwritten with the new text
_edit_old_data = {}  # {f"{chat_id}_{msg_id}": {"old_text": str, "old_caption": str}}
_edit_old_data_lock = threading.Lock()

# ==========================================
# Compact Notification Helper Functions
# ==========================================

def _get_time_ago(timestamp=None):
    """Return a human-readable Arabic time-ago string."""
    if timestamp is None:
        return "الآن"
    diff = time.time() - timestamp
    if diff < 5:
        return "الآن"
    elif diff < 60:
        return f"قبل {int(diff)} ثانية"
    elif diff < 3600:
        return f"قبل {int(diff // 60)} دقيقة"
    elif diff < 86400:
        return f"قبل {int(diff // 3600)} ساعة"
    else:
        return f"قبل {int(diff // 86400)} يوم"

def _get_media_emoji_label(media_type):
    """Return (emoji, arabic_label) for a media type."""
    mapping = {
        'photo': ('📷', 'صورة'),
        'video': ('🎬', 'فيديو'),
        'document': ('📄', 'ملف'),
        'voice': ('🎤', 'رسالة صوتية'),
        'audio': ('🎵', 'صوت'),
        'sticker': ('🏷️', 'ملصق'),
        'animation': ('🎞️', 'متحركة'),
        'video_note': ('📹', 'ملاحظة فيديو'),
        'photo_spoiler': ('📷', 'صورة مخفية'),
        'paid_photo': ('📷', 'صورة مدفوعة'),
        'paid_video': ('🎬', 'فيديو مدفوع'),
        'paid_media': ('📦', 'وسائط مدفوعة'),
    }
    return mapping.get(media_type, ('📦', media_type or 'نص'))

def _store_notification_data(ndata):
    """Store notification data and return a unique notif_id."""
    global _notif_counter
    with _notification_data_lock:
        _notif_counter += 1
        nid = _notif_counter
        _notification_data[nid] = ndata
        # Keep only last 500 notification data entries to avoid memory bloat
        if len(_notification_data) > 500:
            keys_to_remove = sorted(_notification_data.keys())[:len(_notification_data) - 500]
            for k in keys_to_remove:
                _notification_data.pop(k, None)
    return nid

def send_compact_delete_notification(owner_user_id, customer_name, chat_id, msg_id,
                                      text_content, caption_content, file_id, media_type,
                                      local_path, sender_name, sender_id, owner_user_id_str,
                                      send_date_str, delete_time_str, is_sender_customer):
    """Send a compact delete notification with inline buttons."""
    # Determine media info line
    has_media = bool(file_id and media_type)
    if has_media:
        media_emoji, media_label = _get_media_emoji_label(media_type)
        media_info = f"{media_emoji} {media_label}"
    else:
        media_emoji, media_label = '💬', 'نص'
        media_info = f"{media_emoji} {media_label}"

    # Time ago
    time_ago = _get_time_ago()

    # Build compact text
    notif_text = (
        f"🗑️ حذف رسالة — {escape_html(customer_name)}\n"
        f"{media_info} | {time_ago}"
    )

    # Store data for callbacks
    notif_id = _store_notification_data({
        'type': 'delete',
        'text': text_content,
        'caption': caption_content,
        'media_type': media_type,
        'file_id': file_id,
        'local_path': local_path,
        'sender_name': sender_name,
        'chat_id': chat_id,
        'msg_id': msg_id,
        'send_date': send_date_str,
        'delete_time': delete_time_str,
        'is_sender_customer': is_sender_customer,
        'customer_name': customer_name,
        'owner_user_id': owner_user_id,
    })

    # Build inline keyboard
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_view = types.InlineKeyboardButton("👁️ عرض المحتوى", callback_data=f"view_del_{notif_id}")
    buttons = [btn_view]
    if has_media:
        btn_dl = types.InlineKeyboardButton("📥 تحميل الوسائط", callback_data=f"dl_media_{notif_id}")
        buttons.append(btn_dl)
    markup.add(*buttons)

    try:
        safe_bot_call(bot.send_message, owner_user_id, notif_text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.error(f"Failed to send compact delete notification: {e}")

def send_compact_edit_notification(owner_user_id, customer_name, chat_id, msg_id,
                                    old_text, old_caption, new_text, sender_name, edit_time_str,
                                    file_id=None, media_type=None, local_path=None, send_date_str=None):
    """Send a compact edit notification with inline buttons showing both old and new text."""
    # Determine media info line
    has_media = bool(file_id and media_type)
    if has_media:
        media_emoji, media_label = _get_media_emoji_label(media_type)
        media_info = f"{media_emoji} {media_label}"
    else:
        media_emoji, media_label = '💬', 'نص'
        media_info = f"{media_emoji} {media_label}"

    # Time ago
    time_ago = _get_time_ago()

    # Build compact notification text
    notif_text = (
        f"✏️ تعديل رسالة — {escape_html(customer_name)}\n"
        f"{media_info} | {time_ago}"
    )

    # Store data for callbacks
    notif_id = _store_notification_data({
        'type': 'edit',
        'old_text': old_text,
        'old_caption': old_caption,
        'new_text': new_text,
        'media_type': media_type,
        'file_id': file_id,
        'local_path': local_path,
        'sender_name': sender_name,
        'chat_id': chat_id,
        'msg_id': msg_id,
        'send_date': send_date_str,
        'edit_time': edit_time_str,
        'customer_name': customer_name,
        'owner_user_id': owner_user_id,
    })

    # Build inline keyboard
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_view = types.InlineKeyboardButton("👁️ عرض التفاصيل", callback_data=f"view_edit_{notif_id}")
    buttons = [btn_view]
    if has_media:
        btn_dl = types.InlineKeyboardButton("📥 تحميل الوسائط", callback_data=f"dl_edit_{notif_id}")
        buttons.append(btn_dl)
    markup.add(*buttons)

    try:
        safe_bot_call(bot.send_message, owner_user_id, notif_text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.error(f"Failed to send compact edit notification: {e}")

# _export_chat_deletion_txt removed - full chat deletion export feature no longer needed

# Raw update log file for debugging
RAW_UPDATES_LOG = os.path.join(DATA_DIR, 'raw_updates.log')

def _log_raw_update(update_json):
    """Log every raw update for diagnostic purposes."""
    try:
        update_id = update_json.get('update_id', '?')
        update_types = [k for k in update_json.keys() if k != 'update_id']
        log_line = f"{datetime.datetime.now().isoformat()} | update_id={update_id} | types={update_types}"
        
        # For business_message, log the msg_id and chat_id
        for ut in ['business_message', 'edited_business_message']:
            if ut in update_json and isinstance(update_json[ut], dict):
                bm = update_json[ut]
                msg_id = bm.get('message_id', '?')
                chat_id = bm.get('chat', {}).get('id', '?')
                has_media = any(k in bm for k in ['photo', 'video', 'document', 'voice', 'audio', 'sticker', 'animation', 'video_note', 'paid_media'])
                spoiler = bm.get('has_media_spoiler')
                log_line += f" | {ut}: msg_id={msg_id} chat={chat_id} media={has_media} spoiler={spoiler}"
        
        # For deleted_business_messages, log the message_ids
        if 'deleted_business_messages' in update_json and isinstance(update_json['deleted_business_messages'], dict):
            dbm = update_json['deleted_business_messages']
            msg_ids = dbm.get('message_ids', [])
            chat_id = dbm.get('chat', {}).get('id', '?') if isinstance(dbm.get('chat'), dict) else '?'
            log_line += f" | deleted_business_messages: chat={chat_id} msg_ids={msg_ids}"
        
        with open(RAW_UPDATES_LOG, 'a', encoding='utf-8') as f:
            f.write(log_line + '\n')
    except Exception:
        pass

def _precache_business_message_from_raw_json(bm_raw, is_edit=False):
    """Extract key data from raw business_message JSON and cache it immediately.
    When is_edit=True, preserves the OLD text/caption BEFORE overwriting the cache,
    so that edit notifications can show both the before and after text."""
    try:
        if not isinstance(bm_raw, dict):
            return

        msg_id = bm_raw.get('message_id')
        if not msg_id:
            return

        chat_raw = bm_raw.get('chat', {})
        chat_id = chat_raw.get('id')
        if not chat_id:
            return

        # Track that we've seen this message
        cache_key = f"{chat_id}_{msg_id}"
        _seen_business_msg_ids[cache_key] = time.time()

        # When this is an edited message, save the OLD text/caption BEFORE overwriting
        old_text = None
        old_caption = None
        if is_edit:
            old_cached = get_cached_message(chat_id, msg_id)
            if old_cached:
                old_text = old_cached.get('text') or ''
                old_caption = old_cached.get('caption') or ''
                # Store in global dict so process_business_message can retrieve it
                with _edit_old_data_lock:
                    _edit_old_data[cache_key] = {"old_text": old_text, "old_caption": old_caption}
                logger.info(f"[PRE-CACHE EDIT] Preserved old text for msg_id={msg_id}: '{old_text[:50]}...' old_caption='{old_caption[:50]}...'")

        from_raw = bm_raw.get('from', {}) or {}
        sender_id = from_raw.get('id')
        sender_name = (from_raw.get('first_name', '') or '')
        if from_raw.get('last_name'):
            sender_name += ' ' + from_raw['last_name']
        sender_name = sender_name.strip() or 'غير معروف'

        text = bm_raw.get('text', '') or ''
        caption = bm_raw.get('caption', '') or ''

        file_id = None
        media_type = None

        # paid_media
        if 'paid_media' in bm_raw and isinstance(bm_raw['paid_media'], dict):
            paid_info = bm_raw['paid_media']
            paid_list = paid_info.get('paid_media', [])
            if paid_list and isinstance(paid_list, list) and len(paid_list) > 0:
                first_media = paid_list[0]
                if 'photo' in first_media and isinstance(first_media['photo'], list):
                    file_id = first_media['photo'][-1].get('file_id')
                    media_type = 'paid_photo'
                elif 'video' in first_media and isinstance(first_media['video'], dict):
                    file_id = first_media['video'].get('file_id')
                    media_type = 'paid_video'
                else:
                    media_type = 'paid_media'

        if not file_id and not media_type:
            if 'photo' in bm_raw and isinstance(bm_raw['photo'], list) and len(bm_raw['photo']) > 0:
                file_id = bm_raw['photo'][-1].get('file_id')
                media_type = 'photo'
                if bm_raw.get('has_media_spoiler'):
                    media_type = 'photo_spoiler'
            elif 'video' in bm_raw and isinstance(bm_raw['video'], dict):
                file_id = bm_raw['video'].get('file_id')
                media_type = 'video'
            elif 'document' in bm_raw and isinstance(bm_raw['document'], dict):
                file_id = bm_raw['document'].get('file_id')
                media_type = 'document'
            elif 'voice' in bm_raw and isinstance(bm_raw['voice'], dict):
                file_id = bm_raw['voice'].get('file_id')
                media_type = 'voice'
            elif 'audio' in bm_raw and isinstance(bm_raw['audio'], dict):
                file_id = bm_raw['audio'].get('file_id')
                media_type = 'audio'
            elif 'sticker' in bm_raw and isinstance(bm_raw['sticker'], dict):
                file_id = bm_raw['sticker'].get('file_id')
                media_type = 'sticker'
            elif 'animation' in bm_raw and isinstance(bm_raw['animation'], dict):
                file_id = bm_raw['animation'].get('file_id')
                media_type = 'animation'
            elif 'video_note' in bm_raw and isinstance(bm_raw['video_note'], dict):
                file_id = bm_raw['video_note'].get('file_id')
                media_type = 'video_note'
            elif 'contact' in bm_raw:
                media_type = 'contact'
            elif 'location' in bm_raw:
                media_type = 'location'
            elif 'venue' in bm_raw:
                media_type = 'venue'
            elif 'poll' in bm_raw:
                media_type = 'poll'
            elif 'dice' in bm_raw:
                media_type = 'dice'
            elif 'game' in bm_raw:
                media_type = 'game'
            elif 'story' in bm_raw:
                media_type = 'story'

        date_ts = bm_raw.get('date')
        send_date_str = ''
        if date_ts:
            try:
                send_date_str = datetime.datetime.fromtimestamp(date_ts, tz=datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            except Exception:
                send_date_str = str(date_ts)

        # Check for spoiler indicator
        has_spoiler = bm_raw.get('has_media_spoiler')
        
        extra_info = ""
        if has_spoiler:
            extra_info = " | SPOILER"

        cache_message(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            sender_id=sender_id,
            sender_name=sender_name,
            file_id=file_id,
            media_type=media_type,
            caption=caption,
            local_path=None,
            send_date=send_date_str
        )
        label = "EDITED" if is_edit else "NEW"
        logger.info(f"[PRE-CACHE] {label} msg_id={msg_id} | chat={chat_id} | media_type={media_type} | text={bool(text)} | file_id={bool(file_id)}{extra_info}")

        # If this message was in the pending deletions queue, process it now
        with _pending_deletions_lock:
            if cache_key in _pending_deletions:
                logger.info(f"[PRE-CACHE] Message {cache_key} was in pending deletions! Will be processed by background checker.")

    except Exception as e:
        logger.error(f"[PRE-CACHE] Failed to pre-cache business_message: {e}", exc_info=True)


# Monkey-patch bot.get_updates to intercept raw JSON and pre-cache business messages
_original_bot_get_updates = bot.get_updates.__func__

def _precaching_get_updates(self_bot, **kwargs):
    """Monkey-patched get_updates that pre-caches business_messages from raw JSON
    BEFORE pyTelegramBotAPI parses them. This ensures no message is ever missed."""
    from telebot import apihelper as _apihelper

    # Step 1: Get raw JSON from Telegram API directly
    try:
        json_updates = _apihelper.get_updates(
            self_bot.token,
            offset=kwargs.get('offset'),
            limit=kwargs.get('limit'),
            timeout=kwargs.get('timeout', 20),
            allowed_updates=kwargs.get('allowed_updates'),
            long_polling_timeout=kwargs.get('long_polling_timeout', 20)
        )
    except Exception as e:
        logger.error(f"[PRE-CACHE] get_updates API call failed: {e}")
        raise

    if not json_updates:
        return []

    # Step 2: Log every raw update for diagnostic
    for ju in json_updates:
        _log_raw_update(ju)

    # Step 3: Pre-cache EVERY business_message from raw JSON
    bm_count = 0
    for ju in json_updates:
        try:
            if 'business_message' in ju and isinstance(ju.get('business_message'), dict):
                _precache_business_message_from_raw_json(ju['business_message'], is_edit=False)
                bm_count += 1
            if 'edited_business_message' in ju and isinstance(ju.get('edited_business_message'), dict):
                _precache_business_message_from_raw_json(ju['edited_business_message'], is_edit=True)
                bm_count += 1
        except Exception as e:
            logger.error(f"[PRE-CACHE] Error pre-caching from raw update: {e}")

    if bm_count > 0:
        logger.info(f"[PRE-CACHE] Batch: {len(json_updates)} updates, {bm_count} business_messages pre-cached from raw JSON")

    # Step 4: Parse updates using pyTelegramBotAPI (original flow)
    parsed_updates = []
    for ju in json_updates:
        try:
            parsed = telebot.types.Update.de_json(ju)
            if parsed is not None:
                parsed_updates.append(parsed)
            else:
                logger.warning(f"[PRE-CACHE] Update de_json returned None. Keys: {list(ju.keys())}")
        except Exception as e:
            logger.error(f"[PRE-CACHE] Update de_json failed: {e}. Keys: {list(ju.keys())}")

    return parsed_updates

bot.get_updates = _types.MethodType(_precaching_get_updates, bot)
logger.info("[PRE-CACHE] Monkey-patched bot.get_updates with raw JSON pre-caching + diagnostic logging")


# ==========================================
# Background Pending Deletions Checker
# ==========================================
def _background_pending_deletions_checker():
    """Background thread that checks pending deletions every 2 seconds.
    If a business_message arrives for a pending deletion, processes it."""
    while not SHUTDOWN_REQUESTED:
        try:
            with _pending_deletions_lock:
                items_to_check = list(_pending_deletions.items())
            
            for cache_key, info in items_to_check:
                chat_id = info['chat_id']
                msg_id = info['msg_id']
                elapsed = time.time() - info['timestamp']
                
                # Check if the message is now in cache
                cached_msg = get_cached_message(chat_id, msg_id)
                if cached_msg:
                    logger.info(f"[Pending-Del] Message {cache_key} found in cache after {elapsed:.1f}s! Processing deletion...")
                    with _pending_deletions_lock:
                        _pending_deletions.pop(cache_key, None)
                    # Process the deletion with the cached message
                    _process_cached_deletion(cached_msg, info)
                    continue
                
                # If we've waited too long, give up
                if elapsed > 30:
                    logger.warning(f"[Pending-Del] Message {cache_key} still not in cache after {elapsed:.1f}s. Giving up - sending unarchived notification.")
                    with _pending_deletions_lock:
                        _pending_deletions.pop(cache_key, None)
                    _send_unarchived_notification(info)
                    continue
                
                # Check if we ever saw this message ID in business_message updates
                if cache_key in _seen_business_msg_ids:
                    logger.info(f"[Pending-Del] Message {cache_key} was seen in business_message at {_seen_business_msg_ids[cache_key]}, but not in cache. Waiting...")
            
            time.sleep(2)
        except Exception as e:
            logger.error(f"[Pending-Del] Background checker error: {e}")
            time.sleep(5)

def _process_cached_deletion(cached_msg, deletion_info):
    """Process a deletion that was pending but the message has now been cached."""
    try:
        owner_user_id = deletion_info['owner_user_id']
        customer_name = deletion_info['customer_name']
        customer_username = deletion_info['customer_username']
        owner_name = deletion_info.get('owner_name', 'صاحب الحساب')
        
        sender_id = cached_msg.get("sender_id")
        sender_name = cached_msg.get("sender_name") or "غير معروف"
        text_content = cached_msg.get("text") or ""
        caption_content = cached_msg.get("caption") or ""
        file_id = cached_msg.get("file_id")
        media_type = cached_msg.get("media_type")
        local_path = cached_msg.get("local_path")
        send_date_str = cached_msg.get("send_date") or ""
        
        chat_id = deletion_info['chat_id']
        msg_id = deletion_info['msg_id']
        remove_cached_message(chat_id, msg_id)
        
        is_sender_customer = (str(sender_id) != str(owner_user_id))
        delete_time_str = deletion_info.get('delete_time_str', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # Send compact delete notification with inline buttons
        send_compact_delete_notification(
            owner_user_id=owner_user_id,
            customer_name=customer_name,
            chat_id=chat_id,
            msg_id=msg_id,
            text_content=text_content,
            caption_content=caption_content,
            file_id=file_id,
            media_type=media_type,
            local_path=local_path,
            sender_name=sender_name,
            sender_id=sender_id,
            owner_user_id_str=str(owner_user_id),
            send_date_str=send_date_str,
            delete_time_str=delete_time_str,
            is_sender_customer=is_sender_customer
        )
                
        logger.info(f"[Pending-Del] Successfully processed delayed deletion for {chat_id}_{msg_id}")
    except Exception as e:
        logger.error(f"[Pending-Del] Error processing cached deletion: {e}", exc_info=True)

def _send_unarchived_notification(info):
    """Send compact unarchived deletion notification."""
    try:
        owner_user_id = info['owner_user_id']
        customer_name = info['customer_name']
        chat_id = info['chat_id']
        msg_id = info['msg_id']
        
        # Send compact notification
        notif_text = (
            f"🗑️ حذف رسالة — {escape_html(customer_name)}\n"
            f"⚠️ غير مؤرشفة"
        )
        try:
            safe_bot_call(bot.send_message, owner_user_id, notif_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send unarchived delete notification: {e}")
    except Exception as e:
        logger.error(f"Error in _send_unarchived_notification: {e}")

# Start background pending deletions checker
_pending_del_thread = threading.Thread(target=_background_pending_deletions_checker, daemon=True)
_pending_del_thread.start()
logger.info("[Pending-Del] Background pending deletions checker started")

# ==========================================
# Deleted Business Messages Handler
# ==========================================

@bot.deleted_business_messages_handler()
def handle_deleted_business_messages(deleted_updates):
    data = load_data()
    owner_user_id = None
    for uid, conn_id in data["connections"].items():
        if conn_id == deleted_updates.business_connection_id:
            owner_user_id = uid
            break

    if not owner_user_id:
        return

    notify_deletions = data.get("notify_deletions", {}).get(owner_user_id, True)
    if not notify_deletions:
        logger.info(f"Deletion notification disabled for owner {owner_user_id}")
        return

    chat = deleted_updates.chat
    customer_name = chat.first_name or ""
    if chat.last_name: customer_name += f" {chat.last_name}"
    customer_name = customer_name.strip() or "العميل"
    customer_username = f"@{chat.username}" if chat.username else "لا يوجد"

    owner_details = data.get("connection_details", {}).get(owner_user_id, {})
    owner_name = owner_details.get("first_name", "")
    if owner_details.get("last_name"): owner_name += f" {owner_details.get('last_name')}"
    owner_name = owner_name.strip() or "صاحب الحساب"

    # ---- Full Chat Deletion Detection ----
    if len(deleted_updates.message_ids) > 5:
        # Treat as full chat deletion - just send a simple notification
        logger.info(f"[Full-Chat-Delete] Detected bulk deletion of {len(deleted_updates.message_ids)} messages from chat {chat.id}.")
        notif_text = (
            f"🗑️ حذف محادثة كاملة — {escape_html(customer_name)}\n"
            f"📝 {len(deleted_updates.message_ids)} رسالة"
        )
        try:
            safe_bot_call(bot.send_message, owner_user_id, notif_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send full chat delete notification: {e}")
        # Remove all cached messages for this chat
        remove_all_cached_messages_for_chat(chat.id)
        return

    # ---- Individual Message Deletion Handling ----
    for msg_id in deleted_updates.message_ids:
        cache_key = f"{chat.id}_{msg_id}"
        cached_msg = get_cached_message(chat.id, msg_id)

        # Multi-retry: if message not found, try multiple times with increasing delays
        if not cached_msg:
            logger.info(f"[Deletion] Message {msg_id} not in cache. Checking if business_message was ever received...")
            
            # Check if we ever saw this message in a business_message update
            was_ever_seen = cache_key in _seen_business_msg_ids
            if was_ever_seen:
                logger.info(f"[Deletion] Message {msg_id} was seen in PRE-CACHE but not in DB. Retrying with delays...")
            else:
                logger.warning(f"[Deletion] Message {msg_id} was NEVER received as business_message. Telegram didn't send it.")
            
            # Try multiple retries with increasing delays
            for retry_delay in [2, 5, 10]:
                time.sleep(retry_delay)
                cached_msg = get_cached_message(chat.id, msg_id)
                if cached_msg:
                    logger.info(f"[Deletion] Message {msg_id} found in cache after {retry_delay}s retry!")
                    break
                logger.info(f"[Deletion] Message {msg_id} still not in cache after {retry_delay}s wait.")

        if cached_msg:
            sender_id = cached_msg.get("sender_id")
            sender_name = cached_msg.get("sender_name") or "غير معروف"
            text_content = cached_msg.get("text") or ""
            caption_content = cached_msg.get("caption") or ""
            file_id = cached_msg.get("file_id")
            media_type = cached_msg.get("media_type")
            local_path = cached_msg.get("local_path")
            send_date_str = cached_msg.get("send_date") or ""

            remove_cached_message(chat.id, msg_id)

            is_sender_customer = (str(sender_id) != str(owner_user_id))
            delete_time_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Send compact delete notification with inline buttons
            send_compact_delete_notification(
                owner_user_id=owner_user_id,
                customer_name=customer_name,
                chat_id=chat.id,
                msg_id=msg_id,
                text_content=text_content,
                caption_content=caption_content,
                file_id=file_id,
                media_type=media_type,
                local_path=local_path,
                sender_name=sender_name,
                sender_id=sender_id,
                owner_user_id_str=str(owner_user_id),
                send_date_str=send_date_str,
                delete_time_str=delete_time_str,
                is_sender_customer=is_sender_customer
            )
        else:
            # Message not in cache after all retries
            # Add to pending deletions queue for background checker (up to 30s more)
            delete_time_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            with _pending_deletions_lock:
                _pending_deletions[cache_key] = {
                    'timestamp': time.time(),
                    'chat_id': chat.id,
                    'msg_id': msg_id,
                    'owner_user_id': owner_user_id,
                    'customer_name': customer_name,
                    'customer_username': customer_username,
                    'owner_name': owner_name,
                    'delete_time_str': delete_time_str
                }
            
            logger.info(f"[Deletion] Message {msg_id} added to pending deletions queue. Background checker will retry for up to 30s.")
            
            # Send compact pending/unarchived notification
            was_ever_seen = cache_key in _seen_business_msg_ids
            if was_ever_seen:
                pending_text = (
                    f"⏳ حذف رسالة — {escape_html(customer_name)}\n"
                    f"💬 جاري الاسترجاع..."
                )
            else:
                pending_text = (
                    f"🗑️ حذف رسالة — {escape_html(customer_name)}\n"
                    f"⚠️ غير مؤرشفة"
                )
            try:
                safe_bot_call(bot.send_message, owner_user_id, pending_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send pending delete notification: {e}")

# ==========================================
# Main Entry Point - Long Polling
# ==========================================

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Telegram Business Bot - Fly.io Edition")
    logger.info("Starting with Long Polling (no webhook needed)...")
    logger.info(f"Data directory: {DATA_DIR}")
    logger.info(f"Owner ID: {OWNER_ID}")
    logger.info("=" * 50)

    try:
        bot.remove_webhook()
        logger.info("Webhook removed successfully - switching to long polling")
    except Exception as e:
        logger.warning(f"Could not remove webhook: {e}")

    # Use infinity_polling with monkey-patched get_updates that pre-caches
    # ALL business messages from raw JSON before pyTelegramBotAPI parsing
    while not SHUTDOWN_REQUESTED:
        try:
            logger.info("Starting infinity polling with raw JSON pre-caching...")
            ALL_UPDATE_TYPES = [
                'message', 'edited_message', 'channel_post', 'edited_channel_post',
                'inline_query', 'chosen_inline_result', 'callback_query',
                'shipping_query', 'pre_checkout_query', 'poll', 'poll_answer',
                'my_chat_member', 'chat_member', 'chat_join_request',
                'message_reaction', 'message_reaction_count',
                'chat_boost', 'removed_chat_boost',
                'business_connection', 'business_message',
                'edited_business_message', 'deleted_business_messages',
                'purchased_paid_media'
            ]
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=False,
                allowed_updates=ALL_UPDATE_TYPES
            )
        except Exception as e:
            logger.error(f"Polling crashed with error: {e}")
            if not SHUTDOWN_REQUESTED:
                logger.info("Restarting polling in 5 seconds...")
                time.sleep(5)
            else:
                break

    logger.info("Bot shutdown complete.")