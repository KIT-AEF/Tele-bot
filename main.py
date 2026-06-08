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
TOKEN = "8841147509:AAGQJu6MoRQdkAD-wphy7Xkzn5xa7X6XMRg"
OWNER_ID = "7115401970"

DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# تشغيل البوت بدون threading لتقليل الموارد
bot = telebot.TeleBot(TOKEN, threaded=False)

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# حالات المستخدم في الذاكرة
user_states = {}
user_temp_trigger = {}


# ==========================================
# البيانات (JSON)
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
        logger.error(f"save_data: {e}")


# ==========================================
# دوال مساعدة
# ==========================================

def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def safe_request(url, payload=None, timeout=10):
    for delay in [0, 3, 6]:
        try:
            if delay:
                time.sleep(delay)
            r = requests.post(url, json=payload, timeout=timeout)
            return r.json()
        except Exception as e:
            logger.warning(f"safe_request attempt failed: {e}")
    return {"ok": False, "description": "فشل الاتصال"}

def get_user_commands(data, uid):
    cmds = dict(data.get("custom_commands", {}).get(uid, {}))
    for k, v in {"delete": "!delete", "pin": "!pin", "unpin": "!unpin", "id": "!id", "help": "!help"}.items():
        cmds.setdefault(k, v)
    return cmds

def get_conn_id(data, uid):
    return data["connections"].get(uid)


# ==========================================
# Telegram Business API
# ==========================================

def api_delete_msgs(conn_id, msg_ids):
    return safe_request(f"https://api.telegram.org/bot{TOKEN}/deleteBusinessMessages",
                        {"business_connection_id": conn_id, "message_ids": msg_ids})

def api_pin(conn_id, chat_id, msg_id):
    return safe_request(f"https://api.telegram.org/bot{TOKEN}/pinChatMessage",
                        {"business_connection_id": conn_id, "chat_id": chat_id, "message_id": msg_id})

def api_unpin(conn_id, chat_id, msg_id):
    return safe_request(f"https://api.telegram.org/bot{TOKEN}/unpinChatMessage",
                        {"business_connection_id": conn_id, "chat_id": chat_id, "message_id": msg_id})

def api_edit(conn_id, chat_id, msg_id, text):
    return safe_request(f"https://api.telegram.org/bot{TOKEN}/editMessageText",
                        {"business_connection_id": conn_id, "chat_id": chat_id,
                         "message_id": msg_id, "text": text, "parse_mode": "HTML"})

def api_check_connection(conn_id):
    """يتحقق من الربط مباشرة عبر getBusinessConnection"""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/getBusinessConnection",
            json={"business_connection_id": conn_id},
            timeout=8
        )
        res = r.json()
        if res.get("ok") and res.get("result", {}).get("is_enabled"):
            return True
        return False
    except Exception:
        return False


# ==========================================
# لوحة الأزرار — كل زرين جنب بعض
# ==========================================

def kb_main():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("⚙️ تخصيص الأوامر", callback_data="menu_commands"),
        types.InlineKeyboardButton("⚡ الاختصارات",     callback_data="menu_shortcuts"),
        types.InlineKeyboardButton("📋 الأوامر الحالية", callback_data="show_commands"),
        types.InlineKeyboardButton("🔗 حالة الربط",     callback_data="check_connection"),
    )
    return m

def kb_commands():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("✏️ أمر الحذف",          callback_data="set_cmd_delete"),
        types.InlineKeyboardButton("✏️ أمر التثبيت",        callback_data="set_cmd_pin"),
        types.InlineKeyboardButton("✏️ إلغاء التثبيت",      callback_data="set_cmd_unpin"),
        types.InlineKeyboardButton("✏️ أمر الآيدي",         callback_data="set_cmd_id"),
        types.InlineKeyboardButton("✏️ أمر المساعدة",       callback_data="set_cmd_help"),
        types.InlineKeyboardButton("🔙 رجوع",               callback_data="menu_main"),
    )
    return m

