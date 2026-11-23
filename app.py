import os
import json
import time
import sqlite3
import logging
import threading
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, jsonify
from bale import Bot, Message, InlineKeyboardMarkup, InlineKeyboardButton

# =========================================================
# تنظیمات اصلی
# =========================================================
BALE_TOKEN = os.getenv("BALE_TOKEN", "").strip()
ADMIN_ID = "696006270"
DB_PATH = os.getenv("DB_PATH", "glass_button_bot.db")
DAILY_FREE_LIMIT = 12
AD_BOT_URL = "https://ble.ir/GlassButtonBot"
SUPPORT_URL = "https://ble.ir/imsina51"
BALE_API = "https://tapi.bale.ai/bot"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("glass-button-maker")

app = Flask(__name__)
bot = None
bot_thread_started = False

# =========================================================
# پایگاه داده
# =========================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                required INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS states (
                user_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS creations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                key TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        count = con.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        if count == 0:
            defaults = [
                ("@olito_studio", "کانال اول ⚡️", 1),
                ("@gold_pakat", "کانال دوم 🧑‍💻", 0),
                ("@sarbase_vatan", "کانال سوم ⚜️", 0),
                ("@joke_abad", "کانال چهارم 🕳", 0),
            ]
            now = datetime.now(timezone.utc).isoformat()
            con.executemany(
                "INSERT INTO channels(username,label,required,created_at) VALUES(?,?,?,?)",
                [(u, label, req, now) for u, label, req in defaults]
            )


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_key():
    # سهمیه روزانه بر اساس تاریخ UTC محاسبه می‌شود.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def add_user(user_id):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id,first_seen) VALUES(?,?)",
            (str(user_id), now_iso())
        )


def get_state(user_id):
    with db() as con:
        row = con.execute(
            "SELECT state,data FROM states WHERE user_id=?",
            (str(user_id),)
        ).fetchone()
    if not row:
        return None, {}
    try:
        data = json.loads(row["data"] or "{}")
    except Exception:
        data = {}
    return row["state"], data


def set_state(user_id, state, data=None):
    payload = json.dumps(data or {}, ensure_ascii=False)
    with db() as con:
        con.execute("""
            INSERT INTO states(user_id,state,data) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data
        """, (str(user_id), state, payload))


def clear_state(user_id):
    with db() as con:
        con.execute("DELETE FROM states WHERE user_id=?", (str(user_id),))


def get_channels(required=None):
    with db() as con:
        if required is None:
            rows = con.execute("SELECT * FROM channels ORDER BY id").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM channels WHERE required=? ORDER BY id",
                (int(required),)
            ).fetchall()
    return rows


def daily_count(user_id):
    with db() as con:
        return con.execute(
            "SELECT COUNT(*) FROM daily_usage WHERE user_id=? AND day=?",
            (str(user_id), today_key())
        ).fetchone()[0]


def total_count(user_id):
    with db() as con:
        return con.execute(
            "SELECT COUNT(*) FROM creations WHERE user_id=?",
            (str(user_id),)
        ).fetchone()[0]


def add_creation(user_id, title, action):
    with db() as con:
        con.execute(
            "INSERT INTO daily_usage(user_id,day,created_at) VALUES(?,?,?)",
            (str(user_id), today_key(), now_iso())
        )
        con.execute(
            "INSERT INTO creations(user_id,title,action,created_at) VALUES(?,?,?,?)",
            (str(user_id), title, action, now_iso())
        )


# =========================================================
# ابزارهای پیام و دکمه
# =========================================================
def ikb(rows):
    """Build an inline keyboard while preserving the requested row layout."""
    markup = InlineKeyboardMarkup()
    for row in rows:
        if not row:
            continue
        # add() receives all buttons in one row at once; calling it once per
        # button can cause buttons intended for separate rows to run together.
        markup.add(*row)
    return markup


