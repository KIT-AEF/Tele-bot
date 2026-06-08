import os
import json
import logging
import time
import requests
import telebot
from telebot import types

# ==========================================
# الإعدادات
# ==========================================
TOKEN = "8841147509:AAGGm0ydQptJCyQ19fOqjl4V2O14bijKRU8"
OWNER_ID = "7115401970"

DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

bot = telebot.TeleBot(TOKEN, threaded=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# حالات المستخدم في الذاكرة
user_states = {}        # uid -> state
user_temp_trigger = {}  # uid -> trigger keyword (لحفظ الاختصار مؤقتاً)


# ==========================================
# البيانات (JSON بسيط)
# ==========================================

def load_data():
    default = {"connections": {}, "custom_commands": {}, "shortcuts": {}}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                for k in default:
                    if k not in d:
                        d[k] = default[k]
                return d
        except Exception:
            pass
    return default

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"save_data error: {e}")


# ==========================================
# دوال مساعدة
# ==========================================

def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def safe_request(url, payload=None, files=None, timeout=15):
    retries = [3, 6, 10]
    attempt = 0
    while True:
        try:
            if attempt > 0 and files:
                for f_val in files.values():
                    if isinstance(f_val, tuple) and len(f_val) >= 2:
                        f_obj = f_val[1]
                        if hasattr(f_obj, 'seek'):
                            try: f_obj.seek(0)
                            except Exception: pass
            if files:
                response = requests.post(url, data=payload, files=files, timeout=timeout)
            else:
                response = requests.post(url, json=payload, timeout=timeout)
            return response.json()
        except Exception as e:
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                return {"ok": False, "description": str(e)}

def safe_bot_call(func, *args, **kwargs):
    retries = [3, 6, 10]
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                raise e

def get_user_commands(data, user_id):
    cmds = data.get("custom_commands", {}).get(user_id, {})
    defaults = {
        "delete": "!delete",
        "pin": "!pin",
        "unpin": "!unpin",
        "id": "!id",
        "help": "!help"
    }
    for k, v in defaults.items():
        if k not in cmds:
            cmds[k] = v
    return cmds


# ==========================================
# Telegram Business API Wrappers
# ==========================================

def api_delete_business_messages(connection_id, message_ids):
    url = f"https://api.telegram.org/bot{TOKEN}/deleteBusinessMessages"
    return safe_request(url, {"business_connection_id": connection_id, "message_ids": message_ids})

def api_pin_message(connection_id, chat_id, message_id):
    url = f"https://api.telegram.org/bot{TOKEN}/pinChatMessage"
    return safe_request(url, {"business_connection_id": connection_id, "chat_id": chat_id, "message_id": message_id})

def api_unpin_message(connection_id, chat_id, message_id):
    url = f"https://api.telegram.org/bot{TOKEN}/unpinChatMessage"
    return safe_request(url, {"business_connection_id": connection_id, "chat_id": chat_id, "message_id": message_id})

def api_edit_message(connection_id, chat_id, message_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    return safe_request(url, {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode
    })

def api_get_business_connection(connection_id):
    url = f"https://api.telegram.org/bot{TOKEN}/getBusinessConnection"
    return safe_request(url, {"business_connection_id": connection_id}, timeout=10)


# ==========================================
# لوحة الأزرار
# ==========================================

def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⚙️ تخصيص الأوامر", callback_data="menu_commands"),
        types.InlineKeyboardButton("⚡ الاختصارات", callback_data="menu_shortcuts"),
    )
    markup.add(
        types.InlineKeyboardButton("📋 عرض الأوامر الحالية", callback_data="show_commands"),
    )
    return markup

