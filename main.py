import json
import os
import re
import sqlite3
import csv
import urllib.request
import zipfile
from pathlib import Path

import psycopg
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BOT_BRAND = "EuropeUAConnectBot | Українці в Європі"
POSTAL_DB = Path(__file__).with_name("europe_postal.sqlite3")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

DEFAULT_TOPICS = [
    {"key": "thread_01", "title": "Thread 1"},
    {"key": "thread_02", "title": "Thread 2"},
    {"key": "thread_03", "title": "Thread 3"},
    {"key": "thread_04", "title": "Thread 4"},
    {"key": "thread_05", "title": "Thread 5"},
    {"key": "thread_06", "title": "Thread 6"},
    {"key": "thread_07", "title": "Thread 7"},
    {"key": "thread_08", "title": "Thread 8"},
    {"key": "thread_09", "title": "Thread 9"},
    {"key": "thread_10", "title": "Thread 10"},
]


def load_topics():
    raw = os.getenv("TOPICS_JSON", "").strip()
    if not raw:
        return DEFAULT_TOPICS
    try:
        items = json.loads(raw)
        result = []
        for index, item in enumerate(items):
            title = str(item.get("title", "")).strip()
            key = str(item.get("key", "")).strip() or f"topic_{index + 1}"
            if title:
                result.append({"key": key[:48], "title": title[:128]})
        return result or DEFAULT_TOPICS
    except Exception:
        return DEFAULT_TOPICS


TOPIC_SPECS = load_topics()

# Phase 1: Ukrainians in Europe. The catalogue is intentionally simple and expandable.
EUROPE = {
    "DE": {"name": "🇩🇪 Deutschland", "cities": ["Berlin", "München", "Hamburg", "Köln", "Münster"]},
    "PL": {"name": "🇵🇱 Polen", "cities": ["Warschau", "Krakau", "Breslau", "Danzig"]},
    "ES": {"name": "🇪🇸 Spanien", "cities": ["Barcelona", "Madrid", "Valencia", "Alicante"]},
    "NL": {"name": "🇳🇱 Niederlande", "cities": ["Amsterdam", "Rotterdam", "Den Haag", "Eindhoven"]},
    "CZ": {"name": "🇨🇿 Tschechien", "cities": ["Prag", "Brünn"]},
}


