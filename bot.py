# bot.py — Telegram-бот с должностями, модерацией, перм-бан/мут и системой отношений
# Установка: pip install aiogram aiosqlite
# Запуск: python bot.py

import asyncio
import random
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message
from aiogram.utils.markdown import hcode

# ==================== КОНФИГ ====================
BOT_TOKEN = "8628783756:AAFWgpGgwlnkf0pDUPwzuz_LZaukxqIUnpA"
OWNER_IDS = {8985370261}  # Замени на свои user_id
DB_PATH = "bot_data.db"

RANKS = [
    "Новичок",
    "Мл модер",
    "Модер",
    "Ст модер",
    "Мл админ",
    "Админ",
    "Ст админ",
    "Зам гл админа",
    "Гл админ",
    "Зам владельца",
    "Владелец",
]

MUTE_LIMITS = {
    "Мл модер":      (3600, 4),
    "Модер":         (7200, 5),
    "Ст модер":      (21600, 6),
    "Мл админ":      (43200, 8),
    "Админ":         (86400, 10),
    "Ст админ":      (172800, 12),
    "Зам гл админа": (604800, 15),
    "Гл админ":      (1209600, 20),
    "Зам владельца": (0, 999999),
    "Владелец":      (0, 999999),
}

BAN_LIMITS = {
    "Мл модер":      (6 * 3600, 2),
    "Модер":         (12 * 3600, 2),
    "Ст модер":      (86400, 2),
    "Мл админ":      (3 * 86400, 2),
    "Админ":         (7 * 86400, 2),
    "Ст админ":      (14 * 86400, 2),
    "Зам гл админа": (30 * 86400, 3),
    "Гл админ":      (90 * 86400, 4),
    "Зам владельца": (0, 999999),
    "Владелец":      (0, 999999),
}

ONLINE_CACHE: set[int] = set()
WARNS: dict[tuple[int, int], int] = {}
NOTES: dict[tuple[int, int], str] = {}
RULES: dict[int, str] = {}
WELCOME: dict[int, str] = {}
TRIGGERS: dict[int, dict[str, str]] = {}
BLACKLIST: dict[int, set[int]] = {}
FILTER_WORDS: dict[int, set[str]] = {}


# ==================== УТИЛИТЫ ====================
def rank_index(rank: str) -> int:
    try:
        return RANKS.index(rank)
    except ValueError:
        return -1


def has_rank(user_rank: str, min_rank: str) -> bool:
    return rank_index(user_rank) >= rank_index(min_rank)


def parse_duration(text: str):
    if not text or len(text) < 2:
        return None
    unit = text[-1].lower()
    try:
        value = int(text[:-1])
    except ValueError:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    return value * mult if mult else None


def human_duration(seconds: int) -> str:
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    if s and not parts: parts.append(f"{s}с")
    return " ".join(parts) or "0с"


def now_utc() -> datetime:
    return datetime.utcnow()


# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                chat_id INTEGER,
                username TEXT,
                rank TEXT DEFAULT 'Новичок',
                muted_until TEXT,
                banned_until TEXT,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                actor_id INTEGER,
                target_id INTEGER,
                action TEXT,
                reason TEXT,
                ts TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS limits (
                user_id INTEGER,
                chat_id INTEGER,
                action TEXT,
                used INTEGER DEFAULT 0,
                reset_at TEXT,
                PRIMARY KEY (user_id, chat_id, action)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cmd_access (
                command TEXT PRIMARY KEY,
                min_rank TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS marriages (
                chat_id INTEGER,
                user1_id INTEGER,
                user2_id INTEGER,
                married_at TEXT,
                PRIMARY KEY (chat_id, user1_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                chat_id INTEGER,
                from_id INTEGER,
                to_id INTEGER,
                ts TEXT,
                PRIMARY KEY (chat_id, from_id)
            )
        """)
        await db.commit()
        for cmd in ("up", "down"):
            await db.execute(
                "INSERT OR IGNORE INTO cmd_access (command, min_rank) VALUES (?, ?)",
                (cmd, "Зам владельца"),
            )
        await db.commit()


async def get_user(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE user_id=? AND chat_id=?",
            (user_id, chat_id),
        )
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users (user_id, chat_id, rank) VALUES (?, ?, 'Новичок')",
                (user_id, chat_id),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT * FROM users WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            )
            row = await cur.fetchone()
        return dict(row)


async def get_rank(user_id: int, chat_id: int) -> str:
    u = await get_user(user_id, chat_id)
    return u["rank"]


async def set_rank(user_id: int, chat_id: int, rank: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET rank=? WHERE user_id=? AND chat_id=?",
            (rank, user_id, chat_id),
        )
        await db.commit()


async def add_log(chat_id, actor_id, target_id, action, reason=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (chat_id, actor_id, target_id, action, reason, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, actor_id, target_id, action, reason, now_utc().isoformat()),
        )
        await db.commit()


async def get_logs(chat_id: int, limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


def _next_reset(action: str, now: datetime) -> datetime:
    msk_now = now + timedelta(hours=3)
    target = msk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if target <= msk_now:
        target += timedelta(days=1)
    if action == "ban":
        target += timedelta(days=1)
    return target - timedelta(hours=3)


async def get_limit(user_id: int, chat_id: int, action: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM limits WHERE user_id=? AND chat_id=? AND action=?",
            (user_id, chat_id, action),
        )
        row = await cur.fetchone()
        now = now_utc()
        if row is None:
            reset = _next_reset(action, now)
            await db.execute(
                "INSERT INTO limits (user_id, chat_id, action, used, reset_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (user_id, chat_id, action, reset.isoformat()),
            )
            await db.commit()
            return {"used": 0, "reset_at": reset}
        data = dict(row)
        reset_at = datetime.fromisoformat(data["reset_at"])
        if now >= reset_at:
            reset = _next_reset(action, now)
            await db.execute(
                "UPDATE limits SET used=0, reset_at=? "
                "WHERE user_id=? AND chat_id=? AND action=?",
                (reset.isoformat(), user_id, chat_id, action),
            )
            await db.commit()
            return {"used": 0, "reset_at": reset}
        return {"used": data["used"], "reset_at": reset_at}


async def inc_limit(user_id: int, chat_id: int, action: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE limits SET used = used + 1 "
            "WHERE user_id=? AND chat_id=? AND action=?",
            (user_id, chat_id, action),
        )
        await db.commit()


async def get_cmd_min_rank(command: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT min_rank FROM cmd_access WHERE command=?", (command,)
        )
        row = await cur.fetchone()
        return row["min_rank"] if row else "Владелец"


async def set_cmd_min_rank(command: str, min_rank: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO cmd_access (command, min_rank) VALUES (?, ?) "
            "ON CONFLICT(command) DO UPDATE SET min_rank=excluded.min_rank",
            (command, min_rank),
        )
        await db.commit()


# ==================== СВАДЬБЫ (БД) ====================
async def get_partner(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM marriages WHERE chat_id=? AND (user1_id=? OR user2_id=?)",
            (chat_id, user_id, user_id),
        )
        return await cur.fetchone()


async def create_marriage(chat_id: int, u1: int, u2: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO marriages (chat_id, user1_id, user2_id, married_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, u1, u2, now_utc().isoformat()),
        )
        await db.commit()


async def delete_marriage(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM marriages WHERE chat_id=? AND (user1_id=? OR user2_id=?)",
            (chat_id, user_id, user_id),
        )
        await db.commit()


async def save_proposal(chat_id: int, from_id: int, to_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO proposals (chat_id, from_id, to_id, ts) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, from_id, to_id, now_utc().isoformat()),
        )
        await db.commit()


async def get_proposal(chat_id: int, from_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM proposals WHERE chat_id=? AND from_id=?",
            (chat_id, from_id),
        )
        return await cur.fetchone()


async def delete_proposal(chat_id: int, from_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM proposals WHERE chat_id=? AND from_id=?",
            (chat_id, from_id),
        )
        await db.commit()


async def top_marriages(chat_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM marriages WHERE chat_id=? ORDER BY married_at ASC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


# ==================== ФИЛЬТРЫ ====================
class RankFilter(BaseFilter):
    def __init__(self, min_rank: str):
        self.min_rank = min_rank

    async def __call__(self, message: Message) -> bool:
        if message.from_user.id in OWNER_IDS:
            return True
        if message.chat.type == "private":
            return message.from_user.id in OWNER_IDS
        rank = await get_rank(message.from_user.id, message.chat.id)
        return has_rank(rank, self.min_rank)


class CmdRankFilter(BaseFilter):
    def __init__(self, command: str):
        self.command = command

    async def __call__(self, message: Message) -> bool:
        if message.from_user.id in OWNER_IDS:
            return True
        min_rank = await get_cmd_min_rank(self.command)
        if message.chat.type == "private":
            return False
        rank = await get_rank(message.from_user.id, message.chat.id)
        return has_rank(rank, min_rank)


# ==================== РОУТЕР ====================
router = Router()


@router.message(F.text)
async def track_online(message: Message):
    ONLINE_CACHE.add(message.from_user.id)
    triggers = TRIGGERS.get(message.chat.id, {})
    if message.text.lower() in triggers:
        await message.reply(triggers[message.text.lower()])
    words = FILTER_WORDS.get(message.chat.id, set())
    if words:
        lowered = message.text.lower().split()
        if any(w.strip(".,!?") in words for w in lowered):
            try:
                await message.delete()
                await message.answer(
                    f"⚠️ {message.from_user.mention_html()}, такие слова запрещены.",
                    parse_mode="HTML")
            except Exception:
                pass


# ---------- БАЗОВЫЕ ----------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_user(message.from_user.id, message.chat.id)
    await message.answer(
        "👋 Привет! Я бот управления чатом.\n"
        "Используй /help для списка команд."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Команды:</b>\n"
        "/start /help /help_command /myrank /rules /info\n\n"
        "<b>Просмотр:</b>\n"
        "/checkadmin /log /stats /warns\n\n"
        "<b>Модерация:</b>\n"
        "/mute /unmute /ban /unban /kick /warn /unwarn /clearwarns\n"
        "/del /purge /pin /unpin /slowmode /lock /unlock\n\n"
        "<b>Перм-модерация (Ст модер+):</b>\n"
        "/pmute &lt;юзер&gt; [причина] — мут навсегда\n"
        "/pban &lt;юзер&gt; [причина] — бан навсегда\n"
        "/unpmute &lt;юзер&gt; — снять перм-мут\n"
        "/pmutelist — список перм-мутов\n\n"
        "<b>Ранги:</b>\n"
        "/up /down /setrank\n\n"
        "<b>Заметки:</b>\n"
        "/note /notes\n\n"
        "<b>Отношения:</b>\n"
        "/love /yes /no /divorce /partner\n"
        "/kiss /hug /marriages /ship\n\n"
        "<b>Владелец:</b>\n"
        "/setcmd /setrules /setwelcome /addtrigger /deltrigger\n"
        "/addword /delword /addblacklist /delblacklist"
    )


@router.message(Command("help_command"))
async def cmd_help_command(message: Message):
    lines = ["🧭 <b>Должности:</b>\n"]
    for i, r in enumerate(RANKS, 1):
        lines.append(f"{i}. {r}")
    lines.append(f"\n/up и /down — с {await get_cmd_min_rank('up')} и выше.")
    lines.append("\n💍 Свадьбы: /love, /yes, /no, /divorce, /partner, "
                 "/kiss, /hug, /marriages, /ship")
    await message.answer("\n".join(lines))


@router.message(Command("myrank"))
async def cmd_myrank(message: Message):
    r = await get_rank(message.from_user.id, message.chat.id)
    await message.reply(f"🎖 Твой ранг: <b>{r}</b>")


@router.message(Command("info"))
async def cmd_info(message: Message):
    if message.reply_to_message:
        u = message.reply_to_message.from_user
    else:
        u = message.from_user
    rank = await get_rank(u.id, message.chat.id)
    warns = WARNS.get((message.chat.id, u.id), 0)
    note = NOTES.get((message.chat.id, u.id), "—")
    p = await get_partner(message.chat.id, u.id)
    if p:
        pid = p["user2_id"] if p["user1_id"] == u.id else p["user1_id"]
        partner_line = f"❤️ Партнёр: <a href=\"tg://user?id={pid}\">{pid}</a>"
    else:
        partner_line = "💔 Партнёр: нет"
    await message.reply(
        f"👤 <b>{u.full_name}</b>\n"
        f"🆔 <code>{u.id}</code>\n"
        f"🎖 Ранг: <b>{rank}</b>\n"
        f"⚠️ Варны: <b>{warns}</b>\n"
        f"{partner_line}\n"
        f"📝 Заметка: {note}",
        parse_mode="HTML")


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    text = RULES.get(message.chat.id)
    await message.reply(text or "📜 Правила ещё не установлены.")


# ---------- ПРОСМОТР ----------
@router.message(Command("checkadmin"))
async def cmd_checkadmin(message: Message):
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
    except Exception:
        return await message.reply("Не удалось получить список.")
    lines = ["👑 <b>Администрация:</b>\n"]
    for a in admins:
        u = a.user
        name = u.full_name or u.username or str(u.id)
        st = "🟢" if u.id in ONLINE_CACHE else "⚪"
        lines.append(f"{st} {name}")
    await message.answer("\n".join(lines))


@router.message(Command("log"))
async def cmd_log(message: Message):
    logs = await get_logs(message.chat.id, 20)
    if not logs:
        return await message.answer("Логов нет.")
    lines = ["📜 <b>Логи:</b>\n"]
    for l in logs:
        ts = datetime.fromisoformat(l["ts"]).strftime("%d.%m %H:%M")
        lines.append(f"[{ts}] {hcode(l['action'])} {l['actor_id']}→{l['target_id']} ({l['reason'] or '—'})")
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE chat_id=?", (message.chat.id,))
        (uc,) = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=?", (message.chat.id,))
        (lc,) = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM marriages WHERE chat_id=?", (message.chat.id,))
        (mc,) = await cur.fetchone()
    await message.reply(
        f"📊 <b>Статистика чата</b>\n"
        f"👥 Пользователей в БД: <b>{uc}</b>\n"
        f"📜 Логов: <b>{lc}</b>\n"
        f"💍 Пар: <b>{mc}</b>\n"
        f"⚠️ Варнов: <b>{sum(v for k, v in WARNS.items() if k[0]==message.chat.id)}</b>"
    )


# ---------- МОДЕРАЦИЯ ----------
async def _resolve_target_id(message: Message, arg: str):
    try:
        return int(arg)
    except ValueError:
        pass
    arg = arg.lstrip("@")
    try:
        m = await message.bot.get_chat_member(message.chat.id, arg)
        return m.user.id
    except Exception:
        return None


@router.message(Command("mute"), RankFilter("Мл модер"))
async def cmd_mute(message: Message):
    args = message.text.split(maxsplit=3)
    if len(args) < 4:
        return await message.reply("Использование: /mute &lt;юзер&gt; &lt;время&gt; &lt;причина&gt;")
    _, target_arg, time_arg, reason = args
    seconds = parse_duration(time_arg)
    if not seconds:
        return await message.reply("Неверный формат времени.")
    target_id = await _resolve_target_id(message, target_arg)
    if not target_id:
        return await message.reply("Пользователь не найден.")
    actor_rank = await get_rank(message.from_user.id, message.chat.id)
    max_sec, max_count = MUTE_LIMITS.get(actor_rank, (0, 0))
    if max_sec and seconds > max_sec:
        return await message.reply(f"Максимум {human_duration(max_sec)}.")
    lim = await get_limit(message.from_user.id, message.chat.id, "mute")
    if max_count and lim["used"] >= max_count:
        return await message.reply(f"Лимит исчерпан ({lim['used']}/{max_count}).")
    until = now_utc() + timedelta(seconds=seconds)
    try:
        await message.bot.restrict_chat_member(
            message.chat.id, target_id,
            permissions={"can_send_messages": False, "can_send_audios": False,
                         "can_send_documents": False, "can_send_photos": False,
                         "can_send_videos": False, "can_send_video_notes": False,
                         "can_send_voice_notes": False, "can_send_polls": False,
                         "can_send_other_messages": False, "can_add_web_page_previews": False},
            until_date=until)
    except Exception as e:
        return await message.reply(f"Ошибка: {e}")
    await inc_limit(message.from_user.id, message.chat.id, "mute")
    await add_log(message.chat.id, message.from_user.id, target_id, "mute",
                  f"{human_duration(seconds)}: {reason}")
    await message.reply(f"🔇 {target_id} замучен на {human_duration(seconds)}. Причина: {reason}")


@router.message(Command("unmute"), RankFilter("Мл модер"))
async def cmd_unmute(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /unmute &lt;юзер&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    await message.bot.restrict_chat_member(
        message.chat.id, tid,
        permissions={"can_send_messages": True, "can_send_audios": True,
                     "can_send_documents": True, "can_send_photos": True,
                     "can_send_videos": True, "can_send_video_notes": True,
                     "can_send_voice_notes": True, "can_send_polls": True,
                     "can_send_other_messages": True, "can_add_web_page_previews": True})
    await add_log(message.chat.id, message.from_user.id, tid, "unmute")
    await message.reply(f"🔊 {tid} размучен.")


@router.message(Command("ban"), RankFilter("Мл модер"))
async def cmd_ban(message: Message):
    args = message.text.split(maxsplit=3)
    if len(args) < 4:
        return await message.reply("Использование: /ban &lt;юзер&gt; &lt;время&gt; &lt;причина&gt;")
    _, target_arg, time_arg, reason = args
    seconds = parse_duration(time_arg)
    if not seconds:
        return await message.reply("Неверный формат времени.")
    tid = await _resolve_target_id(message, target_arg)
    if not tid:
        return await message.reply("Не найден.")
    actor_rank = await get_rank(message.from_user.id, message.chat.id)
    max_sec, max_count = BAN_LIMITS.get(actor_rank, (0, 0))
    if max_sec and seconds > max_sec:
        return await message.reply(f"Максимум {human_duration(max_sec)}.")
    lim = await get_limit(message.from_user.id, message.chat.id, "ban")
    if max_count and lim["used"] >= max_count:
        return await message.reply(f"Лимит исчерпан ({lim['used']}/{max_count}).")
    until = now_utc() + timedelta(seconds=seconds)
    try:
        await message.bot.ban_chat_member(message.chat.id, tid, until_date=until)
    except Exception as e:
        return await message.reply(f"Ошибка: {e}")
    await inc_limit(message.from_user.id, message.chat.id, "ban")
    await add_log(message.chat.id, message.from_user.id, tid, "ban",
                  f"{human_duration(seconds)}: {reason}")
    await message.reply(f"🚫 {tid} забанен на {human_duration(seconds)}. Причина: {reason}")


@router.message(Command("unban"), RankFilter("Мл модер"))
async def cmd_unban(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /unban &lt;юзер&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    await message.bot.unban_chat_member(message.chat.id, tid)
    await add_log(message.chat.id, message.from_user.id, tid, "unban")
    await message.reply(f"✅ {tid} разбанен.")


@router.message(Command("kick"), RankFilter("Модер"))
async def cmd_kick(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /kick &lt;юзер&gt; [причина]")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    reason = " ".join(args[2:]) or "не указана"
    await message.bot.ban_chat_member(message.chat.id, tid)
    await message.bot.unban_chat_member(message.chat.id, tid)
    await add_log(message.chat.id, message.from_user.id, tid, "kick", reason)
    await message.reply(f"👢 {tid} кикнут. Причина: {reason}")


# ---------- ПЕРМ МУТ / ПЕРМ БАН ----------
async def _perm_mute(message: Message, reason: str):
    args = message.text.split(maxsplit=2)
    tid = None
    if message.reply_to_message:
        tid = message.reply_to_message.from_user.id
    elif len(args) >= 2:
        tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Использование: /pmute &lt;юзер&gt; [причина] (или реплай)")
    try:
        await message.bot.restrict_chat_member(
            message.chat.id, tid,
            permissions={"can_send_messages": False, "can_send_audios": False,
                         "can_send_documents": False, "can_send_photos": False,
                         "can_send_videos": False, "can_send_video_notes": False,
                         "can_send_voice_notes": False, "can_send_polls": False,
                         "can_send_other_messages": False, "can_add_web_page_previews": False})
    except Exception as e:
        return await message.reply(f"Ошибка: {e}")
    await add_log(message.chat.id, message.from_user.id, tid, "perm_mute", reason)
    await message.reply(f"🔇 {tid} замучен <b>навсегда</b>. Причина: {reason}")


async def _perm_ban(message: Message, reason: str):
    args = message.text.split(maxsplit=2)
    tid = None
    if message.reply_to_message:
        tid = message.reply_to_message.from_user.id
    elif len(args) >= 2:
        tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Использование: /pban &lt;юзер&gt; [причина] (или реплай)")
    try:
        await message.bot.ban_chat_member(message.chat.id, tid)
    except Exception as e:
        return await message.reply(f"Ошибка: {e}")
    await add_log(message.chat.id, message.from_user.id, tid, "perm_ban", reason)
    await message.reply(f"🚫 {tid} забанен <b>навсегда</b>. Причина: {reason}")


@router.message(Command("pmute"), RankFilter("Ст модер"))
async def cmd_pmute(message: Message):
    args = message.text.split(maxsplit=2)
    reason = "не указана"
    if message.reply_to_message and len(args) > 1:
        reason = " ".join(args[1:])
    elif len(args) > 2:
        reason = args[2]
    await _perm_mute(message, reason)


@router.message(Command("pban"), RankFilter("Ст модер"))
async def cmd_pban(message: Message):
    args = message.text.split(maxsplit=2)
    reason = "не указана"
    if message.reply_to_message and len(args) > 1:
        reason = " ".join(args[1:])
    elif len(args) > 2:
        reason = args[2]
    await _perm_ban(message, reason)


@router.message(Command("pmutelist"), RankFilter("Мл модер"))
async def cmd_pmutelist(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT target_id FROM logs WHERE chat_id=? AND action='perm_mute' "
            "AND target_id NOT IN (SELECT target_id FROM logs WHERE chat_id=? "
            "AND action='unperm_mute')",
            (message.chat.id, message.chat.id),
        )
        rows = await cur.fetchall()
    if not rows:
        return await message.reply("Нет пользователей с перм-мутом.")
    lines = ["🔇 <b>Перм-муты:</b>\n"]
    for r in rows:
        lines.append(f"• <code>{r['target_id']}</code>")
    await message.answer("\n".join(lines))


@router.message(Command("unpmute"), RankFilter("Ст модер"))
async def cmd_unpmute(message: Message):
    args = message.text.split()
    tid = message.reply_to_message.from_user.id if message.reply_to_message else (
        await _resolve_target_id(message, args[1]) if len(args) > 1 else None)
    if not tid:
        return await message.reply("Укажи юзера или ответь на сообщение.")
    await message.bot.restrict_chat_member(
        message.chat.id, tid,
        permissions={"can_send_messages": True, "can_send_audios": True,
                     "can_send_documents": True, "can_send_photos": True,
                     "can_send_videos": True, "can_send_video_notes": True,
                     "can_send_voice_notes": True, "can_send_polls": True,
                     "can_send_other_messages": True, "can_add_web_page_previews": True})
    await add_log(message.chat.id, message.from_user.id, tid, "unperm_mute")
    await message.reply(f"🔊 {tid} размучен.")


# ---------- ВАРНЫ ----------
@router.message(Command("warn"), RankFilter("Мл модер"))
async def cmd_warn(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("Использование: /warn &lt;юзер&gt; [причина]")
    tid = await _resolve_target_id(message, args[1].split()[0])
    if not tid:
        return await message.reply("Не найден.")
    reason = args[1].split(maxsplit=1)[1] if " " in args[1] else "не указана"
    key = (message.chat.id, tid)
    WARNS[key] = WARNS.get(key, 0) + 1
    await add_log(message.chat.id, message.from_user.id, tid, "warn",
                  f"{WARNS[key]}/3: {reason}")
    if WARNS[key] >= 3:
        until = now_utc() + timedelta(hours=1)
        await message.bot.restrict_chat_member(
            message.chat.id, tid,
            permissions={"can_send_messages": False},
            until_date=until)
        WARNS[key] = 0
        await message.reply(f"⚠️ {tid} получил 3/3 варнов и замучен на 1ч.")
    else:
        await message.reply(f"⚠️ {tid} получил предупреждение {WARNS[key]}/3. Причина: {reason}")


@router.message(Command("unwarn"), RankFilter("Мл модер"))
async def cmd_unwarn(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /unwarn &lt;юзер&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    key = (message.chat.id, tid)
    if WARNS.get(key, 0) > 0:
        WARNS[key] -= 1
    await add_log(message.chat.id, message.from_user.id, tid, "unwarn")
    await message.reply(f"✅ Снято предупреждение. Осталось: {WARNS.get(key,0)}")


@router.message(Command("warns"))
async def cmd_warns(message: Message):
    if message.reply_to_message:
        u = message.reply_to_message.from_user
    else:
        u = message.from_user
    c = WARNS.get((message.chat.id, u.id), 0)
    await message.reply(f"⚠️ {u.full_name}: {c}/3 предупреждений.")


@router.message(Command("clearwarns"), RankFilter("Админ"))
async def cmd_clearwarns(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /clearwarns &lt;юзер&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    WARNS[(message.chat.id, tid)] = 0
    await add_log(message.chat.id, message.from_user.id, tid, "clearwarns")
    await message.reply(f"✅ Варны {tid} обнулены.")


# ---------- СООБЩЕНИЯ ----------
@router.message(Command("del"), RankFilter("Модер"))
async def cmd_del(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь на сообщение.")
    await message.reply_to_message.delete()
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("purge"), RankFilter("Админ"))
async def cmd_purge(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("Использование: /purge &lt;кол-во&gt;")
    n = min(int(args[1]), 100)
    deleted = 0
    for offset in range(0, n + 1):
        try:
            await message.bot.delete_message(message.chat.id, message.message_id - offset)
            deleted += 1
        except Exception:
            pass
    await message.answer(f"🧹 Удалено {deleted} сообщений.")


@router.message(Command("pin"), RankFilter("Модер"))
async def cmd_pin(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь на сообщение.")
    try:
        await message.reply_to_message.pin(disable_notification=False)
        await message.reply("📌 Закреплено.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("unpin"), RankFilter("Модер"))
async def cmd_unpin(message: Message):
    try:
        await message.bot.unpin_chat_message(message.chat.id)
        await message.reply("📌 Откреплено.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("slowmode"), RankFilter("Админ"))
async def cmd_slowmode(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("Использование: /slowmode &lt;секунды&gt;")
    sec = int(args[1])
    try:
        await message.bot.set_chat_slow_mode(message.chat.id, sec)
        await message.reply(f"🐌 Slow mode: {sec} сек.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("lock"), RankFilter("Админ"))
async def cmd_lock(message: Message):
    try:
        await message.bot.set_chat_permissions(
            message.chat.id,
            permissions={"can_send_messages": False})
        await message.reply("🔒 Чат закрыт.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("unlock"), RankFilter("Админ"))
async def cmd_unlock(message: Message):
    try:
        await message.bot.set_chat_permissions(
            message.chat.id,
            permissions={"can_send_messages": True, "can_send_media_messages": True,
                         "can_send_other_messages": True, "can_add_web_page_previews": True})
        await message.reply("🔓 Чат открыт.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


# ---------- РАНГИ ----------
@router.message(Command("up"), CmdRankFilter("up"))
async def cmd_up(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /up &lt;юзер&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    cur = await get_rank(tid, message.chat.id)
    idx = rank_index(cur)
    if idx >= len(RANKS) - 1:
        return await message.reply("Уже максимум.")
    new = RANKS[idx + 1]
    await set_rank(tid, message.chat.id, new)
    await add_log(message.chat.id, message.from_user.id, tid, "up", f"{cur}→{new}")
    await message.reply(f"⬆️ {tid}: {cur} → {new}")


@router.message(Command("down"), CmdRankFilter("down"))
async def cmd_down(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /down &lt;юзер&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    cur = await get_rank(tid, message.chat.id)
    idx = rank_index(cur)
    if idx <= 0:
        return await message.reply("Уже минимум.")
    new = RANKS[idx - 1]
    await set_rank(tid, message.chat.id, new)
    await add_log(message.chat.id, message.from_user.id, tid, "down", f"{cur}→{new}")
    await message.reply(f"⬇️ {tid}: {cur} → {new}")


@router.message(Command("setrank"), RankFilter("Зам гл админа"))
async def cmd_setrank(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply("Использование: /setrank &lt;юзер&gt; &lt;ранг&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    rank = args[2].strip()
    if rank not in RANKS:
        return await message.reply(f"Неизвестный ранг. Доступные: {', '.join(RANKS)}")
    cur = await get_rank(tid, message.chat.id)
    await set_rank(tid, message.chat.id, rank)
    await add_log(message.chat.id, message.from_user.id, tid, "setrank", f"{cur}→{rank}")
    await message.reply(f"✅ {tid}: {cur} → {rank}")


# ---------- ЗАМЕТКИ ----------
@router.message(Command("note"), RankFilter("Мл модер"))
async def cmd_note(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply("Использование: /note &lt;юзер&gt; &lt;текст&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    NOTES[(message.chat.id, tid)] = args[2]
    await message.reply(f"📝 Заметка сохранена для {tid}.")


@router.message(Command("notes"), RankFilter("Мл модер"))
async def cmd_notes(message: Message):
    chat_notes = {k: v for k, v in NOTES.items() if k[0] == message.chat.id}
    if not chat_notes:
        return await message.reply("Заметок нет.")
    lines = ["📝 <b>Заметки:</b>\n"]
    for (_, uid), txt in chat_notes.items():
        lines.append(f"• <code>{uid}</code> — {txt}")
    await message.answer("\n".join(lines))


# ---------- ВЛАДЕЛЕЦ ----------
@router.message(Command("setcmd"))
async def cmd_setcmd(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply("Использование: /setcmd &lt;команда&gt; &lt;ранг&gt;")
    cmd = args[1].lstrip("/")
    rank = args[2].strip()
    if rank not in RANKS:
        return await message.reply(f"Неизвестный ранг. Доступные: {', '.join(RANKS)}")
    await set_cmd_min_rank(cmd, rank)
    await add_log(message.chat.id, message.from_user.id, 0, "setcmd", f"{cmd}→{rank}")
    await message.reply(f"✅ /{cmd} теперь с {rank}.")


@router.message(Command("setrules"))
async def cmd_setrules(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("Использование: /setrules &lt;текст&gt;")
    RULES[message.chat.id] = args[1]
    await message.reply("📜 Правила обновлены.")


@router.message(Command("setwelcome"))
async def cmd_setwelcome(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("Использование: /setwelcome &lt;текст с {name}&gt;")
    WELCOME[message.chat.id] = args[1]
    await message.reply("👋 Приветствие обновлено.")


@router.message(Command("addtrigger"))
async def cmd_addtrigger(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply("Использование: /addtrigger &lt;слово&gt; &lt;ответ&gt;")
    TRIGGERS.setdefault(message.chat.id, {})[args[1].lower()] = args[2]
    await message.reply(f"✅ Триггер «{args[1]}» добавлен.")


@router.message(Command("deltrigger"))
async def cmd_deltrigger(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /deltrigger &lt;слово&gt;")
    TRIGGERS.get(message.chat.id, {}).pop(args[1].lower(), None)
    await message.reply("🗑 Удалено.")


@router.message(Command("addword"))
async def cmd_addword(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /addword &lt;слово&gt;")
    FILTER_WORDS.setdefault(message.chat.id, set()).add(args[1].lower())
    await message.reply(f"✅ Слово «{args[1]}» в фильтре.")


@router.message(Command("delword"))
async def cmd_delword(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /delword &lt;слово&gt;")
    FILTER_WORDS.get(message.chat.id, set()).discard(args[1].lower())
    await message.reply("🗑 Удалено из фильтра.")


@router.message(Command("addblacklist"))
async def cmd_addblacklist(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /addblacklist &lt;user_id&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    BLACKLIST.setdefault(message.chat.id, set()).add(tid)
    await message.bot.ban_chat_member(message.chat.id, tid)
    await message.reply(f"⛔ {tid} в чёрном списке.")


@router.message(Command("delblacklist"))
async def cmd_delblacklist(message: Message):
    if message.from_user.id not in OWNER_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /delblacklist &lt;user_id&gt;")
    tid = await _resolve_target_id(message, args[1])
    if not tid:
        return await message.reply("Не найден.")
    BLACKLIST.get(message.chat.id, set()).discard(tid)
    await message.bot.unban_chat_member(message.chat.id, tid)
    await message.reply(f"✅ {tid} убран из ЧС.")


# ==================== СИСТЕМА ОТНОШЕНИЙ ====================
@router.message(Command("love"))
async def cmd_love(message: Message):
    args = message.text.split(maxsplit=2)
    tid = None
    note = ""
    if message.reply_to_message:
        tid = message.reply_to_message.from_user.id
        note = " ".join(args[1:]) if len(args) > 1 else ""
    elif len(args) >= 2:
        tid = await _resolve_target_id(message, args[1])
        note = args[2] if len(args) > 2 else ""
    if not tid:
        return await message.reply("Использование: /love &lt;юзер&gt; [сообщение] (или реплай)")
    if tid == message.from_user.id:
        return await message.reply("😅 Нельзя признаться в любви самому себе.")
    p1 = await get_partner(message.chat.id, message.from_user.id)
    p2 = await get_partner(message.chat.id, tid)
    if p1:
        return await message.reply("💔 Ты уже в отношениях. Сначала /divorce.")
    if p2:
        return await message.reply("💔 Этот человек уже в отношениях.")
    await save_proposal(message.chat.id, message.from_user.id, tid)
    text = f"💌 {message.from_user.mention_html()} признаётся в любви!\n"
    text += f"❤️ Объект чувств: <a href=\"tg://user?id={tid}\">{tid}</a>\n"
    if note:
        text += f"\n💬 {note}\n"
    text += f"\nЕсли согласен(на) — ответь /yes {message.from_user.id}"
    text += f"\nЕсли нет — /no {message.from_user.id}"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("yes"))
async def cmd_yes(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /yes &lt;user_id&gt;")
    try:
        from_id = int(args[1])
    except ValueError:
        return await message.reply("Неверный ID.")
    if from_id == message.from_user.id:
        return await message.reply("😅 Нельзя ответить самому себе.")
    prop = await get_proposal(message.chat.id, from_id)
    if not prop or prop["to_id"] != message.from_user.id:
        return await message.reply("❌ Нет активного предложения от этого пользователя.")
    if await get_partner(message.chat.id, from_id):
        return await message.reply("💔 Этот пользователь уже занят.")
    if await get_partner(message.chat.id, message.from_user.id):
        return await message.reply("💔 Ты уже в отношениях.")
    await create_marriage(message.chat.id, from_id, message.from_user.id)
    await delete_proposal(message.chat.id, from_id)
    await add_log(message.chat.id, from_id, message.from_user.id, "marriage")
    await message.answer(
        f"💍🎉 <a href=\"tg://user?id={from_id}\">{from_id}</a> и "
        f"<a href=\"tg://user?id={message.from_user.id}\">{message.from_user.id}</a> "
        f"теперь вместе! Поздравляем! ❤️",
        parse_mode="HTML")


@router.message(Command("no"))
async def cmd_no(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Использование: /no &lt;user_id&gt;")
    try:
        from_id = int(args[1])
    except ValueError:
        return await message.reply("Неверный ID.")
    prop = await get_proposal(message.chat.id, from_id)
    if prop and prop["to_id"] == message.from_user.id:
        await delete_proposal(message.chat.id, from_id)
        await message.reply("💔 Отказ принят. Предложение удалено.")


@router.message(Command("divorce"))
async def cmd_divorce(message: Message):
    p = await get_partner(message.chat.id, message.from_user.id)
    if not p:
        return await message.reply("💔 Ты не в отношениях.")
    partner_id = p["user2_id"] if p["user1_id"] == message.from_user.id else p["user1_id"]
    await delete_marriage(message.chat.id, message.from_user.id)
    await add_log(message.chat.id, message.from_user.id, partner_id, "divorce")
    await message.answer(
        f"💔 <a href=\"tg://user?id={message.from_user.id}\">{message.from_user.id}</a> "
        f"разводится с <a href=\"tg://user?id={partner_id}\">{partner_id}</a>.",
        parse_mode="HTML")


@router.message(Command("partner"))
async def cmd_partner(message: Message):
    uid = message.reply_to_message.from_user.id if message.reply_to_message else message.from_user.id
    p = await get_partner(message.chat.id, uid)
    if not p:
        return await message.reply("💔 Свободен(а).")
    partner_id = p["user2_id"] if p["user1_id"] == uid else p["user1_id"]
    days = (now_utc() - datetime.fromisoformat(p["married_at"])).days
    await message.reply(
        f"❤️ <a href=\"tg://user?id={partner_id}\">{partner_id}</a> — "
        f"вместе уже <b>{days}</b> дн.",
        parse_mode="HTML")


@router.message(Command("kiss"))
async def cmd_kiss(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь на сообщение того, кого хочешь поцеловать.")
    p = await get_partner(message.chat.id, message.from_user.id)
    target = message.reply_to_message.from_user.id
    partner_id = None
    if p:
        partner_id = p["user2_id"] if p["user1_id"] == message.from_user.id else p["user1_id"]
    if partner_id != target:
        await message.reply("😳 Ты пытаешься поцеловать не своего партнёра! /love?")
        return
    await message.answer(
        f"😘 {message.from_user.mention_html()} целует "
        f"{message.reply_to_message.from_user.mention_html()}! ❤️",
        parse_mode="HTML")


@router.message(Command("hug"))
async def cmd_hug(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь на сообщение, чтобы обнять.")
    await message.answer(
        f"🤗 {message.from_user.mention_html()} обнимает "
        f"{message.reply_to_message.from_user.mention_html()}!",
        parse_mode="HTML")


@router.message(Command("marriages"))
async def cmd_marriages(message: Message):
    rows = await top_marriages(message.chat.id, 10)
    if not rows:
        return await message.reply("💔 В этом чате пока нет пар.")
    lines = ["💍 <b>Пары чата:</b>\n"]
    for r in rows:
        days = (now_utc() - datetime.fromisoformat(r["married_at"])).days
        lines.append(
            f"❤️ <a href=\"tg://user?id={r['user1_id']}\">{r['user1_id']}</a> + "
            f"<a href=\"tg://user?id={r['user2_id']}\">{r['user2_id']}</a> "
            f"— {days} дн."
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("ship"))
async def cmd_ship(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответь на сообщение — и узнай совместимость.")
    a = message.from_user.id
    b = message.reply_to_message.from_user.id
    if a == b:
        return await message.reply("😅 Сам с собой? Совместимость 100%.")
    seed = (min(a, b) << 20) ^ max(a, b)
    random.seed(seed)
    pct = random.randint(0, 100)
    bar = "❤️" * (pct // 10) + "🖤" * (10 - pct // 10)
    await message.answer(
        f"💘 Совместимость {message.from_user.mention_html()} и "
        f"{message.reply_to_message.from_user.mention_html()}:\n"
        f"{bar} <b>{pct}%</b>",
        parse_mode="HTML")


# ==================== ЗАПУСК ====================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