def get_commands_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✏️ تغيير أمر الحذف", callback_data="set_cmd_delete"),
        types.InlineKeyboardButton("✏️ تغيير أمر التثبيت", callback_data="set_cmd_pin"),
        types.InlineKeyboardButton("✏️ تغيير أمر إلغاء التثبيت", callback_data="set_cmd_unpin"),
        types.InlineKeyboardButton("✏️ تغيير أمر الآيدي", callback_data="set_cmd_id"),
        types.InlineKeyboardButton("✏️ تغيير أمر المساعدة", callback_data="set_cmd_help"),
        types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
    )
    return markup

def get_shortcuts_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ إضافة اختصار", callback_data="add_shortcut"),
        types.InlineKeyboardButton("📋 عرض الاختصارات", callback_data="list_shortcuts"),
    )
    markup.add(
        types.InlineKeyboardButton("🗑️ مسح الكل", callback_data="clear_shortcuts"),
        types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
    )
    return markup


# ==========================================
# هاندلر /start مع فحص الربط تلقائياً
# ==========================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = str(message.from_user.id)
    if uid != str(OWNER_ID):
        bot.send_message(message.chat.id, "⛔ غير مصرح.")
        return

    data = load_data()
    conn_id = data["connections"].get(uid)

    # ── لو فيه connection_id محفوظ، تحقق منه مع تيليجرام مباشرة ──
    if conn_id:
        waiting_msg = safe_bot_call(bot.send_message, message.chat.id,
                                    "🔄 <b>جاري التحقق من حالة الربط...</b>", parse_mode="HTML")
        res = api_get_business_connection(conn_id)
        try:
            safe_bot_call(bot.delete_message, message.chat.id, waiting_msg.message_id)
        except Exception:
            pass

        if res.get("ok") and res.get("result", {}).get("is_enabled"):
            # الربط صحيح وشغال
            safe_bot_call(bot.send_message, message.chat.id,
                          f"✅ <b>مرحباً {escape_html(message.from_user.first_name)}!</b>\n\n"
                          "🔗 الحساب التجاري مرتبط وشغال.\n\n"
                          "اختر ما تريد:",
                          parse_mode="HTML",
                          reply_markup=get_main_keyboard())
        else:
            # الربط انقطع أو غير صالح - امسحه وأعلم المستخدم
            del data["connections"][uid]
            save_data(data)
            safe_bot_call(bot.send_message, message.chat.id,
                          f"مرحباً {escape_html(message.from_user.first_name)}! 👋\n\n"
                          "⚠️ <b>الربط بالحساب التجاري انقطع أو انتهت صلاحيته.</b>\n\n"
                          "لإعادة الربط:\n"
                          "1️⃣ افتح إعدادات تليجرام ← بيزنس ← روبوتات الدردشة\n"
                          "2️⃣ احذف البوت وأضفه من جديد\n"
                          "3️⃣ امنحه كل الصلاحيات\n\n"
                          "بمجرد الربط ستصلك رسالة تأكيد هنا. ✅",
                          parse_mode="HTML")
    else:
        # لا يوجد ربط أصلاً
        safe_bot_call(bot.send_message, message.chat.id,
                      f"مرحباً {escape_html(message.from_user.first_name)}! 👋\n\n"
                      "⚠️ <b>البوت غير مرتبط بعد بحسابك التجاري.</b>\n\n"
                      "لبدء الاستخدام:\n"
                      "1️⃣ افتح إعدادات تليجرام ← بيزنس ← روبوتات الدردشة\n"
                      "2️⃣ ابحث عن هذا البوت وقم بربطه\n"
                      "3️⃣ امنحه كل الصلاحيات\n\n"
                      "بمجرد الربط ستصلك رسالة تأكيد هنا. ✅",
                      parse_mode="HTML")


# ==========================================
# هاندلر ربط البيزنس اكونت
# ==========================================