def city_key(name):
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def ensure_postal_db():
    if POSTAL_DB.exists():
        return
    archive = Path(__file__).with_name("postal.zip")
    urllib.request.urlretrieve("https://download.geonames.org/export/zip/allCountries.zip", archive)
    europe = set("AL AD AT BY BE BA BG HR CY CZ DK EE FI FR DE GR HU IS IE IT LV LI LT LU MT MD MC ME NL MK NO PL PT RO RU SM RS SK SI ES SE CH TR UA GB VA XK".split())
    with sqlite3.connect(POSTAL_DB) as db, zipfile.ZipFile(archive) as z:
        db.execute("CREATE TABLE postal_places(country_code TEXT,postal_code TEXT,place_name TEXT,admin_name1 TEXT,admin_code1 TEXT,admin_name2 TEXT,admin_code2 TEXT,admin_name3 TEXT,admin_code3 TEXT,latitude REAL,longitude REAL,accuracy INTEGER,PRIMARY KEY(country_code,postal_code,place_name))")
        reader = csv.reader((x.decode("utf-8") for x in z.open("allCountries.txt")), delimiter="\t")
        rows = []
        for r in reader:
            if len(r) >= 12 and r[0] in europe:
                rows.append((r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8],float(r[9]) if r[9] else None,float(r[10]) if r[10] else None,int(r[11]) if r[11] else None))
                if len(rows) >= 10000:
                    db.executemany("INSERT OR IGNORE INTO postal_places VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows); rows=[]
        if rows:
            db.executemany("INSERT OR IGNORE INTO postal_places VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("CREATE INDEX idx_postal_lookup ON postal_places(country_code,postal_code)")
        db.commit()
    archive.unlink(missing_ok=True)


def postal_lookup(country_code, postal_code):
    """Return all places matching the exact country + postal-code key."""
    if not POSTAL_DB.exists():
        return []
    code = postal_code.strip().upper()
    with sqlite3.connect(POSTAL_DB) as conn:
        rows = conn.execute(
            "SELECT place_name,admin_name1,latitude,longitude FROM postal_places "
            "WHERE country_code=? AND postal_code=? ORDER BY place_name",
            (country_code.upper(), code),
        ).fetchall()
    return [{"city": r[0], "region": r[1], "lat": r[2], "lon": r[3]} for r in rows]


def forum_key_for(country_code, place):
    """Stable city-community key: country + region + city, independent of postal code."""
    region = city_key(place.get("region") or "") or "region"
    city = city_key(place.get("city") or "") or "city"
    return f"{country_code.casefold()}:{region}:{city}"


class Store:
    def __init__(self, database_url=""):
        self.database_url = database_url
        self.sqlite_path = Path(__file__).with_name("forum_bot.sqlite3")

    @property
    def postgres(self):
        return bool(self.database_url)

    def connect(self):
        if self.postgres:
            return psycopg.connect(self.database_url)
        return sqlite3.connect(self.sqlite_path)

    def init(self):
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS forum_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS forum_topics (
                    chat_id BIGINT NOT NULL,
                    topic_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    thread_id BIGINT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    PRIMARY KEY (chat_id, topic_key)
                )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS city_forums (
                    country_code TEXT NOT NULL,
                    country_name TEXT NOT NULL,
                    postal_code TEXT NOT NULL,
                    city_key TEXT NOT NULL,
                    city_name TEXT NOT NULL,
                    forum_key TEXT NOT NULL,
                    chat_id BIGINT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    PRIMARY KEY (country_code, postal_code)
                )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS forum_users (
                    telegram_user_id BIGINT PRIMARY KEY,
                    country_code TEXT,
                    postal_code TEXT,
                    city_key TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.commit()

    def set_setting(self, key, value):
        sql = (
            "INSERT INTO forum_settings(setting_key,setting_value) VALUES(%s,%s) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value"
            if self.postgres
            else "INSERT INTO forum_settings(setting_key,setting_value) VALUES(?,?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value"
        )
        with self.connect() as conn:
            conn.cursor().execute(sql, (key, str(value)))
            conn.commit()

    def get_setting(self, key):
        sql = (
            "SELECT setting_value FROM forum_settings WHERE setting_key=%s"
            if self.postgres
            else "SELECT setting_value FROM forum_settings WHERE setting_key=?"
        )
        with self.connect() as conn:
            row = conn.cursor().execute(sql, (key,)).fetchone()
        return row[0] if row else None

    def topics(self, chat_id):
        sql = (
            "SELECT topic_key,title,thread_id,active FROM forum_topics WHERE chat_id=%s ORDER BY thread_id"
            if self.postgres
            else "SELECT topic_key,title,thread_id,active FROM forum_topics WHERE chat_id=? ORDER BY thread_id"
        )
        with self.connect() as conn:
            rows = conn.cursor().execute(sql, (int(chat_id),)).fetchall()
        return [
            {"key": r[0], "title": r[1], "thread_id": int(r[2]), "active": bool(r[3])}
            for r in rows
        ]

    def save_topic(self, chat_id, key, title, thread_id):
        sql = (
            "INSERT INTO forum_topics(chat_id,topic_key,title,thread_id,active) "
            "VALUES(%s,%s,%s,%s,TRUE) ON CONFLICT(chat_id,topic_key) DO UPDATE SET "
            "title=EXCLUDED.title,thread_id=EXCLUDED.thread_id,active=TRUE"
            if self.postgres
            else "INSERT INTO forum_topics(chat_id,topic_key,title,thread_id,active) "
            "VALUES(?,?,?,?,1) ON CONFLICT(chat_id,topic_key) DO UPDATE SET "
            "title=excluded.title,thread_id=excluded.thread_id,active=1"
        )
        with self.connect() as conn:
            conn.cursor().execute(sql, (int(chat_id), key, title, int(thread_id)))
            conn.commit()

    def bind_postal_place(self, country_code, country_name, postal_code, place):
        forum_key = forum_key_for(country_code, place)
        city = place["city"]
        ckey = city_key(city)
        sql = (
            "INSERT INTO city_forums(country_code,country_name,postal_code,city_key,city_name,forum_key,chat_id,active) "
            "VALUES(%s,%s,%s,%s,%s,%s,NULL,TRUE) ON CONFLICT(country_code,postal_code) DO UPDATE SET "
            "country_name=EXCLUDED.country_name,city_key=EXCLUDED.city_key,city_name=EXCLUDED.city_name,forum_key=EXCLUDED.forum_key,active=TRUE"
            if self.postgres else
            "INSERT INTO city_forums(country_code,country_name,postal_code,city_key,city_name,forum_key,chat_id,active) "
            "VALUES(?,?,?,?,?,?,NULL,1) ON CONFLICT(country_code,postal_code) DO UPDATE SET "
            "country_name=excluded.country_name,city_key=excluded.city_key,city_name=excluded.city_name,forum_key=excluded.forum_key,active=1"
        )
        with self.connect() as conn:
            conn.cursor().execute(sql, (country_code, country_name, postal_code, ckey, city, forum_key))
            conn.commit()
        return forum_key

    def save_user_location(self, telegram_user_id, country_code, postal_code, city_key_value):
        sql = (
            "INSERT INTO forum_users(telegram_user_id,country_code,postal_code,city_key,updated_at) "
            "VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP) ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "country_code=EXCLUDED.country_code,postal_code=EXCLUDED.postal_code,city_key=EXCLUDED.city_key,updated_at=CURRENT_TIMESTAMP"
            if self.postgres else
            "INSERT INTO forum_users(telegram_user_id,country_code,postal_code,city_key,updated_at) "
            "VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "country_code=excluded.country_code,postal_code=excluded.postal_code,city_key=excluded.city_key,updated_at=CURRENT_TIMESTAMP"
        )
        with self.connect() as conn:
            conn.cursor().execute(sql, (int(telegram_user_id), country_code, postal_code, city_key_value))
            conn.commit()

    def user_location(self, telegram_user_id):
        ph = "%s" if self.postgres else "?"
        sql = f"SELECT country_code,postal_code,city_key FROM forum_users WHERE telegram_user_id={ph}"
        with self.connect() as conn:
            row = conn.cursor().execute(sql, (int(telegram_user_id),)).fetchone()
        return {"country_code": row[0], "postal_code": row[1], "city_key": row[2]} if row else None

    def bind_forum_chat(self, forum_key, chat_id):
        ph = "%s" if self.postgres else "?"
        sql = f"UPDATE city_forums SET chat_id={ph} WHERE forum_key={ph}"
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, (int(chat_id), forum_key))
            conn.commit()
            return cur.rowcount

    def forum_chat(self, forum_key):
        ph = "%s" if self.postgres else "?"
        sql = f"SELECT chat_id FROM city_forums WHERE forum_key={ph} AND chat_id IS NOT NULL LIMIT 1"
        with self.connect() as conn:
            row = conn.cursor().execute(sql, (forum_key,)).fetchone()
        return int(row[0]) if row else None

    def set_active(self, chat_id, key, active):
        placeholder = "%s" if self.postgres else "?"
        sql = (
            f"UPDATE forum_topics SET active={placeholder} "
            f"WHERE chat_id={placeholder} AND topic_key={placeholder}"
        )
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, (bool(active), int(chat_id), key))
            conn.commit()
            return cur.rowcount


STORE = Store(DATABASE_URL)


def user_lang(update):
    code = (getattr(update.effective_user, "language_code", "") or "en").lower()
    if code.startswith("uk"): return "uk"
    if code.startswith("de"): return "de"
    return "en"


def tr(lang, de, uk, en):
    return {"de": de, "uk": uk, "en": en}.get(lang, en)


def country_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(data["name"], callback_data=f"country:{code}")]
        for code, data in EUROPE.items()
    ])