def kb_shortcuts():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("➕ إضافة اختصار",   callback_data="add_shortcut"),
        types.InlineKeyboardButton("📋 عرض الاختصارات", callback_data="list_shortcuts"),
        types.InlineKeyboardButton("🗑️ مسح الكل",       callback_data="clear_shortcuts"),
        types.InlineKeyboardButton("🔙 رجوع",           callback_data="menu_main"),
    )
    return m

def kb_shortcuts_list(shortcuts):
    m = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(f"🗑️ {t}", callback_data=f"delsh_{t}") for t in shortcuts]
    # إضافة الأزرار اثنين اثنين
    for i in range(0, len(btns), 2):
        row = btns[i:i+2]
        m.add(*row)
    m.add(
        types.InlineKeyboardButton("➕ إضافة", callback_data="add_shortcut"),
        types.InlineKeyboardButton("🔙 رجوع",  callback_data="menu_shortcuts"),
    )
    return m


# ==========================================
# /start مع تحقق تلقائي من الربط
# ==========================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = str(message.from_user.id)
    if uid != OWNER_ID:
        bot.send_message(message.chat.id, "⛔ غير مصرح.")
        return

    data = load_data()
    conn_id = get_conn_id(data, uid)
    name = escape_html(message.from_user.first_name or "")

    if conn_id:
        # تحقق فعلي من تيليجرام
        wait = bot.send_message(message.chat.id, "🔄 جاري التحقق من الربط...")
        is_active = api_check_connection(conn_id)
        try:
            bot.delete_message(message.chat.id, wait.message_id)
        except Exception:
            pass

        if is_active:
            bot.send_message(
                message.chat.id,
                f"مرحباً {name}! ✅\n\n🔗 <b>الحساب التجاري مرتبط وشغال.</b>\n\nاختر ما تريد:",
                parse_mode="HTML", reply_markup=kb_main()
            )
        else:
            # الربط انتهى — امسحه
            del data["connections"][uid]
            save_data(data)
            bot.send_message(
                message.chat.id,
                f"مرحباً {name}! ⚠️\n\n"
                "<b>الربط بالحساب التجاري انقطع.</b>\n\n"
                "لإعادة الربط:\n"
                "١. إعدادات تيليجرام ← بيزنس ← روبوتات الدردشة\n"
                "٢. احذف البوت وأضفه من جديد\n"
                "٣. امنحه كل الصلاحيات\n\n"
                "ستصلك رسالة تأكيد بعد الربط. ✅",
                parse_mode="HTML"
            )
    else:
        bot.send_message(
            message.chat.id,
            f"مرحباً {name}! 👋\n\n"
            "⚠️ <b>البوت غير مرتبط بعد.</b>\n\n"
            "لبدء الاستخدام:\n"
            "١. إعدادات تيليجرام ← بيزنس ← روبوتات الدردشة\n"
            "٢. ابحث عن البوت وقم بربطه\n"
            "٣. امنحه كل الصلاحيات\n\n"
            "ستصلك رسالة تأكيد بعد الربط. ✅",
            parse_mode="HTML"
        )


# ==========================================
# هاندلر ربط/فك البيزنس
# ==========================================

@bot.business_connection_handler()
def handle_bc(bc):
    uid = str(bc.user.id)
    data = load_data()
    if bc.is_enabled:
        data["connections"][uid] = bc.id
        save_data(data)
        try:
            bot.send_message(int(uid),
                             "🎉 <b>تم الربط بنجاح!</b>\n\nيمكنك الآن استخدام الأوامر والاختصارات:",
                             parse_mode="HTML", reply_markup=kb_main())
        except Exception:
            pass
    else:
        data["connections"].pop(uid, None)
        save_data(data)
        try:
            bot.send_message(int(uid), "⚠️ <b>تم إلغاء الربط.</b>", parse_mode="HTML")
        except Exception:
            pass