@bot.business_connection_handler()
def handle_business_connection(bc):
    uid = str(bc.user.id)
    data = load_data()

    if bc.is_enabled:
        data["connections"][uid] = bc.id
        save_data(data)
        logger.info(f"[Business] ربط حساب {uid} - conn_id: {bc.id}")
        try:
            safe_bot_call(bot.send_message, int(uid),
                          "🎉 <b>تم ربط حسابك التجاري بنجاح!</b>\n\n"
                          "يمكنك الآن إدارة الأوامر والاختصارات:",
                          parse_mode="HTML",
                          reply_markup=get_main_keyboard())
        except Exception as e:
            logger.error(f"Error sending connection confirmation: {e}")
    else:
        data["connections"].pop(uid, None)
        save_data(data)
        logger.info(f"[Business] إلغاء ربط حساب {uid}")
        try:
            safe_bot_call(bot.send_message, int(uid),
                          "⚠️ <b>تم إلغاء ربط البوت بحسابك التجاري.</b>",
                          parse_mode="HTML")
        except Exception:
            pass


# ==========================================
# هاندلر الأزرار
# ==========================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    uid = str(call.from_user.id)
    chat_id = call.message.chat.id

    if uid != str(OWNER_ID):
        safe_bot_call(bot.answer_callback_query, call.id, "⛔ غير مصرح")
        return

    data = load_data()

    # ── القائمة الرئيسية ──
    if call.data == "menu_main":
        safe_bot_call(bot.answer_callback_query, call.id)
        try:
            safe_bot_call(bot.edit_message_text,
                          "🏠 <b>القائمة الرئيسية:</b>", chat_id,
                          call.message.message_id, parse_mode="HTML",
                          reply_markup=get_main_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, "🏠 <b>القائمة الرئيسية:</b>",
                          parse_mode="HTML", reply_markup=get_main_keyboard())

    # ── قسم الأوامر ──
    elif call.data == "menu_commands":
        safe_bot_call(bot.answer_callback_query, call.id)
        cmds = get_user_commands(data, uid)
        text = (
            "⚙️ <b>تخصيص الأوامر:</b>\n\n"
            f"🗑️ حذف: <code>{escape_html(cmds['delete'])}</code>\n"
            f"📌 تثبيت: <code>{escape_html(cmds['pin'])}</code>\n"
            f"🔓 إلغاء التثبيت: <code>{escape_html(cmds['unpin'])}</code>\n"
            f"🆔 آيدي: <code>{escape_html(cmds['id'])}</code>\n"
            f"❓ مساعدة: <code>{escape_html(cmds['help'])}</code>"
        )
        try:
            safe_bot_call(bot.edit_message_text, text, chat_id, call.message.message_id,
                          parse_mode="HTML", reply_markup=get_commands_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, text, parse_mode="HTML",
                          reply_markup=get_commands_keyboard())

    # ── عرض الأوامر الحالية ──
    elif call.data == "show_commands":
        safe_bot_call(bot.answer_callback_query, call.id)
        cmds = get_user_commands(data, uid)
        text = (
            "📋 <b>الأوامر الحالية في شات البيزنس:</b>\n\n"
            f"🗑️ <code>{escape_html(cmds['delete'])}</code> — حذف الرسالة (مع الرد عليها)\n"
            f"📌 <code>{escape_html(cmds['pin'])}</code> — تثبيت الرسالة (مع الرد عليها)\n"
            f"🔓 <code>{escape_html(cmds['unpin'])}</code> — إلغاء التثبيت (مع الرد عليها)\n"
            f"🆔 <code>{escape_html(cmds['id'])}</code> — عرض الآيدي\n"
            f"❓ <code>{escape_html(cmds['help'])}</code> — قائمة المساعدة\n\n"
            "💡 <i>اكتب الأمر في شات البيزنس رداً على أي رسالة لتنفيذه.</i>"
        )
        safe_bot_call(bot.send_message, chat_id, text, parse_mode="HTML",
                      reply_markup=get_main_keyboard())

    # ── تغيير الأوامر ──
    elif call.data in ("set_cmd_delete", "set_cmd_pin", "set_cmd_unpin", "set_cmd_id", "set_cmd_help"):
        cmd_map = {
            "set_cmd_delete": ("delete", "الحذف"),
            "set_cmd_pin":    ("pin",    "التثبيت"),
            "set_cmd_unpin":  ("unpin",  "إلغاء التثبيت"),
            "set_cmd_id":     ("id",     "الآيدي"),
            "set_cmd_help":   ("help",   "المساعدة"),
        }
        key, label = cmd_map[call.data]
        user_states[uid] = f"AWAITING_CMD_{key.upper()}"
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id)
        safe_bot_call(bot.send_message, chat_id,
                      f"✏️ <b>أرسل الكلمة أو الرمز الجديد لأمر {label}:</b>",
                      parse_mode="HTML")

    # ── قسم الاختصارات ──
    elif call.data == "menu_shortcuts":
        safe_bot_call(bot.answer_callback_query, call.id)
        shortcuts = data.get("shortcuts", {}).get(uid, {})
        count = len(shortcuts)
        text = (
            f"⚡ <b>اختصارات النصوص السريعة:</b>\n\n"
            f"لديك حالياً <b>{count}</b> اختصار.\n\n"
            "عند كتابة رمز الاختصار في شات البيزنس، "
            "يُبدّله البوت تلقائياً بالنص الكامل."
        )
        try:
            safe_bot_call(bot.edit_message_text, text, chat_id, call.message.message_id,
                          parse_mode="HTML", reply_markup=get_shortcuts_keyboard())
        except Exception:
            safe_bot_call(bot.send_message, chat_id, text, parse_mode="HTML",
                          reply_markup=get_shortcuts_keyboard())

    # ── إضافة اختصار ──
    elif call.data == "add_shortcut":
        user_states[uid] = "AWAITING_SHORTCUT_TRIGGER"
        safe_bot_call(bot.answer_callback_query, call.id)
        safe_bot_call(bot.send_message, chat_id,
                      "⌨️ <b>أرسل رمز أو كلمة الاختصار الآن:</b>\n\n"
                      "مثال: <code>.</code> أو <code>سلام</code>",
                      parse_mode="HTML")

    # ── عرض الاختصارات وحذفها ──
    elif call.data == "list_shortcuts" or call.data.startswith("delsh_"):
        if call.data.startswith("delsh_"):
            trigger_to_delete = call.data[6:]
            if uid in data.get("shortcuts", {}):
                if trigger_to_delete in data["shortcuts"][uid]:
                    del data["shortcuts"][uid][trigger_to_delete]
                    save_data(data)
                    safe_bot_call(bot.answer_callback_query, call.id,
                                  f"✅ تم حذف: {trigger_to_delete}")
        else:
            safe_bot_call(bot.answer_callback_query, call.id)

        shortcuts = data.get("shortcuts", {}).get(uid, {})
        if not shortcuts:
            safe_bot_call(bot.send_message, chat_id,
                          "📋 <b>لا توجد اختصارات محفوظة.</b>",
                          parse_mode="HTML", reply_markup=get_shortcuts_keyboard())
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        lines = ["📋 <b>الاختصارات المحفوظة:</b>\n"]
        for trigger, expanded in shortcuts.items():
            preview = expanded[:30] + "..." if len(expanded) > 30 else expanded
            lines.append(f"• <code>{escape_html(trigger)}</code> ← {escape_html(preview)}")
            markup.add(types.InlineKeyboardButton(
                f"🗑️ حذف: {trigger}", callback_data=f"delsh_{trigger}"
            ))
        markup.add(types.InlineKeyboardButton("➕ إضافة اختصار", callback_data="add_shortcut"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_shortcuts"))
        safe_bot_call(bot.send_message, chat_id, "\n".join(lines),
                      parse_mode="HTML", reply_markup=markup)

    # ── مسح كل الاختصارات ──
    elif call.data == "clear_shortcuts":
        if "shortcuts" not in data:
            data["shortcuts"] = {}
        data["shortcuts"][uid] = {}
        save_data(data)
        safe_bot_call(bot.answer_callback_query, call.id, "✅ تم المسح")
        safe_bot_call(bot.send_message, chat_id,
                      "✅ <b>تم مسح جميع الاختصارات بنجاح.</b>",
                      parse_mode="HTML", reply_markup=get_shortcuts_keyboard())


# ==========================================
# هاندلر الرسائل النصية (حالات الإدخال)
# ==========================================

@bot.message_handler(
    content_types=["text"],
    func=lambda m: str(m.from_user.id) == str(OWNER_ID)
                   and not m.business_connection_id
                   and user_states.get(str(m.from_user.id)) is not None
)
def handle_state_input(message):
    uid = str(message.from_user.id)
    state = user_states.get(uid)
    text = message.text.strip()
    data = load_data()

    # ── تغيير الأوامر ──
    if state and state.startswith("AWAITING_CMD_"):
        key = state.replace("AWAITING_CMD_", "").lower()
        if "custom_commands" not in data: data["custom_commands"] = {}
        if uid not in data["custom_commands"]: data["custom_commands"][uid] = {}
        data["custom_commands"][uid][key] = text
        save_data(data)
        user_states.pop(uid, None)
        safe_bot_call(bot.send_message, message.chat.id,
                      f"✅ <b>تم تغيير أمر {key} إلى:</b> <code>{escape_html(text)}</code>",
                      parse_mode="HTML", reply_markup=get_main_keyboard())

    # ── إضافة اختصار (المرحلة 1: الرمز) ──
    elif state == "AWAITING_SHORTCUT_TRIGGER":
        user_temp_trigger[uid] = text
        user_states[uid] = "AWAITING_SHORTCUT_TEXT"
        safe_bot_call(bot.send_message, message.chat.id,
                      f"📝 <b>تم حفظ الرمز: <code>{escape_html(text)}</code></b>\n\n"
                      "الآن أرسل النص الكامل الذي يظهر بدله عند الإرسال:",
                      parse_mode="HTML")

    # ── إضافة اختصار (المرحلة 2: النص الكامل) ──
    elif state == "AWAITING_SHORTCUT_TEXT":
        trigger = user_temp_trigger.pop(uid, None)
        if not trigger:
            safe_bot_call(bot.send_message, message.chat.id,
                          "⚠️ <b>حدث خطأ، ابدأ من جديد.</b>",
                          parse_mode="HTML")
            user_states.pop(uid, None)
            return
        if "shortcuts" not in data: data["shortcuts"] = {}
        if uid not in data["shortcuts"]: data["shortcuts"][uid] = {}
        data["shortcuts"][uid][trigger] = text
        save_data(data)
        user_states.pop(uid, None)
        safe_bot_call(bot.send_message, message.chat.id,
                      f"✅ <b>تم حفظ الاختصار بنجاح!</b>\n\n"
                      f"الرمز: <code>{escape_html(trigger)}</code>\n"
                      f"النص: {escape_html(text)}",
                      parse_mode="HTML", reply_markup=get_shortcuts_keyboard())


# ==========================================
# هاندلر رسائل البيزنس (تنفيذ الأوامر والاختصارات)
# ==========================================

@bot.business_message_handler(content_types=["text"])
def handle_business_message(message):
    data = load_data()
    owner_user_id = None

    for uid, conn_id in data["connections"].items():
        if conn_id == message.business_connection_id:
            owner_user_id = uid
            break

    if not owner_user_id:
        return

    is_outgoing = str(message.from_user.id) == owner_user_id

    # ── تنفيذ اختصارات النصوص (رسائل صادرة) ──
    if is_outgoing:
        msg_text = message.text.strip() if message.text else ""
        shortcuts = data.get("shortcuts", {}).get(owner_user_id, {})
        if msg_text in shortcuts:
            expanded = shortcuts[msg_text]
            api_edit_message(message.business_connection_id, message.chat.id,
                             message.message_id, expanded)
            return

    # ── تنفيذ الأوامر (رسائل صادرة مع رد) ──
    if is_outgoing and message.text and message.reply_to_message:
        cmd_text = message.text.strip().lower()
        cmd_parts = message.text.strip().split(None, 1)
        cmd_word = cmd_parts[0].lower()
        conn_id = message.business_connection_id
        cmds = get_user_commands(data, owner_user_id)
        target_msg_id = message.reply_to_message.message_id
        target_chat_id = message.chat.id

        if cmd_text == cmds.get("delete", "!delete").lower():
            api_delete_business_messages(conn_id, [target_msg_id, message.message_id])

        elif cmd_text == cmds.get("pin", "!pin").lower():
            api_pin_message(conn_id, target_chat_id, target_msg_id)
            api_delete_business_messages(conn_id, [message.message_id])

        elif cmd_text == cmds.get("unpin", "!unpin").lower():
            api_unpin_message(conn_id, target_chat_id, target_msg_id)
            api_delete_business_messages(conn_id, [message.message_id])

        elif cmd_text == cmds.get("id", "!id").lower():
            target_user = message.reply_to_message.from_user
            name = (target_user.first_name or "") + (" " + target_user.last_name if target_user.last_name else "")
            username = f"@{target_user.username}" if target_user.username else "لا يوجد"
            api_edit_message(conn_id, target_chat_id, message.message_id,
                             f"🆔 <b>معلومات:</b>\n👤 {escape_html(name.strip())}\n"
                             f"🔗 {escape_html(username)}\n🪪 <code>{target_user.id}</code>")

        elif cmd_text == cmds.get("help", "!help").lower():
            api_edit_message(conn_id, target_chat_id, message.message_id,
                             "❓ <b>الأوامر المتاحة:</b>\n\n"
                             f"🗑️ <code>{escape_html(cmds['delete'])}</code> — حذف الرسالة\n"
                             f"📌 <code>{escape_html(cmds['pin'])}</code> — تثبيت الرسالة\n"
                             f"🔓 <code>{escape_html(cmds['unpin'])}</code> — إلغاء التثبيت\n"
                             f"🆔 <code>{escape_html(cmds['id'])}</code> — عرض الآيدي\n"
                             f"❓ <code>{escape_html(cmds['help'])}</code> — المساعدة")

    # ── أمر الآيدي بدون رد ──
    elif is_outgoing and message.text and not message.reply_to_message:
        cmd_text = message.text.strip().lower()
        conn_id = message.business_connection_id
        cmds = get_user_commands(data, owner_user_id)

        if cmd_text == cmds.get("id", "!id").lower():
            name = (message.from_user.first_name or "") + (" " + message.from_user.last_name if message.from_user.last_name else "")
            username = f"@{message.from_user.username}" if message.from_user.username else "لا يوجد"
            api_edit_message(conn_id, message.chat.id, message.message_id,
                             f"🆔 <b>معلوماتك:</b>\n👤 {escape_html(name.strip())}\n"
                             f"🔗 {escape_html(username)}\n🪪 <code>{owner_user_id}</code>")

        elif cmd_text == cmds.get("help", "!help").lower():
            api_edit_message(conn_id, message.chat.id, message.message_id,
                             "❓ <b>الأوامر (مع الرد):</b>\n\n"
                             f"🗑️ <code>{escape_html(cmds['delete'])}</code> — حذف\n"
                             f"📌 <code>{escape_html(cmds['pin'])}</code> — تثبيت\n"
                             f"🔓 <code>{escape_html(cmds['unpin'])}</code> — إلغاء التثبيت\n"
                             f"🆔 <code>{escape_html(cmds['id'])}</code> — آيدي\n"
                             f"❓ <code>{escape_html(cmds['help'])}</code> — مساعدة")


# ==========================================
# تشغيل البوت
# ==========================================

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Commands & Shortcuts Bot - Started")
    logger.info("=" * 50)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=False,
        allowed_updates=["message", "callback_query", "business_connection", "business_message"]
    )