def city_keyboard(code):
    rows = [
        [InlineKeyboardButton(city, callback_data=f"city:{code}:{city_key(city)}")]
        for city in EUROPE[code]["cities"]
    ]
    rows.append([InlineKeyboardButton("⬅️ Länder", callback_data="countries")])
    return InlineKeyboardMarkup(rows)


async def location_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"🇺🇦 EuropeUAConnectBot\n\n🌍 {tr(user_lang(update), 'Land auswählen:', 'Оберіть країну:', 'Choose your country:')}",
        reply_markup=country_keyboard(),
    )


async def location_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "countries":
        await query.edit_message_text("🌍 " + tr(user_lang(update), "Land auswählen:", "Оберіть країну:", "Choose your country:"), reply_markup=country_keyboard())
        return
    if data.startswith("country:"):
        code = data.split(":", 1)[1]
        if code in EUROPE:
            context.user_data["country_code"] = code
            context.user_data["awaiting_postal"] = True
            await query.edit_message_text(
                f'{EUROPE[code]["name"]}\n\n📮 {tr(user_lang(update), "Bitte PLZ eingeben:", "Введіть поштовий індекс:", "Enter your postal code:")}'
            )
        return
    if data.startswith("city:"):
        _, code, key = data.split(":", 2)
        city = next((x for x in EUROPE.get(code, {}).get("cities", []) if city_key(x) == key), None)
        if city:
            await query.edit_message_text(
                f"✅ {EUROPE[code]['name']} → {city}\n\nDas Stadtforum wird hier verbunden. Die 10 Threads sind für jede Stadt identisch.\n\n🌍 /location – Land/Stadt wechseln"
            )