# ==========================================
# هاندلر الأزرار
# ==========================================

@bot.callback_query_handler(func=lambda c: True)
def handle_cb(call):
    uid = str(call.from_user.id)
    cid = call.message.chat.id
    mid = call.message.message_id

    if uid != OWNER_ID:
        bot.answer_callback_query(call.id, "⛔")
        return

    data = load_data()
    conn_id = get_conn_id(data, uid)
    bot.answer_callback_query(call.id)

    def edit(text, markup=None):
        try:
            bot.edit_message_text(text, cid, mid, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup)

    def send(text, markup=None):
        bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup)

    # ── رجوع للرئيسية ──
    if call.data == "menu_main":
        edit("🏠 <b>القائمة الرئيسية:</b>", kb_main())

    # ── فحص الربط ──
    elif call.data == "check_connection":
        if not conn_id:
            send("⚠️ <b>لا يوجد ربط محفوظ.</b>", kb_main())
            return
        wait = send("🔄 جاري التحقق...")
        ok = api_check_connection(conn_id)
        try:
            bot.delete_message(cid, wait.message_id)
        except Exception:
            pass
        if ok:
            send("✅ <b>الحساب التجاري مرتبط وشغال.</b>", kb_main())
        else:
            del data["connections"][uid]
            save_data(data)
            send("❌ <b>الربط انقطع، تم المسح.</b>\nأعد الربط من إعدادات تيليجرام.")

    # ── قسم الأوامر ──
    elif call.data == "menu_commands":
        cmds = get_user_commands(data, uid)
        text = (
            "⚙️ <b>الأوامر الحالية:</b>\n\n"
            f"🗑️ حذف:          <code>{escape_html(cmds['delete'])}</code>\n"
            f"📌 تثبيت:        <code>{escape_html(cmds['pin'])}</code>\n"
            f"🔓 إلغاء تثبيت: <code>{escape_html(cmds['unpin'])}</code>\n"
            f"🆔 آيدي:         <code>{escape_html(cmds['id'])}</code>\n"
            f"❓ مساعدة:       <code>{escape_html(cmds['help'])}</code>\n\n"
            "اختر الأمر الذي تريد تغييره:"
        )
        edit(text, kb_commands())

    # ── عرض الأوامر ──
    elif call.data == "show_commands":
        cmds = get_user_commands(data, uid)
        text = (
            "📋 <b>الأوامر في شات البيزنس (رد على رسالة):</b>\n\n"
            f"🗑️ <code>{escape_html(cmds['delete'])}</code> — حذف الرسالة\n"
            f"📌 <code>{escape_html(cmds['pin'])}</code> — تثبيت الرسالة\n"
            f"🔓 <code>{escape_html(cmds['unpin'])}</code> — إلغاء التثبيت\n"
            f"🆔 <code>{escape_html(cmds['id'])}</code> — عرض الآيدي\n"
            f"❓ <code>{escape_html(cmds['help'])}</code> — قائمة المساعدة\n\n"
            "💡 <i>اكتب الأمر رداً على أي رسالة في شات البيزنس.</i>"
        )
        send(text, kb_main())

    # ── تغيير الأوامر ──
    elif call.data.startswith("set_cmd_"):
        key = call.data.replace("set_cmd_", "")
        label = {"delete": "الحذف", "pin": "التثبيت", "unpin": "إلغاء التثبيت",
                 "id": "الآيدي", "help": "المساعدة"}.get(key, key)
        user_states[uid] = f"CMD_{key}"
        send(f"✏️ <b>أرسل الكلمة الجديدة لأمر {label}:</b>")

    # ── قسم الاختصارات ──
    elif call.data == "menu_shortcuts":
        count = len(data.get("shortcuts", {}).get(uid, {}))
        text = (
            f"⚡ <b>اختصارات النصوص:</b>\n\n"
            f"لديك <b>{count}</b> اختصار.\n\n"
            "اكتب رمز الاختصار في شات البيزنس\n"
            "وسيبدّله البوت بالنص الكامل تلقائياً."
        )
        edit(text, kb_shortcuts())

    # ── إضافة اختصار ──
    elif call.data == "add_shortcut":
        user_states[uid] = "SC_TRIGGER"
        send("⌨️ <b>أرسل رمز الاختصار:</b>\n\nمثال: <code>.</code> أو <code>سلام</code>")

    # ── عرض الاختصارات ──
    elif call.data == "list_shortcuts" or call.data.startswith("delsh_"):
        if call.data.startswith("delsh_"):
            trigger = call.data[6:]
            if uid in data.get("shortcuts", {}) and trigger in data["shortcuts"][uid]:
                del data["shortcuts"][uid][trigger]
                save_data(data)

        shortcuts = data.get("shortcuts", {}).get(uid, {})
        if not shortcuts:
            send("📋 <b>لا توجد اختصارات.</b>", kb_shortcuts())
            return

        lines = ["📋 <b>الاختصارات المحفوظة:</b>\n"]
        for t, exp in shortcuts.items():
            preview = exp[:35] + "..." if len(exp) > 35 else exp
            lines.append(f"• <code>{escape_html(t)}</code> ← {escape_html(preview)}")
        send("\n".join(lines), kb_shortcuts_list(shortcuts))

    # ── مسح الاختصارات ──
    elif call.data == "clear_shortcuts":
        data.setdefault("shortcuts", {})[uid] = {}
        save_data(data)
        send("✅ <b>تم مسح جميع الاختصارات.</b>", kb_shortcuts())


