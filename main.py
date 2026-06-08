import os
import json
import logging
import time
import subprocess
import requests
import telebot
from telebot import types
from PIL import Image

# ==========================================
# الإعدادات — عدّلها حسب بياناتك
# ==========================================
TOKEN = "ضع_توكن_البوت_هنا"
OWNER_ID = "ضع_آيدي_الأونر_هنا"

DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)

# ==========================================
# تهيئة البوت
# ==========================================
bot = telebot.TeleBot(TOKEN, threaded=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==========================================
# ثوابت الاستوري
# ==========================================
STORY_WIDTH = 1080
STORY_HEIGHT = 1920

# ==========================================
# حالات المستخدم (في الذاكرة)
# ==========================================
# user_id -> state string
user_states = {}
# user_id -> "blurred" / "black" / "white"
user_story_bg = {}
# user_id -> active period in seconds
user_story_period = {}
# user_id -> last story id (for deletion)
user_last_story_id = {}
# user_id -> business connection_id
user_connections = {}


# ==========================================
# دوال مساعدة
# ==========================================

def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_request(url, method="post", payload=None, files=None, timeout=30):
    """HTTP request مع إعادة المحاولة عند الفشل"""
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
                response = requests.get(url, params=payload, timeout=timeout)
            else:
                if files:
                    response = requests.post(url, data=payload, files=files, timeout=timeout)
                else:
                    response = requests.post(url, json=payload, timeout=timeout)
            return response.json()
        except requests.exceptions.Timeout:
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                return {"ok": False, "description": "انتهت مهلة الاتصال. جرب ملفاً أصغر."}
        except Exception as e:
            if attempt < len(retries):
                time.sleep(retries[attempt])
                attempt += 1
            else:
                return {"ok": False, "description": f"خطأ في الاتصال: {str(e)}"}


def safe_bot_call(func, *args, **kwargs):
    """Bot API call مع إعادة المحاولة"""
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


def make_safe_temp_path(prefix, file_id, extension):
    safe_id = ''.join(ch for ch in str(file_id) if ch.isalnum() or ch in ('_', '-'))[:80] or 'media'
    stamp = int(time.time() * 1000)
    if not extension.startswith('.'):
        extension = f'.{extension}'
    return os.path.join(DATA_DIR, f"{prefix}_{safe_id}_{stamp}{extension}")


# ==========================================
# معالجة الصور والفيديو لنسبة 9:16
# ==========================================

def process_story_photo(input_path, output_path, bg_type="blurred"):
    """تجهيز الصورة بنسبة 9:16 للاستوري"""
    try:
        img = Image.open(input_path)
        img = img.convert("RGBA")
        orig_w, orig_h = img.size

        target_ratio = STORY_WIDTH / STORY_HEIGHT
        current_ratio = orig_w / orig_h

        if abs(current_ratio - target_ratio) < 0.05:
            result = img.resize((STORY_WIDTH, STORY_HEIGHT), Image.LANCZOS)
            result = result.convert("RGB")
            result.save(output_path, "JPEG", quality=95)
            return True

        if bg_type == "black":
            bg = Image.new("RGBA", (STORY_WIDTH, STORY_HEIGHT), (0, 0, 0, 255))
        elif bg_type == "white":
            bg = Image.new("RGBA", (STORY_WIDTH, STORY_HEIGHT), (255, 255, 255, 255))
        else:
            bg = img.resize((STORY_WIDTH, STORY_HEIGHT), Image.BILINEAR)
            from PIL import ImageFilter
            bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
            bg = bg.convert("RGBA")

        scale_w = STORY_WIDTH / orig_w
        scale_h = STORY_HEIGHT / orig_h
        scale = min(scale_w, scale_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)

        if new_w < STORY_WIDTH:
            scale = STORY_WIDTH / orig_w
            new_w = STORY_WIDTH
            new_h = int(orig_h * scale)
        if new_h > STORY_HEIGHT:
            scale = STORY_HEIGHT / orig_h
            new_h = STORY_HEIGHT
            new_w = int(orig_w * scale)

        foreground = img.resize((new_w, new_h), Image.LANCZOS)
        paste_x = (STORY_WIDTH - new_w) // 2
        paste_y = (STORY_HEIGHT - new_h) // 2
        bg.paste(foreground, (paste_x, paste_y), foreground if foreground.mode == 'RGBA' else None)

        result = bg.convert("RGB")
        result.save(output_path, "JPEG", quality=95)
        return True
    except Exception as e:
        logger.error(f"[Story Photo] خطأ: {e}")
        return False


def get_video_metadata(path):
    """إرجاع (العرض، الارتفاع، المدة) للفيديو"""
    try:
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "json", path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=20)
        if probe_result.returncode != 0:
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
        logger.warning(f"[Story Video] تعذر قراءة البيانات: {e}")
        return None, None, None