async def postal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_postal"):
        return
    code = context.user_data.get("country_code")
    postal = (update.effective_message.text or "").strip().upper()
    places = postal_lookup(code, postal)
    if not places:
        await update.effective_message.reply_text(
            "❌ PLZ nicht gefunden. Bitte erneut eingeben oder /location für anderes Land."
        )
        return
    context.user_data["awaiting_postal"] = False
    context.user_data["postal_code"] = postal
    place = places[0]
    context.user_data["city_name"] = place["city"]
    forum_key = STORE.bind_postal_place(code, EUROPE[code]["name"], postal, place)
    context.user_data["forum_key"] = forum_key
    region = f' · {place["region"]}' if place["region"] else ""
    rows = [[InlineKeyboardButton(f"Thread {i}", callback_data=f"thread:{i}")] for i in range(1, 11)]
    rows.append([InlineKeyboardButton("🌍 Land / PLZ wechseln", callback_data="countries")])
    await update.effective_message.reply_text(
        f"✅ {EUROPE[code]['name']} → {postal} → {place['city']}{region}\n\n"
        "Lokales Forum · Thema auswählen:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def thread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    number = (query.data or "").split(":", 1)[1]
    city = context.user_data.get("city_name", "Ort")
    postal = context.user_data.get("postal_code", "")
    forum_key = context.user_data.get("forum_key")
    chat_id = STORE.forum_chat(forum_key) if forum_key else None
    if not chat_id:
        await query.edit_message_text(
            f"📍 {postal} {city}\n🧵 Thread {number}\n\n⏳ Dieses Stadtforum ist noch nicht aktiviert."
        )
        return
    topic = next((x for x in STORE.topics(chat_id) if x["key"] == f"thread_{int(number):02d}" and x["active"]), None)
    if not topic:
        await query.edit_message_text(f"📍 {postal} {city}\n🧵 Thread {number}\n\n⏳ Thread noch nicht aktiviert.")
        return
    forum = await context.bot.get_chat(chat_id)
    url = topic_link(chat_id, forum.username, topic["thread_id"])
    await query.edit_message_text(
        f"📍 {postal} {city}\n🧵 {topic['title']}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Thread öffnen", url=url)]])
    )


def topic_link(chat_id, username, thread_id):
    if username:
        return f"https://t.me/{username}/{thread_id}"
    internal = str(abs(int(chat_id)))
    if internal.startswith("100"):
        internal = internal[3:]
    return f"https://t.me/c/{internal}/{thread_id}"