# ==========================================
# هاندلر النصوص (حالات الإدخال من البوت مش البيزنس)
# ==========================================

@bot.message_handler(
    content_types=["text"],
    func=lambda m: str(m.from_user.id) == OWNER_ID
                   and not getattr(m, "business_connection_id", None)
                   and user_states.get(str(m.from_user.id)) is not None
)
def handle_input(message):
    uid = str(message.from_user.id)
    state = user_states.pop(uid, None)
    text = message.text.strip()
    data = load_data()

    # تغيير أمر
    if state and state.startswith("CMD_"):
        key = state[4:]
        data.setdefault("custom_commands", {}).setdefault(uid, {})[key] = text
        save_data(data)
        bot.send_message(message.chat.id,
                         f"✅ <b>تم تغيير الأمر إلى:</b> <code>{escape_html(text)}</code>",
                         parse_mode="HTML", reply_markup=kb_main())

    # إضافة اختصار - الرمز
    elif state == "SC_TRIGGER":
        user_temp_trigger[uid] = text
        user_states[uid] = "SC_TEXT"
        bot.send_message(message.chat.id,
                         f"📝 <b>الرمز: <code>{escape_html(text)}</code></b>\n\n"
                         "الآن أرسل النص الكامل الذي يظهر بدله:",
                         parse_mode="HTML")

    # إضافة اختصار - النص الكامل
    elif state == "SC_TEXT":
        trigger = user_temp_trigger.pop(uid, None)
        if not trigger:
            bot.send_message(message.chat.id, "⚠️ حدث خطأ، ابدأ من جديد.", reply_markup=kb_main())
            return
        data.setdefault("shortcuts", {}).setdefault(uid, {})[trigger] = text
        save_data(data)
        bot.send_message(message.chat.id,
                         f"✅ <b>تم حفظ الاختصار!</b>\n\n"
                         f"الرمز: <code>{escape_html(trigger)}</code>\n"
                         f"النص: {escape_html(text)}",
                         parse_mode="HTML", reply_markup=kb_shortcuts())


# ==========================================
# هاندلر رسائل البيزنس (تنفيذ الأوامر والاختصارات)
# ==========================================