def process_story_video(input_path, output_path, bg_type="blurred"):
    """تجهيز الفيديو بنسبة 9:16 للاستوري"""
    try:
        if bg_type == "black":
            filter_complex = (
                f"color=c=black:s={STORY_WIDTH}x{STORY_HEIGHT}:d=999[bg];"
                f"[0:v]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=decrease,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto:eof_action=repeat,format=yuv420p[v]"
            )
        elif bg_type == "white":
            filter_complex = (
                f"color=c=white:s={STORY_WIDTH}x{STORY_HEIGHT}:d=999[bg];"
                f"[0:v]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=decrease,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto:eof_action=repeat,format=yuv420p[v]"
            )
        else:
            filter_complex = (
                f"[0:v]split=2[bg][fg];"
                f"[bg]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={STORY_WIDTH}:{STORY_HEIGHT},"
                f"boxblur=luma_radius=20:luma_power=1:chroma_radius=20:chroma_power=1,setsar=1[blurred_bg];"
                f"[fg]scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=decrease,setsar=1[scaled_fg];"
                f"[blurred_bg][scaled_fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[v]"
            )

        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-max_muxing_queue_size", "1024",
            "-aspect", "9:16", "-shortest", output_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            logger.error(f"[Story Video] ffmpeg فشل: {result.stderr[:1200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("[Story Video] ffmpeg انتهت المهلة")
        return False
    except Exception as e:
        logger.error(f"[Story Video] خطأ: {e}")
        return False


# ==========================================
# Telegram API — نشر وحذف الاستوري
# ==========================================

def api_post_story_with_file(token, connection_id, media_type, file_obj,
                              caption=None, video_width=None, video_height=None,
                              video_duration=None, active_period=86400):
    url = f"https://api.telegram.org/bot{token}/postStory"
    attach_name = "story_media"

    if media_type == "photo":
        content = {"type": "photo", "photo": f"attach://{attach_name}"}
        mime_type = "image/jpeg"
        file_name = "photo.jpg"
        timeout_setting = 60
    else:
        content = {"type": "video", "video": f"attach://{attach_name}"}
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
    return safe_request(url, payload=payload, files=files, timeout=timeout_setting)


def api_delete_story(token, connection_id, story_id):
    url = f"https://api.telegram.org/bot{token}/deleteStory"
    payload = {"business_connection_id": connection_id, "story_id": story_id}
    return safe_request(url, payload=payload, timeout=15)


# ==========================================
# لوحة الأزرار
# ==========================================

def get_story_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📸 نشر استوري جديد", callback_data="post_story"),
        types.InlineKeyboardButton("🗑️ حذف آخر استوري", callback_data="delete_story"),
    )
    return markup


def get_bg_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🌫️ خلفية ضبابية", callback_data="story_bg_blurred"),
        types.InlineKeyboardButton("⬛ خلفية سوداء", callback_data="story_bg_black"),
    )
    markup.add(types.InlineKeyboardButton("⬜ خلفية بيضاء", callback_data="story_bg_white"))
    markup.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_story"))
    return markup