async def caller_is_admin(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return False
    if user.id in ADMIN_IDS:
        return True
    if not chat or chat.type not in {ChatType.SUPERGROUP, ChatType.GROUP}:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
    except Exception:
        return False


async def require_admin(update, context):
    if await caller_is_admin(update, context):
        return True
    await update.effective_message.reply_text("⛔ Только администратор / Nur Administratoren.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = STORE.get_setting("forum_chat_id")
    if not chat_id:
        await update.effective_message.reply_text(
            "Forum noch nicht eingerichtet. Admin: Bot zur Forum-Supergruppe hinzufügen "
            "und dort /forum_setup ausführen."
        )
        return
    forum = await context.bot.get_chat(int(chat_id))
    topics = [x for x in STORE.topics(chat_id) if x["active"]]
    if not topics:
        await update.effective_message.reply_text("Noch keine Topics eingerichtet.")
        return
    rows = [
        [InlineKeyboardButton(t["title"], url=topic_link(chat_id, forum.username, t["thread_id"]))]
        for t in topics
    ]
    await update.effective_message.reply_text(
        "🇺🇦 Münster Ukraine Forum\n\nВыберите тему / Thema auswählen:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def forum_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    if chat.type != ChatType.SUPERGROUP or not getattr(chat, "is_forum", False):
        await update.effective_message.reply_text(
            "Diese Gruppe muss eine Telegram-Supergruppe mit aktivierten Themen (Forum) sein."
        )
        return
    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        can_manage = bool(getattr(me, "can_manage_topics", False))
    except Exception:
        can_manage = False
    if not can_manage:
        await update.effective_message.reply_text(
            "Der Bot braucht Adminrechte mit ‘Themen verwalten / Manage Topics’."
        )
        return

    # Optional city binding: /forum_setup <forum_key>. Example: /forum_setup de:nordrhein_westfalen:munster
    forum_key = context.args[0].strip() if context.args else None
    if forum_key:
        bound = STORE.bind_forum_chat(forum_key, chat.id)
        if not bound:
            await update.effective_message.reply_text("❌ Forum-Key noch nicht in der PLZ-Datenbank aktiviert.")
            return
    STORE.set_setting("forum_chat_id", chat.id)
    existing = {x["key"]: x for x in STORE.topics(chat.id)}
    created, kept = [], []
    for spec in TOPIC_SPECS:
        if spec["key"] in existing:
            kept.append(spec["title"])
            continue
        topic = await context.bot.create_forum_topic(chat.id, spec["title"])
        STORE.save_topic(chat.id, spec["key"], spec["title"], topic.message_thread_id)
        created.append(spec["title"])

    await update.effective_message.reply_text(
        "✅ Forum verbunden.\n"
        f"Chat ID: {chat.id}\n"
        f"Neu erstellt: {len(created)}\n"
        f"Bereits vorhanden: {len(kept)}"
    )


async def forum_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    if chat.type != ChatType.SUPERGROUP or not getattr(chat, "is_forum", False):
        await update.effective_message.reply_text("Befehl bitte in einer Telegram-Forumgruppe benutzen.")
        return
    existing = {x["key"]: x for x in STORE.topics(chat.id)}
    created, renamed = 0, 0
    for spec in TOPIC_SPECS:
        current = existing.get(spec["key"])
        if not current:
            topic = await context.bot.create_forum_topic(chat.id, spec["title"])
            STORE.save_topic(chat.id, spec["key"], spec["title"], topic.message_thread_id)
            created += 1
            continue
        if current["title"] != spec["title"]:
            await context.bot.edit_forum_topic(chat.id, current["thread_id"], name=spec["title"])
            STORE.save_topic(chat.id, spec["key"], spec["title"], current["thread_id"])
            renamed += 1
    await update.effective_message.reply_text(
        f"✅ Thread-Vorlage synchronisiert.\nNeu: {created}\nUmbenannt: {renamed}\nSoll: {len(TOPIC_SPECS)}"
    )


async def forum_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = STORE.get_setting("forum_chat_id")
    if not chat_id:
        await update.effective_message.reply_text("Forum noch nicht eingerichtet.")
        return
    forum = await context.bot.get_chat(int(chat_id))
    topics = STORE.topics(chat_id)
    if not topics:
        await update.effective_message.reply_text("Keine gespeicherten Topics.")
        return
    lines = ["📚 Forum-Topics:"]
    for t in topics:
        state = "✅" if t["active"] else "⏸"
        link = topic_link(chat_id, forum.username, t["thread_id"])
        lines.append(f'{state} {t["key"]} — {t["title"]}\n{link}')
    await update.effective_message.reply_text("\n\n".join(lines), disable_web_page_preview=True)


def make_key(title):
    key = re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")
    return (key or "topic")[:48]


async def city_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Nutzung: /city_activate <LAND> <PLZ>")
        return
    code, postal = context.args[0].upper(), context.args[1].upper()
    if code not in EUROPE:
        await update.effective_message.reply_text("❌ Land ist noch nicht im Bot-Ländermenü aktiviert.")
        return
    places = postal_lookup(code, postal)
    if not places:
        await update.effective_message.reply_text("❌ PLZ nicht in der Europa-Datenbank gefunden.")
        return
    place = places[0]
    key = STORE.bind_postal_place(code, EUROPE[code]["name"], postal, place)
    await update.effective_message.reply_text(
        f"✅ Stadt vorbereitet: {code} {postal} {place['city']}\nForum-Key: {key}\n\n"
        f"Nächster Schritt in der passenden Telegram-Forumgruppe: /forum_setup {key}"
    )


async def forum_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    if chat.type != ChatType.SUPERGROUP or not getattr(chat, "is_forum", False):
        await update.effective_message.reply_text("Befehl bitte in der Forumgruppe benutzen.")
        return
    title = " ".join(context.args).strip()
    if not title:
        await update.effective_message.reply_text("Nutzung: /forum_add Name des Topics")
        return
    key = make_key(title)
    existing = {x["key"] for x in STORE.topics(chat.id)}
    suffix = 2
    base_key = key
    while key in existing:
        key = f"{base_key[:42]}_{suffix}"
        suffix += 1
    topic = await context.bot.create_forum_topic(chat.id, title[:128])
    STORE.save_topic(chat.id, key, title[:128], topic.message_thread_id)
    STORE.set_setting("forum_chat_id", chat.id)
    await update.effective_message.reply_text(
        f"✅ Topic erstellt: {title}\nKey: {key}\nThread ID: {topic.message_thread_id}"
    )


async def forum_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Nutzung: /forum_close topic_key")
        return
    chat = update.effective_chat
    key = context.args[0]
    topic = next((x for x in STORE.topics(chat.id) if x["key"] == key), None)
    if not topic:
        await update.effective_message.reply_text("Topic-Key nicht gefunden.")
        return
    await context.bot.close_forum_topic(chat.id, topic["thread_id"])
    STORE.set_active(chat.id, key, False)
    await update.effective_message.reply_text(f"⏸ Topic geschlossen: {topic['title']}")


async def forum_reopen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Nutzung: /forum_reopen topic_key")
        return
    chat = update.effective_chat
    key = context.args[0]
    topic = next((x for x in STORE.topics(chat.id) if x["key"] == key), None)
    if not topic:
        await update.effective_message.reply_text("Topic-Key nicht gefunden.")
        return
    await context.bot.reopen_forum_topic(chat.id, topic["thread_id"])
    STORE.set_active(chat.id, key, True)
    await update.effective_message.reply_text(f"▶️ Topic wieder geöffnet: {topic['title']}")


async def forum_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = STORE.get_setting("forum_chat_id")
    if not chat_id:
        await update.effective_message.reply_text("❌ Noch keine Forumgruppe verbunden.")
        return
    topics = STORE.topics(chat_id)
    active = sum(1 for x in topics if x["active"])
    await update.effective_message.reply_text(
        f"✅ Forum verbunden\nChat ID: {chat_id}\nTopics: {len(topics)}\nAktiv: {active}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Українці в Європі | Ukrainer in Europa\n\n"
        "/start – Topic-Auswahl\n"
        "/forum_topics – alle Topics\n"
        "/forum_status – Status\n\n"
        "Admin:\n"
        "/forum_setup – Forum verbinden + Standardtopics erstellen\n"
        "/forum_add <Name> – neues Topic\n"
        "/forum_close <key> – Topic schließen\n"
        "/forum_reopen <key> – Topic wieder öffnen"
    )


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Forum öffnen"),
        BotCommand("forum_topics", "Topics anzeigen"),
        BotCommand("forum_status", "Forum-Status"),
        BotCommand("help", "Hilfe"),
    ])


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN fehlt.")
    ensure_postal_db()
    STORE.init()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", location_menu))
    app.add_handler(CommandHandler("location", location_menu))
    app.add_handler(CallbackQueryHandler(location_callback, pattern=r"^(countries|country:|city:)"))
    app.add_handler(CallbackQueryHandler(thread_callback, pattern=r"^thread:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, postal_message))
    app.add_handler(CommandHandler("forum_setup", forum_setup))
    app.add_handler(CommandHandler("forum_topics", forum_topics))
    app.add_handler(CommandHandler("forum_sync", forum_sync))
    app.add_handler(CommandHandler("city_activate", city_activate))
    app.add_handler(CommandHandler("forum_add", forum_add))
    app.add_handler(CommandHandler("forum_close", forum_close))
    app.add_handler(CommandHandler("forum_reopen", forum_reopen))
    app.add_handler(CommandHandler("forum_status", forum_status))
    app.add_handler(CommandHandler("help", help_command))
    print("Ukrainer in Europa Forum Bot started.", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