def cb(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def url_button(text, url):
    return InlineKeyboardButton(text=text, url=url)


def user_id_of(obj):
    user = (
        getattr(obj, "author", None)
        or getattr(obj, "from_user", None)
        or getattr(obj, "user", None)
    )
    if user is None:
        return None
    value = getattr(user, "user_id", None)
    if value is None:
        value = getattr(user, "id", None)
    return str(value) if value is not None else None


def chat_id_of(message):
    chat = getattr(message, "chat", None)
    if chat is None:
        return None
    value = getattr(chat, "chat_id", None)
    if value is None:
        value = getattr(chat, "id", None)
    return value


def message_text(message):
    return (
        getattr(message, "content", None)
        or getattr(message, "text", None)
        or ""
    ).strip()


async def send(chat_id, text, markup=None):
    await bot.send_message(chat_id, text, components=markup)


async def answer_callback(callback_id, text="", show_alert=False):
    """پاسخ سریع به کلیک دکمه؛ برای اعلان بالای صفحه بله."""
    if not callback_id:
        return
    payload = {
        "callback_query_id": str(callback_id),
        "text": str(text)[:200],
        "show_alert": bool(show_alert),
    }
    request = urllib.request.Request(
        f"{BALE_API}{BALE_TOKEN}/answerCallbackQuery",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", exc)


def main_keyboard():
    return ikb([
        [cb("ساخت دکمه شیشه‌ای جدید 🕺", "create")],
        [cb("حساب کاربری 🧸", "account")],
        [cb("خرید عضویت اجباری/اختیاری ⚜️", "buy")],
        [cb("پشتیبانی 📞", "support")],
    ])


def home_text():
    return (
        "سلام 😍\n"
        "به ربات ساخت دکمه شیشه ای خوش اومدی 💙👋\n\n"
        "روزانه ۱۲ بار میتونی دکمه شیشه ای بدون تبلیغات ربات بسازی🤩 "
        "ولی اگر بار سیزدهم بشه ، یه دکمه تبلیغی هم برات اضافه میشه 😭 "
        "ولی میتونی تا بینهایت بار با تبلیغ بسازی 🤑\n\n"
        "در ضمن ، اشتراک پرو و ... نداریم و برای همه کاربران همینجوریه😊"
    )


# =========================================================
# بررسی عضویت کانال‌های اجباری
# =========================================================
async def is_member_of(channel_username, user_id):
    try:
        member = await bot.get_chat_member(channel_username, user_id)
        if member is None:
            return False
        if bool(getattr(member, "is_member", False)):
            return True
        status = str(getattr(member, "status", "")).lower()
        return status in ("member", "administrator", "creator", "owner")
    except Exception as exc:
        log.warning("Membership check failed for %s: %s", channel_username, exc)
        return False


async def missing_required_channels(user_id):
    missing = []
    for channel in get_channels(required=1):
        if not await is_member_of(channel["username"], user_id):
            missing.append(channel)
    return missing


async def show_gate(chat_id):
    channels = get_channels()
    rows = []
    for channel in channels:
        kind = "اجباری" if channel["required"] else "اختیاری"
        label = f"{channel['label']} ({kind})"
        username = channel["username"].lstrip("@")
        rows.append([url_button(label, f"https://ble.ir/{username}")])
    rows.append([cb("عضو شدم ✍️✅", "check_join")])
    await send(
        chat_id,
        "🌹 برای استفاده از این ربات باید در کانال‌های زیر عضو شوید:\n\n"
        "کانال‌های اختیاری بررسی نمی‌شوند و عضویت در آن‌ها الزامی نیست.",
        ikb(rows)
    )


async def access_or_gate(chat_id, user_id):
    missing = await missing_required_channels(user_id)
    if missing:
        await show_gate(chat_id)
        return False
    return True


# =========================================================
# پنل مدیریت
# =========================================================
def panel_keyboard():
    return ikb([
        [cb("📋 مشاهده کانال‌ها", "adm_channels")],
        [cb("➕ افزودن کانال", "adm_add")],
        [cb("🗑 حذف کانال", "adm_remove")],
        [cb("🔁 تغییر اجباری/اختیاری", "adm_toggle")],
        [cb("📊 آمار ربات", "adm_stats")],
    ])


async def show_panel(chat_id):
    await send(chat_id, "🛠 پنل مدیریت دکمه شیشه‌ای ساز\n\nیکی از بخش‌ها را انتخاب کن:", panel_keyboard())


async def show_channels(chat_id):
    channels = get_channels()
    if not channels:
        await send(chat_id, "هنوز کانالی ثبت نشده است.", panel_keyboard())
        return
    lines = ["📋 فهرست کانال‌ها:\n"]
    for i, ch in enumerate(channels, 1):
        kind = "🔴 اجباری" if ch["required"] else "⚪ اختیاری"
        lines.append(f"{i}. {ch['label']} — {ch['username']} — {kind}")
    await send(chat_id, "\n".join(lines), ikb([[cb("↩️ بازگشت به پنل", "adm_home")]]))


async def show_remove_channels(chat_id):
    channels = get_channels()
    rows = []
    for ch in channels:
        rows.append([cb(f"❌ {ch['username']}", f"adm_del:{ch['id']}")])
    rows.append([cb("↩️ بازگشت", "adm_home")])
    await send(chat_id, "برای حذف، کانال موردنظر را انتخاب کن:", ikb(rows))


async def show_toggle_channels(chat_id):
    channels = get_channels()
    rows = []
    for ch in channels:
        kind = "اجباری" if ch["required"] else "اختیاری"
        rows.append([cb(f"{ch['username']} | {kind}", f"adm_toggle:{ch['id']}")])
    rows.append([cb("↩️ بازگشت", "adm_home")])
    await send(chat_id, "برای تغییر نوع عضویت، کانال را انتخاب کن:", ikb(rows))


async def show_stats(chat_id):
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        creations = con.execute("SELECT COUNT(*) FROM creations").fetchone()[0]
        today = con.execute(
            "SELECT COUNT(*) FROM daily_usage WHERE day=?",
            (today_key(),)
        ).fetchone()[0]
    await send(
        chat_id,
        f"📊 آمار ربات\n\n👥 کاربران: {users}\n"
        f"🔘 کل دکمه‌های ساخته‌شده: {creations}\n"
        f"📅 ساخت امروز: {today}",
        ikb([[cb("↩️ بازگشت به پنل", "adm_home")]])
    )


# =========================================================
# ساخت دکمه
# =========================================================
async def show_action_choices(chat_id):
    await send(
        chat_id,
        "آفرین 👏\nخوب پیش رفتیم 👍\n"
        "حالا وقتشه انتخاب کنی وقتی کاربر اونو زد چه اتفاقی بیفته؟🧐",
        ikb([
            [cb("یک لینک باز بشه 😀", "act_link")],
            [cb("یک پیام بالای صفحه بیاد 😆", "act_alert")],
            [cb("هیچی نشه 😂", "act_none")],
            [cb("❌ لغو ساخت", "cancel_create")],
        ])
    )


async def show_preview(chat_id, draft):
    action = draft.get("action")
    if action == "link":
        action_text = f"باز کردن لینک:\n{draft.get('url')}"
    elif action == "alert":
        action_text = f"نمایش پیام بالای صفحه:\n{draft.get('alert_text')}"
    else:
        action_text = "بدون عملکرد"
    text = (
        "🔘 پیش‌نمایش دکمه شما\n\n"
        f"عنوان: {draft.get('title')}\n"
        f"عملکرد: {action_text}\n\n"
        "آیا اطلاعات بالا مورد تأیید شماست؟ 🤔"
    )
    await send(
        chat_id,
        text,
        ikb([
            [cb("✅ تأیید و ساخت", "confirm_create")],
            [cb("❌ لغو ساخت", "cancel_create")],
        ])
    )


def valid_http_url(value):
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def finish_creation(chat_id, user_id, draft):
    used = daily_count(user_id)
    key = secrets.token_hex(5)

    if draft["action"] == "link":
        main_button = url_button(draft["title"], draft["url"])
    elif draft["action"] == "alert":
        with db() as con:
            con.execute(
                "INSERT OR REPLACE INTO alerts(key,text,created_at) VALUES(?,?,?)",
                (key, draft["alert_text"], now_iso())
            )
        main_button = cb(draft["title"], f"alert:{key}")
    else:
        main_button = cb(draft["title"], f"noop:{key}")

    rows = [[main_button]]
    if used >= DAILY_FREE_LIMIT:
        rows.append([url_button("ساخت دکمه شیشه ای رایگان🤩", AD_BOT_URL)])

    add_creation(user_id, draft["title"], draft["action"])
    clear_state(user_id)

    if used < DAILY_FREE_LIMIT:
        note = f"🎉 دکمه‌ات ساخته شد!\nساخت بدون تبلیغ امروز: {used + 1} از ۱۲"
    else:
        note = "🎉 دکمه‌ات ساخته شد!\nاین خروجی شامل دکمه تبلیغاتی است."

    await send(chat_id, note, ikb(rows))
    await send(chat_id, "برای ساخت دکمه بعدی از منوی اصلی استفاده کن:", main_keyboard())


# =========================================================
# رویداد پیام‌ها
# =========================================================
if BALE_TOKEN:
    bot = Bot(token=BALE_TOKEN)

    @bot.event
    async def on_before_ready():
        try:
            await bot.delete_webhook()
        except Exception as exc:
            log.info("delete_webhook: %s", exc)

    @bot.event
    async def on_ready():
        log.info("Glass Button Maker bot is ready.")

    @bot.event
    async def on_message(message: Message):
        user_id = user_id_of(message)
        chat_id = chat_id_of(message)
        text = message_text(message)

        if not user_id or chat_id is None:
            return

        add_user(user_id)

        if text.startswith("/start"):
            clear_state(user_id)
            if await access_or_gate(chat_id, user_id):
                await send(chat_id, home_text(), main_keyboard())
            return

        if text.startswith("/panel"):
            if user_id == ADMIN_ID:
                clear_state(user_id)
                await show_panel(chat_id)
            else:
                await send(chat_id, "⛔ دسترسی به پنل مدیریت برای شما فعال نیست.")
            return

        if text in ("/cancel", "لغو"):
            clear_state(user_id)
            await send(chat_id, "فرایند لغو شد. 😊", main_keyboard())
            return

        state, data = get_state(user_id)
        if not state:
            if await access_or_gate(chat_id, user_id):
                await send(chat_id, "از دکمه‌های منوی اصلی استفاده کن 😊", main_keyboard())
            return

        # ورودی‌های مربوط به ساخت دکمه
        if state == "title":
            if not text:
                await send(chat_id, "عنوان نمی‌تواند خالی باشد. دوباره بفرست 😌")
                return
            if len(text) > 20:
                await send(chat_id, f"عنوانت {len(text)} کاراکتره؛ حداکثر ۲۰ کاراکتر مجازه 😌 دوباره بفرست.")
                return
            data["title"] = text
            set_state(user_id, "choose_action", data)
            await show_action_choices(chat_id)
            return

        if state == "link":
            if not valid_http_url(text):
                await send(chat_id, "این لینک معتبر نیست 😕\nلطفاً لینک کامل با https:// یا http:// بفرست.")
                return
            data["action"] = "link"
            data["url"] = text
            set_state(user_id, "preview", data)
            await show_preview(chat_id, data)
            return

        if state == "alert_text":
            if not text:
                await send(chat_id, "متن نمی‌تواند خالی باشد. دوباره بفرست 🫡")
                return
            if len(text) > 200:
                await send(chat_id, "متن اعلان حداکثر ۲۰۰ کاراکتر می‌تواند باشد. کوتاه‌ترش کن و دوباره بفرست.")
                return
            data["action"] = "alert"
            data["alert_text"] = text
            set_state(user_id, "preview", data)
            await show_preview(chat_id, data)
            return

        if state == "admin_add_channel":
            username = text.strip()
            if username.startswith("https://ble.ir/"):
                username = "@" + username.rsplit("/", 1)[-1].lstrip("@")
            elif not username.startswith("@"):
                username = "@" + username
            if len(username) < 3 or " " in username or "/" in username:
                await send(chat_id, "شناسه کانال درست نیست. مثل @channel_username بفرست.")
                return
            data["username"] = username
            set_state(user_id, "admin_add_type", data)
            await send(
                chat_id,
                "نوع عضویت این کانال را انتخاب کن:",
                ikb([
                    [cb("🔴 اجباری", "adm_add_required")],
                    [cb("⚪ اختیاری", "adm_add_optional")],
                    [cb("لغو", "adm_home")],
                ])
            )
            return


    @bot.event
    async def on_callback(callback):
        callback_id = getattr(callback, "callback_id", None)
        data = str(getattr(callback, "data", "") or "")
        user_obj = getattr(callback, "from_user", None) or getattr(callback, "user", None)
        user_id = None
        if user_obj is not None:
            user_id = str(getattr(user_obj, "user_id", None) or getattr(user_obj, "id", ""))
        message = getattr(callback, "message", None)
        chat_id = chat_id_of(message) if message else None

        # پاسخ به کلیک، تا دکمه در حالت انتظار نماند.
        if data.startswith("alert:"):
            key = data.split(":", 1)[1]
            with db() as con:
                row = con.execute("SELECT text FROM alerts WHERE key=?", (key,)).fetchone()
            await answer_callback(callback_id, row["text"] if row else "این پیام در دسترس نیست.")
            return
        if data.startswith("noop:"):
            await answer_callback(callback_id, "")
            return

        await answer_callback(callback_id, "")
        if not user_id or chat_id is None:
            return
        add_user(user_id)

        # پنل مدیریت
        if data.startswith("adm_") or data.startswith("adm:"):
            if user_id != ADMIN_ID:
                return
            if data == "adm_home":
                clear_state(user_id)
                await show_panel(chat_id)
                return
            if data == "adm_channels":
                await show_channels(chat_id)
                return
            if data == "adm_add":
                set_state(user_id, "admin_add_channel", {})
                await send(chat_id, "شناسه کانال را بفرست؛ مثلاً @my_channel")
                return
            if data == "adm_remove":
                await show_remove_channels(chat_id)
                return
            if data == "adm_toggle":
                await show_toggle_channels(chat_id)
                return
            if data == "adm_stats":
                await show_stats(chat_id)
                return
            if data in ("adm_add_required", "adm_add_optional"):
                state, pending = get_state(user_id)
                if state != "admin_add_type" or not pending.get("username"):
                    await show_panel(chat_id)
                    return
                required = 1 if data == "adm_add_required" else 0
                username = pending["username"]
                try:
                    with db() as con:
                        con.execute(
                            "INSERT INTO channels(username,label,required,created_at) VALUES(?,?,?,?)",
                            (username, username, required, now_iso())
                        )
                    clear_state(user_id)
                    await send(chat_id, f"✅ کانال {username} اضافه شد.", panel_keyboard())
                except sqlite3.IntegrityError:
                    clear_state(user_id)
                    await send(chat_id, "این کانال از قبل ثبت شده است.", panel_keyboard())
                return
            if data.startswith("adm_del:"):
                channel_id = data.split(":", 1)[1]
                with db() as con:
                    con.execute("DELETE FROM channels WHERE id=?", (channel_id,))
                await send(chat_id, "✅ کانال حذف شد.", panel_keyboard())
                return
            if data.startswith("adm_toggle:"):
                channel_id = data.split(":", 1)[1]
                with db() as con:
                    row = con.execute("SELECT required FROM channels WHERE id=?", (channel_id,)).fetchone()
                    if row:
                        new_value = 0 if row["required"] else 1
                        con.execute("UPDATE channels SET required=? WHERE id=?", (new_value, channel_id))
                await send(chat_id, "✅ نوع عضویت کانال تغییر کرد.", panel_keyboard())
                return

        # دسترسی کاربران عادی؛ عضویت کانال‌های اختیاری هیچ‌وقت بررسی نمی‌شود.
        if data == "check_join":
            missing = await missing_required_channels(user_id)
            if missing:
                names = "\n".join(ch["username"] for ch in missing)
                await send(chat_id, f"هنوز در این کانال‌های اجباری عضو نشدی:\n{names}\n\nبعد از عضویت دوباره بزن «عضو شدم».")
            else:
                await send(chat_id, "🎉 عضویتت تأیید شد! خوش اومدی 💙", main_keyboard())
            return

        if not await access_or_gate(chat_id, user_id):
            return

        if data == "create":
            set_state(user_id, "title", {})
            await send(
                chat_id,
                "خب رسیدیم به بخش جذاب ساخت دکمه😁\n"
                "بهم بگو می‌خوای عنوان دکمه ات چی باشه؟🤔\n"
                "حداکثر 20 کاراکتر ( space و , و ‌ هم کاراکتر حساب می‌شوند)😌\n"
                "مثلا : روی من کلیک کنید😝"
            )
            return

        if data == "account":
            used = daily_count(user_id)
            remaining = max(0, DAILY_FREE_LIMIT - used)
            await send(
                chat_id,
                f"🧸 حساب کاربری\n\n🆔 شناسه: {user_id}\n"
                f"🔘 کل دکمه‌های ساخته‌شده: {total_count(user_id)}\n"
                f"📅 ساخت امروز: {used}\n"
                f"🎁 باقی‌مانده ساخت بدون تبلیغ: {remaining} از ۱۲",
                ikb([[cb("↩️ بازگشت", "home")]])
            )
            return

        if data == "buy":
            await send(
                chat_id,
                "⚜️ تعرفه عضویت ماهانه\n\n"
                "🎟 عضویت اختیاری: ۲۲٬۷۱۱ ریال\n"
                "😍 عضویت اجباری: ۹۲٬۴۷۹ ریال\n\n"
                "برای هماهنگی خرید فقط با پاکت هدیه به پشتیبانی پیام بده:",
                ikb([[url_button("📩 ارتباط با @imsina51", SUPPORT_URL)],
                     [cb("↩️ بازگشت", "home")]])
            )
            return

        if data == "support":
            await send(
                chat_id,
                "📞 برای پشتیبانی و پیگیری، به @imsina51 پیام بده.",
                ikb([[url_button("ارتباط با پشتیبانی", SUPPORT_URL)],
                     [cb("↩️ بازگشت", "home")]])
            )
            return

        if data == "home":
            clear_state(user_id)
            await send(chat_id, home_text(), main_keyboard())
            return

        if data == "act_link":
            state, draft = get_state(user_id)
            if state != "choose_action":
                return
            set_state(user_id, "link", draft)
            await send(chat_id, "اوکیه 👌\nلینک مقصد رو برام بفرست🫡")
            return

        if data == "act_alert":
            state, draft = get_state(user_id)
            if state != "choose_action":
                return
            set_state(user_id, "alert_text", draft)
            await send(chat_id, "اوکیه👌\nمتن رو برام بفرست 🫡\n\nحداکثر ۲۰۰ کاراکتر.")
            return

        if data == "act_none":
            state, draft = get_state(user_id)
            if state != "choose_action":
                return
            draft["action"] = "none"
            set_state(user_id, "preview", draft)
            await send(chat_id, "باشه😂")
            await show_preview(chat_id, draft)
            return

        if data == "confirm_create":
            state, draft = get_state(user_id)
            if state != "preview" or not draft.get("title"):
                await send(chat_id, "اطلاعات ساخت پیدا نشد. دوباره از منوی اصلی شروع کن.", main_keyboard())
                clear_state(user_id)
                return
            await finish_creation(chat_id, user_id, draft)
            return

        if data == "cancel_create":
            clear_state(user_id)
            await send(chat_id, "ساخت دکمه لغو شد. 😊", main_keyboard())
            return


# =========================================================
# وب‌سرور کوچک برای زنده نگه داشتن Render
# =========================================================
@app.get("/")
def index():
    return jsonify({
        "service": "Glass Button Maker",
        "status": "running" if bot_thread_started else "starting",
        "database": "sqlite"
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "glass-button-maker"}), 200


def run_bot():
    global bot_thread_started
    if not BALE_TOKEN:
        log.error("BALE_TOKEN environment variable is missing.")
        return
    try:
        bot_thread_started = True
        bot.run()
    except Exception:
        bot_thread_started = False
        log.exception("Bot stopped with an error.")


init_db()

if BALE_TOKEN:
    threading.Thread(target=run_bot, daemon=True, name="bale-bot-thread").start()
else:
    log.error("Set BALE_TOKEN in Render Environment before starting the service.")