def get_duration_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("6 ساعات ⏱️", callback_data="story_dur_21600"),
        types.InlineKeyboardButton("12 ساعة ⏱️", callback_data="story_dur_43200"),
        types.InlineKeyboardButton("24 ساعة ⏱️", callback_data="story_dur_86400"),
        types.InlineKeyboardButton("48 ساعة ⏱️", callback_data="story_dur_172800"),
    )
    markup.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_story"))
    return markup


# ==========================================
# هاندلرز البوت
# ==========================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = str(message.from_user.id)
    if uid != str(OWNER_ID):
        bot.send_message(message.chat.id, "⛔ غير مصرح لك باستخدام هذا البوت.")
        return
    bot.send_message(
        message.chat.id,
        "👋 <b>أهلاً! هذا بوت رفع الاستوري.</b>\n\n"
        "اختر ما تريد:",
        parse_mode="HTML",
        reply_markup=get_story_main_keyboard()
    )


@bot.business_connection_handler()
def handle_business_connection(bc):
    """تسجيل ربط البيزنس اكونت"""
    uid = str(bc.user.id)
    if bc.is_enabled:
        user_connections[uid] = bc.id
        logger.info(f"[Business] تم ربط حساب {uid} - connection_id: {bc.id}")
        try:
            bot.send_message(
                int(uid),
                f"✅ <b>تم ربط حسابك التجاري بنجاح!</b>\n"
                f"يمكنك الآن نشر الاستوري.",
                parse_mode="HTML",
                reply_markup=get_story_main_keyboard()
            )
        except Exception:
            pass
    else:
        user_connections.pop(uid, None)
        logger.info(f"[Business] تم إلغاء ربط حساب {uid}")


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    uid = str(call.from_user.id)
    chat_id = call.message.chat.id

    if uid != str(OWNER_ID):
        safe_bot_call(bot.answer_callback_query, call.id, "⛔ غير مصرح")
        return

    conn_id = user_connections.get(uid)

    # ── نشر استوري: اختر الخلفية ──
    if call.data == "post_story":
        if not conn_id:
            safe_bot_call(bot.answer_callback_query, call.id, "⚠️ لم يتم ربط حساب بيزنس بعد")
            safe_bot_call(bot.send_message, chat_id,
                          "⚠️ <b>لم يتم ربط أي حساب بيزنس بعد.</b>\n"
                          "افتح إعدادات تليجرام ← بيزنس ← ربط البوت أولاً.",
                          parse_mode="HTML")
            return
        safe_bot_call(bot.answer_callback_query, call.id)
        safe_bot_call(bot.send_message, chat_id,
                      "🎨 <b>اختر نوع الخلفية للاستوري:</b>\n\n"
                      "🌫️ <b>ضبابية:</b> خلفية ضبابية من نفس الوسائط\n"
                      "⬛ <b>سوداء:</b> خلفية سوداء بالكامل\n"
                      "⬜ <b>بيضاء:</b> خلفية بيضاء بالكامل",
                      parse_mode="HTML",
                      reply_markup=get_bg_keyboard())

    # ── اختيار الخلفية ──
    elif call.data in ("story_bg_blurred", "story_bg_black", "story_bg_white"):
        bg_map = {"story_bg_blurred": "blurred", "story_bg_black": "black", "story_bg_white": "white"}
        bg_label_map = {"blurred": "ضبابية", "black": "سوداء", "white": "بيضاء"}
        chosen = bg_map[call.data]
        user_story_bg[uid] = chosen
        safe_bot_call(bot.answer_callback_query, call.id, f"✅ خلفية {bg_label_map[chosen]}")
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        safe_bot_call(bot.send_message, chat_id,
                      "⏱️ <b>اختر مدة بقاء الاستوري:</b>\n\n"
                      "⚠️ المدد بخلاف 24 ساعة تتطلب اشتراك تليجرام بريميوم للحساب التجاري.",
                      parse_mode="HTML",
                      reply_markup=get_duration_keyboard())

    # ── اختيار المدة ──
    elif call.data.startswith("story_dur_"):
        duration = int(call.data.split("_")[2])
        user_story_period[uid] = duration
        user_states[uid] = "AWAITING_STORY"
        safe_bot_call(bot.answer_callback_query, call.id, "✅ تم تحديد المدة")
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        bg = user_story_bg.get(uid, "blurred")
        bg_label = {"blurred": "ضبابية", "black": "سوداء", "white": "بيضاء"}.get(bg, bg)
        hours = duration // 3600
        safe_bot_call(bot.send_message, chat_id,
                      f"📸 <b>أرسل صورة أو فيديو الآن.</b>\n\n"
                      f"🎨 الخلفية: <b>{bg_label}</b>\n"
                      f"⏱️ المدة: <b>{hours} ساعة</b>\n\n"
                      "سيجهز البوت الوسائط تلقائياً بنسبة 9:16 ثم ينشره كاستوري.",
                      parse_mode="HTML")

    # ── حذف الاستوري ──
    elif call.data == "delete_story":
        safe_bot_call(bot.answer_callback_query, call.id)
        if not conn_id:
            safe_bot_call(bot.send_message, chat_id,
                          "⚠️ <b>لم يتم ربط حساب بيزنس.</b>", parse_mode="HTML",
                          reply_markup=get_story_main_keyboard())
            return
        last_story_id = user_last_story_id.get(uid)
        if not last_story_id:
            safe_bot_call(bot.send_message, chat_id,
                          "⚠️ <b>لم يتم العثور على استوري تم نشره مؤخراً عبر هذا البوت.</b>",
                          parse_mode="HTML", reply_markup=get_story_main_keyboard())
            return
        msg = safe_bot_call(bot.send_message, chat_id,
                            "⏳ <b>جاري حذف الاستوري...</b>", parse_mode="HTML")
        res = api_delete_story(TOKEN, conn_id, last_story_id)
        try:
            safe_bot_call(bot.delete_message, chat_id, msg.message_id)
        except Exception:
            pass
        if res.get("ok"):
            user_last_story_id.pop(uid, None)
            safe_bot_call(bot.send_message, chat_id,
                          "✅ <b>تم حذف الاستوري بنجاح!</b>", parse_mode="HTML",
                          reply_markup=get_story_main_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id,
                          f"❌ <b>فشل حذف الاستوري.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>",
                          parse_mode="HTML", reply_markup=get_story_main_keyboard())

    # ── إلغاء ──
    elif call.data == "cancel_story":
        user_states.pop(uid, None)
        user_story_bg.pop(uid, None)
        user_story_period.pop(uid, None)
        safe_bot_call(bot.answer_callback_query, call.id, "❌ تم الإلغاء")
        try:
            safe_bot_call(bot.delete_message, chat_id, call.message.message_id)
        except Exception:
            pass
        safe_bot_call(bot.send_message, chat_id, "❌ <b>تم إلغاء العملية.</b>",
                      parse_mode="HTML", reply_markup=get_story_main_keyboard())