@bot.business_message_handler(content_types=["text"])
def handle_biz(message):
    if not message.text:
        return

    data = load_data()
    owner_uid = None
    conn_id = None
    for uid, cid in data["connections"].items():
        if cid == message.business_connection_id:
            owner_uid = uid
            conn_id = cid
            break

    if not owner_uid:
        return

    is_out = str(message.from_user.id) == owner_uid
    if not is_out:
        return  # نتجاهل الرسائل الواردة من العملاء

    raw = message.text.strip()
    low = raw.lower()

    # ── اختصارات النصوص ──
    shortcuts = data.get("shortcuts", {}).get(owner_uid, {})
    if raw in shortcuts:
        api_edit(conn_id, message.chat.id, message.message_id, shortcuts[raw])
        return

    # ── أوامر (مع رد) ──
    cmds = get_user_commands(data, owner_uid)
    reply = message.reply_to_message

    if reply:
        target_mid = reply.message_id
        tchat = message.chat.id

        if low == cmds["delete"].lower():
            api_delete_msgs(conn_id, [target_mid, message.message_id])

        elif low == cmds["pin"].lower():
            api_pin(conn_id, tchat, target_mid)
            api_delete_msgs(conn_id, [message.message_id])

        elif low == cmds["unpin"].lower():
            api_unpin(conn_id, tchat, target_mid)
            api_delete_msgs(conn_id, [message.message_id])

        elif low == cmds["id"].lower():
            u = reply.from_user
            nm = ((u.first_name or "") + " " + (u.last_name or "")).strip()
            un = f"@{u.username}" if u.username else "لا يوجد"
            api_edit(conn_id, tchat, message.message_id,
                     f"🆔 <b>معلومات:</b>\n👤 {escape_html(nm)}\n🔗 {escape_html(un)}\n🪪 <code>{u.id}</code>")

        elif low == cmds["help"].lower():
            api_edit(conn_id, tchat, message.message_id,
                     f"❓ <b>الأوامر (مع الرد):</b>\n"
                     f"🗑️ <code>{escape_html(cmds['delete'])}</code> حذف\n"
                     f"📌 <code>{escape_html(cmds['pin'])}</code> تثبيت\n"
                     f"🔓 <code>{escape_html(cmds['unpin'])}</code> إلغاء\n"
                     f"🆔 <code>{escape_html(cmds['id'])}</code> آيدي\n"
                     f"❓ <code>{escape_html(cmds['help'])}</code> مساعدة")

    else:
        # ── أوامر بدون رد ──
        if low == cmds["id"].lower():
            u = message.from_user
            nm = ((u.first_name or "") + " " + (u.last_name or "")).strip()
            un = f"@{u.username}" if u.username else "لا يوجد"
            api_edit(conn_id, message.chat.id, message.message_id,
                     f"🆔 <b>معلوماتك:</b>\n👤 {escape_html(nm)}\n🔗 {escape_html(un)}\n🪪 <code>{owner_uid}</code>")

        elif low == cmds["help"].lower():
            api_edit(conn_id, message.chat.id, message.message_id,
                     f"❓ <b>الأوامر المتاحة:</b>\n"
                     f"🗑️ <code>{escape_html(cmds['delete'])}</code> حذف (مع رد)\n"
                     f"📌 <code>{escape_html(cmds['pin'])}</code> تثبيت (مع رد)\n"
                     f"🔓 <code>{escape_html(cmds['unpin'])}</code> إلغاء (مع رد)\n"
                     f"🆔 <code>{escape_html(cmds['id'])}</code> آيدي\n"
                     f"❓ <code>{escape_html(cmds['help'])}</code> مساعدة")


# ==========================================
# تشغيل البوت
# ==========================================

if __name__ == "__main__":
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=20,
        skip_pending=True,
        allowed_updates=["message", "callback_query", "business_connection", "business_message"]
    )