@bot.message_handler(
    content_types=["photo", "video", "document"],
    func=lambda m: user_states.get(str(m.from_user.id)) == "AWAITING_STORY"
)
def handle_story_media(message):
    uid = str(message.from_user.id)
    chat_id = message.chat.id
    conn_id = user_connections.get(uid)

    if not conn_id:
        safe_bot_call(bot.send_message, chat_id,
                      "⚠️ <b>لم يتم ربط حساب بيزنس.</b>", parse_mode="HTML")
        user_states.pop(uid, None)
        return

    story_bg = user_story_bg.get(uid, "blurred")
    story_active_period = user_story_period.get(uid, 86400)
    caption = message.caption if message.caption else None

    file_id = None
    media_type = None
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
            safe_bot_call(bot.send_message, chat_id,
                          "⚠️ <b>يرجى إرسال صورة أو مقطع فيديو فقط.</b>", parse_mode="HTML")
            return

    bg_label = {"blurred": "ضبابية", "black": "سوداء", "white": "بيضاء"}.get(story_bg, story_bg)
    processing_msg = safe_bot_call(
        bot.send_message, chat_id,
        f"⏳ <b>جاري تحميل وتجهيز الاستوري بنسبة 9:16 مع خلفية {bg_label}...</b>",
        parse_mode="HTML"
    )

    input_ext = ".mp4" if media_type == "video" else ".jpg"
    output_ext = ".mp4" if media_type == "video" else ".jpg"
    temp_filepath = make_safe_temp_path("temp_story", file_id, input_ext)
    processed_filepath = make_safe_temp_path("processed_story", file_id, output_ext)

    video_width = None
    video_height = None

    try:
        # تحميل الملف من تليجرام
        file_info = safe_bot_call(bot.get_file, file_id)
        downloaded_file = safe_bot_call(bot.download_file, file_info.file_path)
        if not downloaded_file or len(downloaded_file) == 0:
            raise Exception("فشل تحميل محتوى الملف من سيرفرات تليجرام.")
        with open(temp_filepath, 'wb') as f:
            f.write(downloaded_file)

        # معالجة الوسائط
        if media_type == "video":
            if not process_story_video(temp_filepath, processed_filepath, bg_type=story_bg):
                raise Exception("فشل تجهيز الفيديو بنسبة 9:16. تأكد من تثبيت FFmpeg.")
            video_width = STORY_WIDTH
            video_height = STORY_HEIGHT
            _, _, detected_duration = get_video_metadata(processed_filepath)
            if detected_duration:
                video_duration = detected_duration
        else:
            if not process_story_photo(temp_filepath, processed_filepath, bg_type=story_bg):
                raise Exception("فشل تجهيز الصورة بنسبة 9:16.")

        # رفع الاستوري
        with open(processed_filepath, 'rb') as f_obj:
            res = api_post_story_with_file(
                TOKEN, conn_id, media_type, f_obj, caption,
                video_width=video_width, video_height=video_height,
                video_duration=video_duration, active_period=story_active_period
            )

        try:
            safe_bot_call(bot.delete_message, chat_id, processing_msg.message_id)
        except Exception:
            pass

        if res.get("ok"):
            story_id = res.get("result", {}).get("id")
            if story_id:
                user_last_story_id[uid] = story_id
            safe_bot_call(bot.send_message, chat_id,
                          f"✅ <b>تم نشر الاستوري بنجاح بنسبة 9:16 مع خلفية {bg_label}!</b>",
                          parse_mode="HTML", reply_markup=get_story_main_keyboard())
        else:
            safe_bot_call(bot.send_message, chat_id,
                          f"❌ <b>فشل نشر الاستوري.</b>\nالسبب: <code>{escape_html(res.get('description'))}</code>",
                          parse_mode="HTML", reply_markup=get_story_main_keyboard())

    except Exception as e:
        try:
            safe_bot_call(bot.delete_message, chat_id, processing_msg.message_id)
        except Exception:
            pass
        safe_bot_call(bot.send_message, chat_id,
                      f"❌ <b>حدث خطأ:</b>\n<code>{escape_html(str(e))}</code>",
                      parse_mode="HTML", reply_markup=get_story_main_keyboard())
        logger.error(f"Story error: {e}")
    finally:
        for path in (temp_filepath, processed_filepath):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    # تنظيف الحالة
    user_states.pop(uid, None)
    user_story_bg.pop(uid, None)
    user_story_period.pop(uid, None)


# ==========================================
# تشغيل البوت
# ==========================================

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Story Uploader Bot - Started")
    logger.info("=" * 50)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=False,
        allowed_updates=["message", "callback_query", "business_connection"]
    )
