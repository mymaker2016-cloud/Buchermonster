import os
import re
import json
import sqlite3
import logging
import base64
import tempfile
import time
import math
import random
import asyncio
from pathlib import Path

import httpx
from telegram.error import BadRequest
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
)
import anthropic
from groq import Groq

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
GROQ_KEY       = os.environ["GROQ_API_KEY"]
PARENT_CHAT_ID = int(os.environ["PARENT_CHAT_ID"])

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
groq   = Groq(api_key=GROQ_KEY)

(WAIT_CONTINUE, WAIT_BOOK_TITLE, WAIT_PAGES, WAIT_PHOTOS, WAIT_AUDIO,
 WAIT_TIMER, WAIT_QA) = range(7)
sessions: dict[int, dict] = {}

MIN_PAGES_PER_SESSION = 30  # минимум страниц за одну сдачу

# ── Настройки (меняются переменными окружения, без правки кода) ──
MAX_PAGES_PER_HOUR = int(os.environ.get("MAX_PAGES_PER_HOUR", "50"))   # потолок скорости чтения (13 лет)
QA_COUNT           = int(os.environ.get("QA_COUNT", "5"))              # случайных вопросов по деталям
QA_MIN_CORRECT     = int(os.environ.get("QA_MIN_CORRECT", "4"))        # сколько из QA_COUNT должно быть верными
PROBE_MAX          = int(os.environ.get("PROBE_MAX", "3"))             # макс. наводящих вопросов о пропущенном
RETELL_MIN_SCORE   = int(os.environ.get("RETELL_MIN_SCORE", "30"))     # ниже — пересказ слишком скудный
QA_SLOW_SEC        = int(os.environ.get("QA_SLOW_SEC", "90"))          # ответ дольше — помечаем родителю
ALLOW_ONLINE_TEXT  = os.environ.get("ALLOW_ONLINE_TEXT", "0") == "1"   # 0 = всегда требовать фото страниц
TIMER_STALE_SEC    = 3 * 3600                                          # завершённый таймер «протухает» через 3 ч

def count_pages(pages: str) -> int:
    """Считает кол-во страниц из строки '10-40' → 30, '12' → 1."""
    m = re.match(r"^(\d+)[\-–](\d+)$", pages)
    if m:
        return abs(int(m.group(2)) - int(m.group(1))) + 1
    return 1

# ══════════════════════════════════════════════════════════════════
#  DB
# ══════════════════════════════════════════════════════════════════

DB_PATH = os.environ.get("DB_PATH", "/app/data/reading.db")

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS progress (
        chat_id INTEGER, book TEXT, pages TEXT, passed INTEGER, score INTEGER,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, book, pages))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS last_book (
        chat_id INTEGER PRIMARY KEY, book TEXT, pages TEXT,
        book_text TEXT DEFAULT '', source TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reading_timer (
        chat_id INTEGER PRIMARY KEY, start_ts REAL, end_ts REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pending (
        chat_id INTEGER PRIMARY KEY, book TEXT, pages TEXT,
        moments TEXT, qpool TEXT, used TEXT DEFAULT '[]',
        attempt INTEGER DEFAULT 1, book_text TEXT DEFAULT '', source TEXT DEFAULT '')""")
    conn.commit()
    return conn

def get_last(chat_id):
    r = db().execute("SELECT book, pages, book_text, source FROM last_book WHERE chat_id=?", (chat_id,)).fetchone()
    return {"book": r[0], "pages": r[1], "book_text": r[2] or "", "source": r[3] or ""} if r else None

def save_last(chat_id, book, pages, book_text="", source=""):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO last_book VALUES (?,?,?,?,?)",
                  (chat_id, book, pages, book_text, source))

def already_passed(chat_id, book, pages):
    r = db().execute(
        "SELECT passed FROM progress WHERE chat_id=? AND book=? AND pages=?",
        (chat_id, book.lower().strip(), pages.strip())
    ).fetchone()
    return bool(r and r[0] == 1)

def save_result(chat_id, book, pages, passed, score):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                  (chat_id, book.lower().strip(), pages.strip(), 1 if passed else 0, score))

def get_history(chat_id):
    rows = db().execute(
        "SELECT book, pages, passed, score FROM progress WHERE chat_id=? ORDER BY ts DESC LIMIT 15",
        (chat_id,)
    ).fetchall()
    return [{"book": r[0], "pages": r[1], "passed": r[2], "score": r[3]} for r in rows]

def save_photo_hashes(chat_id: int, book: str, pages: str, hashes: list[str]):
    """Сохраняем хэши фото чтобы не принимать те же снимки повторно."""
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS photo_hashes (
            chat_id INTEGER, book TEXT, pages TEXT, hash TEXT,
            PRIMARY KEY (chat_id, hash))""")
        for h in hashes:
            c.execute("INSERT OR IGNORE INTO photo_hashes VALUES (?,?,?,?)",
                      (chat_id, book.lower().strip(), pages, h))

def get_seen_hashes(chat_id: int) -> set[str]:
    """Все хэши фото которые этот пользователь уже отправлял."""
    try:
        rows = db().execute(
            "SELECT hash FROM photo_hashes WHERE chat_id=?", (chat_id,)
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()

# ══════════════════════════════════════════════════════════════════
#  BOOK SEARCH  — Google Books API + Open Library (быстро, без зависания)
# ══════════════════════════════════════════════════════════════════

HEADERS = {"User-Agent": "ReadingBot/1.0"}
TIMEOUT = 8  # сек

def _get(url: str, params: dict = None) -> dict | str | None:
    try:
        r = httpx.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            return r.json() if "json" in ct else r.text
    except Exception as e:
        logger.warning("HTTP %s: %s", url, e)
    return None


def search_google_books(title: str) -> str | None:
    """Ищет книгу в Google Books, возвращает текст preview или None."""
    data = _get("https://www.googleapis.com/books/v1/volumes", {"q": title, "maxResults": 3, "langRestrict": "ru"})
    if not data or not isinstance(data, dict):
        return None
    items = data.get("items", [])
    for item in items:
        vi = item.get("volumeInfo", {})
        ap = item.get("accessInfo", {})
        # Проверяем есть ли текстовый фрагмент
        epub = ap.get("epub", {})
        pdf  = ap.get("pdf", {})
        if ap.get("viewability") in ("PARTIAL", "ALL_PAGES"):
            # Берём описание как fallback — для детских книг часто достаточно
            desc = vi.get("description", "")
            if len(desc) > 100:
                logger.info("Google Books: found description for '%s'", title)
                return f"[Аннотация из Google Books]\n{desc}"
    return None


def search_open_library(title: str, pages: str) -> str | None:
    """Open Library — ищет книгу и пробует получить текст."""
    data = _get("https://openlibrary.org/search.json", {"q": title, "limit": 3, "language": "rus"})
    if not data or not isinstance(data, dict):
        return None
    docs = data.get("docs", [])
    if not docs:
        # Пробуем без языкового фильтра
        data = _get("https://openlibrary.org/search.json", {"q": title, "limit": 3})
        if data and isinstance(data, dict):
            docs = data.get("docs", [])
    if not docs:
        return None

    doc = docs[0]
    key = doc.get("key", "")
    title_found = doc.get("title", "")
    logger.info("Open Library: found '%s'", title_found)

    # Пробуем получить текст через Internet Archive
    ia_id = doc.get("ia", [None])[0] if doc.get("ia") else None
    if ia_id:
        text_url = f"https://archive.org/stream/{ia_id}/{ia_id}_djvu.txt"
        text = _get(text_url)
        if text and isinstance(text, str) and len(text) > 500:
            # Обрезаем до разумного размера
            return text[:6000]

    return None


def extract_pages_with_claude(raw_text: str, title: str, pages: str, source: str) -> str | None:
    """Claude вычленяет нужные страницы из большого текста."""
    prompt = (
        f"Из текста книги «{title}» извлеки содержимое страниц {pages}.\n"
        f"Если страниц нет или текст не относится к этой книге — ответь: НЕТ\n"
        f"Иначе — верни только текст страниц, без пояснений.\n\n"
        f"ТЕКСТ (первые 5000 символов):\n{raw_text[:5000]}"
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        result = resp.content[0].text.strip()
        if result == "НЕТ" or len(result) < 80:
            return None
        return result
    except Exception as e:
        logger.error("Claude extract error: %s", e)
        return None


def search_book_text(title: str, pages: str) -> dict:
    """
    Главная функция поиска. Возвращает {"found": bool, "text": str, "source": str}
    Порядок: Google Books → Open Library → не найдено.
    Таймаут каждого запроса: 8 сек. Общий максимум: ~20 сек.
    """
    # 1. Google Books
    text = search_google_books(title)
    if text:
        extracted = extract_pages_with_claude(text, title, pages, "Google Books")
        if extracted:
            return {"found": True, "text": extracted, "source": "Google Books"}

    # 2. Open Library / Internet Archive
    text = search_open_library(title, pages)
    if text:
        extracted = extract_pages_with_claude(text, title, pages, "Open Library")
        if extracted:
            return {"found": True, "text": extracted, "source": "Open Library"}

    return {"found": False, "text": "", "source": ""}

# ══════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════════

def kb_continue(title):
    return ReplyKeyboardMarkup(
        [[f'📖 Продолжаю «{title[:25]}»', "📚 Другая книга"]],
        resize_keyboard=True, one_time_keyboard=True)

def kb_photo():
    return ReplyKeyboardMarkup([["📷 Все фото отправил"]], resize_keyboard=True, one_time_keyboard=True)

def kb_cancel():
    return ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True, one_time_keyboard=True)

def kb_none():
    return ReplyKeyboardRemove()

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════

async def dl_photo(bot, file_id) -> tuple[str, str]:
    """Скачать фото → (base64, sha256_hash)."""
    import hashlib
    f = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as t:
        await f.download_to_drive(t.name); p = t.name
    with open(p, "rb") as fh:
        raw = fh.read()
    data = base64.b64encode(raw).decode()
    h    = hashlib.sha256(raw).hexdigest()[:16]
    Path(p).unlink(missing_ok=True)
    return data, h

def verify_photos_with_claude(photos_b64: list[str], declared_pages: str, book: str) -> dict:
    """
    Claude проверяет фото:
    - Реально ли там страницы книги (а не рандомные картинки)
    - Совпадают ли номера страниц с заявленными
    - Достаточно ли страниц (≥ MIN_PAGES_PER_SESSION)
    Возвращает {"ok": bool, "actual_pages": int, "reason": str}
    """
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
        for b in photos_b64
    ]
    content.append({"type": "text", "text": f"""
Ребёнок утверждает что прочитал страницы {declared_pages} книги «{book}» и прислал фото.

Проверь:
1. Это реально страницы книги (текст, а не картинки/фото природы/скриншоты)?
2. Видны ли номера страниц на фото? Если да — соответствуют ли они диапазону {declared_pages}?
3. Сколько примерно страниц книги видно на всех фото вместе?

Ответь ТОЛЬКО чистым JSON без markdown:
{{"ok": true/false, "actual_pages": число_страниц_на_фото, "reason": "причина отказа или 'всё верно'"}}

Правила:
- ok=false если это не страницы книги
- ok=false если номера страниц видны и НЕ совпадают с {declared_pages}
- ok=false если страниц меньше {MIN_PAGES_PER_SESSION}
- ok=true если всё в порядке или номера страниц не видны но текст выглядит как книга
"""})
    resp = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=256,
        messages=[{"role": "user", "content": content}]
    )
    return _parse_json(resp.content[0].text)

async def dl_voice(bot, file_id) -> bytes:
    f = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as t:
        await f.download_to_drive(t.name); p = t.name
    with open(p, "rb") as fh: data = fh.read()
    Path(p).unlink(missing_ok=True)
    return data

def transcribe(audio: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as t:
        t.write(audio); p = t.name
    try:
        with open(p, "rb") as fh:
            return groq.audio.transcriptions.create(
                file=("audio.ogg", fh, "audio/ogg"),
                model="whisper-large-v3", language="ru", response_format="text"
            ).strip()
    finally:
        Path(p).unlink(missing_ok=True)

def extract_key_moments(book_text: str, pages: str, title: str) -> list[dict]:
    """Шаг 1: извлечь 3-5 ключевых момента из текста."""
    prompt = (
        f"Ты опытный учитель литературы. Прочитай текст книги «{title}» (стр. {pages}).\n\n"
        "Выдели 3-5 КЛЮЧЕВЫХ момента которые ребёнок ОБЯЗАН упомянуть в пересказе.\n"
        "Критерии: сюжетные повороты, действия героев, важные события.\n\n"
        "Ответь ТОЛЬКО чистым JSON — массив объектов:\n"
        '[{"moment": "описание (1 предл.)", "required": true}, ...]\n\n'
        "required=true — 2-3 самых важных (без них текст явно не прочитан).\n"
        "required=false — детали, желательные но не обязательные.\n\n"
        f"ТЕКСТ:\n\"\"\"\n{book_text[:4000]}\n\"\"\"\n"
    )
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("["):
                raw = part; break
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    try:
        return json.loads(m.group() if m else raw)
    except Exception:
        return []


def extract_key_moments_from_photos(photos: list[str], pages: str, title: str) -> tuple[list[dict], str]:
    """Шаг 1 для фото: OCR + ключевые моменты за один вызов."""
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
        for b in photos
    ]
    content.append({"type": "text", "text": (
        f"На фото — страницы {pages} книги «{title}».\n\n"
        "Сделай два дела:\n"
        "1. Прочитай текст с фото.\n"
        "2. Выдели 3-5 КЛЮЧЕВЫХ момента которые ребёнок ОБЯЗАН упомянуть (required=true для 2-3 самых важных).\n\n"
        "Ответь ТОЛЬКО чистым JSON:\n"
        '{"book_text": "первые 300 символов текста", "key_moments": [{"moment": "...", "required": true}]}'
    )})
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=2048,
        messages=[{"role": "user", "content": content}]
    )
    data = _parse_json(resp.content[0].text)
    return data.get("key_moments", []), data.get("book_text", "")


def check_retelling_against_moments(key_moments: list[dict], retelling: str, pages: str, title: str) -> dict:
    """Шаг 2: проверить пересказ против ключевых моментов."""
    moments_text = "\n".join(
        f"- {'[ОБЯЗАТЕЛЬНО]' if m.get('required') else '[ЖЕЛАТЕЛЬНО]'} {m['moment']}"
        for m in key_moments
    )
    prompt = (
        f"Ты добрый но строгий учитель начальной школы.\n\n"
        f"КНИГА: «{title}», страницы {pages}\n\n"
        f"КЛЮЧЕВЫЕ МОМЕНТЫ которые должен упомянуть ребёнок:\n{moments_text}\n\n"
        f"ПЕРЕСКАЗ РЕБЁНКА:\n\"\"\"\n{retelling}\n\"\"\"\n\n"
        "Проверь КАЖДЫЙ момент — упомянут ли он (точно или близко по смыслу).\n\n"
        "Правила:\n"
        "- passed=false если пропущен хотя бы ОДИН [ОБЯЗАТЕЛЬНО] момент\n"
        "- score = % упомянутых моментов из всех\n"
        "- Засчитывай если смысл передан другими словами\n\n"
        "Ответь ТОЛЬКО чистым JSON:\n"
        '{"passed": true/false, "score": 0-100, '
        '"feedback": "для ребёнка — тёплая похвала за старание, 1 предложение; НЕ перечисляй и НЕ намекай, что он пропустил", '
        '"summary": "для родителя — что упомянул, что пропустил (2-3 предл.)", '
        '"missed_required": ["..."], "missed_optional": ["..."]}'
    )
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return _parse_json(resp.content[0].text)


def check_by_text(book_text: str, retelling: str, pages: str, title: str) -> dict:
    """Двухэтапная проверка по тексту книги."""
    moments = extract_key_moments(book_text, pages, title)
    if not moments:
        moments = [{"moment": "основные события и герои", "required": True}]
    return check_retelling_against_moments(moments, retelling, pages, title)


def check_by_photos(photos: list[str], retelling: str, pages: str, title: str) -> dict:
    """Двухэтапная проверка по фото."""
    moments, _ = extract_key_moments_from_photos(photos, pages, title)
    if not moments:
        moments = [{"moment": "основные события и герои", "required": True}]
    return check_retelling_against_moments(moments, retelling, pages, title)


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                raw = part; break
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(m.group() if m else raw)

# ══════════════════════════════════════════════════════════════════
#  ТАЙМЕР / PENDING (БД)
# ══════════════════════════════════════════════════════════════════

def timer_get(cid: int) -> dict | None:
    r = db().execute("SELECT start_ts, end_ts FROM reading_timer WHERE chat_id=?", (cid,)).fetchone()
    return {"start": r[0], "end": r[1]} if r else None

def timer_start(cid: int):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO reading_timer VALUES (?,?,NULL)", (cid, time.time()))

def timer_finish(cid: int):
    with db() as c:
        c.execute("UPDATE reading_timer SET end_ts=? WHERE chat_id=?", (time.time(), cid))

def timer_reopen(cid: int):
    with db() as c:
        c.execute("UPDATE reading_timer SET end_ts=NULL WHERE chat_id=?", (cid,))

def timer_clear(cid: int):
    with db() as c:
        c.execute("DELETE FROM reading_timer WHERE chat_id=?", (cid,))

def reading_seconds(cid: int) -> float | None:
    t = timer_get(cid)
    if not t or not t["end"]:
        return None
    return max(t["end"] - t["start"], 1.0)

def reading_stats(cid: int, n_pages: int) -> dict | None:
    sec = reading_seconds(cid)
    if sec is None:
        return None
    return {"minutes": round(sec / 60), "pph": round(n_pages / (sec / 3600))}

def min_minutes_needed(n_pages: int) -> int:
    return math.ceil(n_pages / MAX_PAGES_PER_HOUR * 60)

def pending_save(cid, book, pages, moments, qpool, book_text="", source=""):
    with db() as c:
        c.execute(
            """INSERT OR REPLACE INTO pending
               (chat_id, book, pages, moments, qpool, used, attempt, book_text, source)
               VALUES (?,?,?,?,?,'[]',1,?,?)""",
            (cid, book, pages, json.dumps(moments, ensure_ascii=False),
             json.dumps(qpool, ensure_ascii=False), book_text, source))

def pending_get(cid: int) -> dict | None:
    r = db().execute(
        "SELECT book, pages, moments, qpool, used, attempt, book_text, source FROM pending WHERE chat_id=?",
        (cid,)).fetchone()
    if not r:
        return None
    return {"book": r[0], "pages": r[1], "moments": json.loads(r[2] or "[]"),
            "qpool": json.loads(r[3] or "[]"), "used": json.loads(r[4] or "[]"),
            "attempt": r[5] or 1, "book_text": r[6] or "", "source": r[7] or ""}

def pending_update(cid: int, used=None, attempt=None):
    with db() as c:
        if used is not None:
            c.execute("UPDATE pending SET used=? WHERE chat_id=?", (json.dumps(used), cid))
        if attempt is not None:
            c.execute("UPDATE pending SET attempt=? WHERE chat_id=?", (attempt, cid))

def pending_clear(cid: int):
    with db() as c:
        c.execute("DELETE FROM pending WHERE chat_id=?", (cid,))


# ══════════════════════════════════════════════════════════════════
#  АНАЛИЗ СТРАНИЦ: ключевые моменты + пул вопросов по деталям
# ══════════════════════════════════════════════════════════════════

def analyze_pages(photos: list[str], book_text: str, pages: str, title: str) -> dict:
    """Один вызов Claude: читает страницы (фото или текст) и возвращает
    ключевые моменты сюжета + пул из 6 вопросов по конкретным деталям."""
    n = count_pages(pages)
    if photos:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
            for b in photos
        ]
        src = f"На фото — страницы {pages} книги «{title}» (~{n} стр.)."
    else:
        content = []
        src = f"Ниже текст страниц {pages} книги «{title}» (~{n} стр.).\n\nТЕКСТ:\n\"\"\"\n{book_text[:12000]}\n\"\"\""

    rules = (
        "Ты опытный учитель литературы. Прочитай текст (книга может быть на любом языке).\n\n"
        "1) Выдели 5-8 КЛЮЧЕВЫХ моментов сюжета, равномерно по всему диапазону страниц. "
        "Каждый момент формулируй с конкретной деталью (кто, что, где, с каким предметом или животным) — "
        "по ней потом будут проверять внимательность. required=true для 3-4 важнейших.\n"
        "2) Составь 12 вопросов по КОНКРЕТНЫМ деталям: имя, число, предмет, кто что сказал или сделал, "
        "порядок событий. По 4 вопроса из начала, середины и конца диапазона (поле part). "
        "Вопрос должен требовать знания именно этого текста: по краткому пересказу сюжета на него "
        "ответить нельзя. Ответ — одно предложение, без «да/нет». Вопросы пиши по-русски.\n\n"
        "Ответь ТОЛЬКО чистым JSON без markdown:\n"
        '{"key_moments": [{"moment": "...", "required": true}],'
        ' "questions": [{"question": "...", "expected": "правильный ответ", "part": "начало"}]}'
    )
    content.append({"type": "text", "text": src + "\n\n" + rules})
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=6000,
        messages=[{"role": "user", "content": content}])
    data = _parse_json(resp.content[0].text)

    moments = [m for m in data.get("key_moments", []) if isinstance(m, dict) and m.get("moment")]
    qpool = [q for q in data.get("questions", [])
             if isinstance(q, dict) and q.get("question") and q.get("expected")]
    if moments and not any(m.get("required") for m in moments):
        for m in moments[:2]:
            m["required"] = True
    return {"moments": moments, "qpool": qpool}


def pick_questions(qpool: list[dict], used: list[int]) -> tuple[list[int], list[int]]:
    """Выбирает QA_COUNT ещё не задававшихся вопросов, по возможности из разных частей текста."""
    all_idx = list(range(len(qpool)))
    unused = [i for i in all_idx if i not in used]
    if len(unused) < QA_COUNT:
        used, unused = [], all_idx[:]
    random.shuffle(unused)
    picked, parts = [], set()
    for i in unused:
        part = qpool[i].get("part", "")
        if part not in parts:
            picked.append(i)
            parts.add(part)
        if len(picked) == QA_COUNT:
            break
    for i in unused:
        if len(picked) >= QA_COUNT:
            break
        if i not in picked:
            picked.append(i)
    picked = picked[:QA_COUNT]
    return picked, used + picked


def make_probes(missed: list[str], title: str) -> list[dict]:
    """Для каждого пропущенного в пересказе события Claude формулирует наводящий вопрос
    в двух вариантах: с верной деталью и с правдоподобной неверной."""
    items = "\n".join(f"{i}. {m}" for i, m in enumerate(missed, 1))
    prompt = (
        f"Книга «{title}». Ребёнок (13 лет) не упомянул в пересказе эти события:\n{items}\n\n"
        "Для КАЖДОГО события составь наводящий вопрос в форме «Ты не забыл упомянуть ...?» в двух вариантах:\n"
        "- q_true: вопрос с ВЕРНОЙ деталью, как было на самом деле;\n"
        "- q_false: тот же вопрос, но с одной правдоподобной НЕВЕРНОЙ деталью "
        "(подменён предмет, животное, место, число или имя), противоречащей событию;\n"
        "- truth: что на самом деле происходило (1 предложение).\n"
        "Пример. Событие: «Герой подружился с белым голубем». "
        "q_true: «Ты не забыл упомянуть историю с белым голубем?» "
        "q_false: «Ты не забыл упомянуть историю с белым гусем?» "
        "truth: «Герой подружился с белым голубем».\n"
        "Обе формулировки должны звучать одинаково естественно. Пиши по-русски.\n\n"
        "Ответь ТОЛЬКО чистым JSON без markdown, объекты по порядку событий:\n"
        '{"probes": [{"q_true": "...", "q_false": "...", "truth": "..."}]}'
    )
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}])
    data = _parse_json(resp.content[0].text)
    return [p for p in data.get("probes", [])
            if isinstance(p, dict) and p.get("q_true") and p.get("q_false") and p.get("truth")]


def build_probe_items(missed: list[str], title: str) -> list[dict]:
    """Наводящие вопросы: случайно верная или ложная деталь, при 2+ вопросах гарантированно есть оба вида."""
    if not missed:
        return []
    probes = make_probes(missed, title)
    items = []
    for p in probes:
        is_true = random.random() < 0.5
        items.append({"kind": "probe", "premise_true": is_true,
                      "q_true": p["q_true"], "q_false": p["q_false"],
                      "question": p["q_true"] if is_true else p["q_false"],
                      "expected": p["truth"]})
    if len(items) >= 2 and len({it["premise_true"] for it in items}) == 1:
        it = random.choice(items)
        it["premise_true"] = not it["premise_true"]
        it["question"] = it["q_true"] if it["premise_true"] else it["q_false"]
    return items


def check_detail_answers(qa: list[dict], answers: list[dict]) -> list[dict]:
    """Claude сверяет голосовые ответы ребёнка: вопросы по деталям и наводящие вопросы."""
    lines = []
    for i, (q, a) in enumerate(zip(qa, answers), 1):
        ans = a.get("text") or "(пусто)"
        if q.get("kind") == "probe" and q.get("premise_true"):
            lines.append(
                f"{i}. НАВОДЯЩИЙ ВОПРОС (в вопросе ВЕРНАЯ деталь): {q['question']}\n"
                f"   ЧТО БЫЛО НА САМОМ ДЕЛЕ: {q['expected']}\n"
                f"   ОТВЕТ РЕБЁНКА: {ans}\n"
                "   Засчитывай, если ребёнок подтвердил И верно рассказал суть события. "
                "Голое «да», «не помню» или пересказ с ошибками в сути — неверно.")
        elif q.get("kind") == "probe":
            lines.append(
                f"{i}. НАВОДЯЩИЙ ВОПРОС (в вопросе НЕВЕРНАЯ деталь): {q['question']}\n"
                f"   ЧТО БЫЛО НА САМОМ ДЕЛЕ: {q['expected']}\n"
                f"   ОТВЕТ РЕБЁНКА: {ans}\n"
                "   Засчитывай, если ребёнок заметил подмену и поправил её. "
                "Если он согласился с неверной деталью, «вспомнил» несуществующее или не заметил подмену — неверно.")
        else:
            lines.append(
                f"{i}. ВОПРОС: {q['question']}\n"
                f"   ПРАВИЛЬНЫЙ ОТВЕТ: {q['expected']}\n"
                f"   ОТВЕТ РЕБЁНКА: {ans}")
    prompt = (
        "Ты справедливый учитель. Проверь голосовые ответы ребёнка (13 лет) по прочитанному тексту.\n"
        "Ответы расшифрованы из голоса автоматически: допускай искажения имён и мелкие неточности.\n"
        "Засчитывай, если ключевой факт совпадает по смыслу. Не засчитывай уклончивые и слишком общие ответы.\n\n"
        + "\n".join(lines) + "\n\n"
        "Ответь ТОЛЬКО чистым JSON без markdown, по одному объекту на пункт по порядку:\n"
        '{"results": [{"correct": true, "comment": "до 10 слов"}]}'
    )
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=600,
        messages=[{"role": "user", "content": prompt}])
    data = _parse_json(resp.content[0].text)
    raw = data.get("results", [])
    out = []
    for i in range(len(qa)):
        r = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        out.append({"correct": bool(r.get("correct")), "comment": str(r.get("comment", ""))})
    return out


# ══════════════════════════════════════════════════════════════════
#  HELPERS: сообщения и отчёт родителю
# ══════════════════════════════════════════════════════════════════

async def send_md(send, text: str, **kw):
    """Markdown с откатом в обычный текст, если Telegram не смог разобрать разметку."""
    try:
        return await send(text, parse_mode="Markdown", **kw)
    except BadRequest as e:
        logger.warning("Markdown failed (%s) — sending plain", e)
        return await send(text, **kw)

async def notify_parent(ctx, text: str):
    try:
        await ctx.bot.send_message(PARENT_CHAT_ID, text[:4000])
    except Exception as e:
        logger.error("Parent notify failed: %s", e)

def fmt_stats(stats: dict | None) -> str:
    if not stats:
        return "⏱ Время чтения: не замеряется (сдача по пересказу и вопросам)"
    return f"⏱ Читал: {stats['minutes']} мин → {stats['pph']} стр/час (лимит {MAX_PAGES_PER_HOUR})"

def clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"

def build_parent_report(*, status, name, cid, book, pages, stats, score, attempt,
                        retelling, summary="", missed_req=None, missed_opt=None,
                        qa_rows=None, notes=None) -> str:
    L = [status, "",
         f"👦 {name} (ID {cid})",
         f"📖 {book} · стр. {pages} ({count_pages(pages)} стр.)",
         fmt_stats(stats),
         f"⭐ Пересказ: {score}/100" + (f" · попытка {attempt}" if attempt > 1 else "")]
    if summary:
        L += ["", "🔍 " + clip(summary, 500)]
    if missed_req:
        L += ["", "🔴 Пропустил обязательное:"] + [f"• {clip(m, 160)}" for m in missed_req]
    if missed_opt:
        L += ["", "🟡 Пропустил детали:"] + [f"• {clip(m, 160)}" for m in missed_opt]
    def _slow(r):
        return f" ⚠️ думал {r['latency']} с" if r["latency"] > QA_SLOW_SEC else f" ({r['latency']} с)"

    probes  = [r for r in (qa_rows or []) if r.get("kind") == "probe"]
    details = [r for r in (qa_rows or []) if r.get("kind") != "probe"]
    if probes:
        L += ["", "🎯 Наводящие вопросы о пропущенном в пересказе:"]
        for i, r in enumerate(probes, 1):
            trap = "верная деталь" if r.get("premise_true") else "ЛОЖНАЯ деталь (ловушка)"
            L += [f"{i}) {'✅' if r['correct'] else '❌'} [{trap}] {clip(r['question'], 200)}",
                  f"   Ответил: {clip(r['answer'], 250)}{_slow(r)}",
                  f"   На самом деле: {clip(r['expected'], 200)}"]
    if details:
        L += ["", "❓ Вопросы по деталям:"]
        for i, r in enumerate(details, 1):
            L += [f"{i}) {'✅' if r['correct'] else '❌'} {clip(r['question'], 200)}",
                  f"   Ответил: {clip(r['answer'], 250)}{_slow(r)}",
                  f"   Верно: {clip(r['expected'], 200)}"]
    for n in (notes or []):
        L += ["", "ℹ️ " + n]
    L += ["", "🗣 Пересказ:", clip(retelling, 1200)]
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════
#  KEYBOARDS (таймер)
# ══════════════════════════════════════════════════════════════════

def kb_timer_idle():
    return ReplyKeyboardMarkup([["▶️ Начинаю читать"]], resize_keyboard=True, one_time_keyboard=True)

def kb_timer_running():
    return ReplyKeyboardMarkup([["⏹ Закончил читать"], ["🔄 Начать отсчёт заново"]],
                               resize_keyboard=True, one_time_keyboard=True)

def kb_retry_audio():
    return ReplyKeyboardMarkup([["🎙 Записать пересказ заново"]],
                               resize_keyboard=True, one_time_keyboard=True)


# ══════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════

def get_session(cid: int) -> dict | None:
    """Сессия из памяти; если бот перезапускался — восстанавливаем из БД (pending)."""
    s = sessions.get(cid)
    if s and s.get("moments"):
        return s
    p = pending_get(cid)
    if p:
        s = {"photos": [], **p, "retry_audio": True}
        sessions[cid] = s
        return s
    return s

async def prepare_check(cid: int, photos: list[str], book_text: str):
    """Анализирует страницы, сохраняет моменты и пул вопросов в БД и в сессию."""
    s = sessions[cid]
    data = await asyncio.to_thread(analyze_pages, photos, book_text, s["pages"], s["book"])
    if not data["moments"]:
        raise ValueError("no key moments extracted")
    pending_save(cid, s["book"], s["pages"], data["moments"], data["qpool"],
                 book_text, s.get("source", ""))
    s.update({"moments": data["moments"], "qpool": data["qpool"], "used": [], "attempt": 1})


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid  = update.effective_chat.id
    name = update.effective_user.first_name or "друг"

    # Ребёнок бросил вопросы по деталям посередине → считаем неудачной попыткой
    old = sessions.get(cid)
    if old and old.get("qa") and len(old.get("qa_answers", [])) < len(old["qa"]):
        att = old.get("attempt", 1)
        pending_update(cid, attempt=att + 1)
        await notify_parent(ctx, f"⚠️ {name} (ID {cid}) начал вопросы по деталям и бросил их "
                                 f"(книга «{old.get('book')}», стр. {old.get('pages')}). Засчитано как неудачная попытка {att}.")
    sessions.pop(cid, None)

    # Есть незачтённая сдача → можно только записать пересказ
    pend = pending_get(cid)
    if pend:
        sessions[cid] = {"photos": [], **pend, "retry_audio": True}
        await send_md(update.message.reply_text,
            f"⚠️ {name}, ты ещё не сдал пересказ!\n\n"
            f"📖 Книга: *«{pend['book']}»*, стр. *{pend['pages']}*\n\n"
            "Пока не сдашь — новое задание начать нельзя.\n"
            "Нажми кнопку и запиши голосовое сообщение 👇",
            reply_markup=kb_retry_audio())
        return WAIT_AUDIO

    return await begin_submission(update, ctx)


async def handle_timer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid  = update.effective_chat.id
    text = update.message.text or ""

    if "Начинаю читать" in text or "заново" in text:
        timer_start(cid)
        await update.message.reply_text(
            "▶️ Время пошло! Читай.\nКогда закончишь — напиши /start и нажми «⏹ Закончил читать».",
            reply_markup=kb_none())
        return ConversationHandler.END

    if "Закончил читать" in text:
        t = timer_get(cid)
        if not t or t["end"]:
            await update.message.reply_text("Сначала нажми «▶️ Начинаю читать».", reply_markup=kb_timer_idle())
            return WAIT_TIMER
        timer_finish(cid)
        return await begin_submission(update, ctx)

    return WAIT_TIMER


async def begin_submission(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор книги после того как таймер остановлен."""
    cid  = update.effective_chat.id
    name = update.effective_user.first_name or "друг"
    sessions[cid] = {"photos": []}

    secs = reading_seconds(cid)
    time_line = f"⏱ Время чтения: {round(secs / 60)} мин\n\n" if secs else ""

    last = get_last(cid)
    if last:
        hist = get_history(cid)
        done = [h["pages"] for h in hist if h["book"] == last["book"].lower().strip() and h["passed"]]
        await send_md(update.message.reply_text,
            f"{time_line}📚 {name}, в прошлый раз: *«{last['book']}»*, стр. *{last['pages']}*\n"
            f"Уже сданы: {', '.join(done) if done else 'нет'}\n\n"
            "Продолжаем или другая книга?",
            reply_markup=kb_continue(last["book"]))
        sessions[cid]["last"] = last
        return WAIT_CONTINUE
    await send_md(update.message.reply_text,
        f"{time_line}Напиши *название книги* которую читал:", reply_markup=kb_cancel())
    return WAIT_BOOK_TITLE


async def handle_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid = update.effective_chat.id
    if "Продолжаю" in update.message.text:
        last = sessions.get(cid, {}).get("last", {})
        if not last:
            await update.message.reply_text("Напиши /start")
            return ConversationHandler.END
        sessions[cid] = {
            "photos": [], "book": last["book"],
            "book_text": last.get("book_text", ""), "source": last.get("source", ""),
            "skip_search": True,
        }
        await send_md(update.message.reply_text,
            f"📖 *«{last['book']}»*\n\nНапиши *страницы* которые читал сегодня:",
            reply_markup=kb_cancel())
        return WAIT_PAGES
    sessions[cid] = {"photos": []}
    await send_md(update.message.reply_text, "Напиши *название новой книги*:", reply_markup=kb_cancel())
    return WAIT_BOOK_TITLE


async def recv_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "❌" in update.message.text: return await cancel(update, ctx)
    cid = update.effective_chat.id
    sessions[cid] = {"photos": [], "book": update.message.text.strip(), "book_text": "", "source": ""}
    await send_md(update.message.reply_text,
        f"📖 *«{sessions[cid]['book']}»*\n\nНапиши *страницы* которые читал:\n_Например: 10-70_",
        reply_markup=kb_cancel())
    return WAIT_PAGES


async def recv_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "❌" in update.message.text: return await cancel(update, ctx)
    cid   = update.effective_chat.id
    pages = update.message.text.strip()
    name  = update.effective_user.first_name or "Ребёнок"

    if cid not in sessions:
        await update.message.reply_text("Напиши /start")
        return ConversationHandler.END
    if not re.match(r"^\d+[\-–]\d+$|^\d+$", pages):
        await send_md(update.message.reply_text, "❗ Формат: *5-10* или *12*")
        return WAIT_PAGES

    book = sessions[cid].get("book", "")
    if already_passed(cid, book, pages):
        await send_md(update.message.reply_text,
            f"🚫 Страницы *{pages}* ты уже сдавал!\nНапиши *другие страницы*.")
        return WAIT_PAGES

    n_pages = count_pages(pages)
    if n_pages < MIN_PAGES_PER_SESSION:
        await send_md(update.message.reply_text,
            f"📏 Маловато! Минимум *{MIN_PAGES_PER_SESSION} страниц* за одну сдачу.\n"
            f"Ты указал только *{n_pages}* стр.\n\nНапиши больший диапазон, например *10-40*")
        return WAIT_PAGES

    sessions[cid]["pages"] = pages

    # ── Текст онлайн (по умолчанию выключен: ALLOW_ONLINE_TEXT=0) ──
    book_text = ""
    if ALLOW_ONLINE_TEXT:
        if sessions[cid].get("skip_search"):
            book_text = sessions[cid].get("book_text", "")
        else:
            await update.message.reply_text("🔍 Ищу книгу онлайн...", reply_markup=kb_none())
            try:
                result = await asyncio.to_thread(search_book_text, book, pages)
            except Exception as e:
                logger.error("Search failed: %s", e)
                result = {"found": False, "text": "", "source": ""}
            if result["found"]:
                book_text = result["text"]
                sessions[cid]["source"] = result["source"]
    if book_text:
        sessions[cid]["book_text"] = book_text
        try:
            await prepare_check(cid, [], book_text)
            await update.message.reply_text(
                "✅ Текст книги найден.\n\n🎙 Запиши *голосовое сообщение* — расскажи что прочитал.".replace("*", ""),
                reply_markup=kb_none())
            return WAIT_AUDIO
        except Exception as e:
            logger.error("prepare_check(text) failed: %s", e)

    # ── Фото страниц ──
    sessions[cid].update({"book_text": "", "source": "фото", "photos": [], "photo_hashes": []})
    await update.message.reply_text(
        f"📷 Сфотографируй все страницы {pages} ({n_pages} стр.) и отправь фото сюда.\n"
        "Можно разворотами — по 2 страницы на фото, чтобы были видны номера страниц.\n"
        "Когда отправишь все — нажми кнопку 👇",
        reply_markup=kb_photo())
    return WAIT_PHOTOS


async def recv_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid = update.effective_chat.id
    if cid not in sessions:
        await update.message.reply_text("Напиши /start")
        return ConversationHandler.END
    b64, photo_hash = await dl_photo(ctx.bot, update.message.photo[-1].file_id)

    if photo_hash in get_seen_hashes(cid):
        await send_md(update.message.reply_text,
            "🚫 Это фото ты уже отправлял раньше!\nПришли *новые* фото страниц, которые читал сегодня.",
            reply_markup=kb_photo())
        return WAIT_PHOTOS
    if photo_hash in sessions[cid].get("photo_hashes", []):
        await update.message.reply_text("⚠️ Это фото уже в текущей загрузке, пропускаю.", reply_markup=kb_photo())
        return WAIT_PHOTOS

    sessions[cid]["photos"].append(b64)
    sessions[cid].setdefault("photo_hashes", []).append(photo_hash)
    n = len(sessions[cid]["photos"])
    await update.message.reply_text(f"✅ Фото {n} получено! Ещё или нажми кнопку.", reply_markup=kb_photo())
    return WAIT_PHOTOS


async def photos_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "❌" in update.message.text: return await cancel(update, ctx)
    cid     = update.effective_chat.id
    session = sessions.get(cid)
    if not session:
        await update.message.reply_text("Напиши /start")
        return ConversationHandler.END
    photos = session.get("photos", [])
    if not photos:
        await update.message.reply_text("❗ Сначала отправь хотя бы одно фото.", reply_markup=kb_photo())
        return WAIT_PHOTOS

    await update.message.reply_text("🔍 Проверяю фото... (до минуты)", reply_markup=kb_none())
    try:
        check = await asyncio.to_thread(verify_photos_with_claude, photos, session["pages"], session["book"])
    except Exception as e:
        logger.error("Photo verify error: %s", e)
        check = {"ok": True, "actual_pages": len(photos) * 2, "reason": ""}

    if not check.get("ok", True):
        session["photos"], session["photo_hashes"] = [], []
        await send_md(update.message.reply_text,
            f"❌ Фото не прошло проверку.\n\n*Причина:* {check.get('reason', '')}\n\n"
            "Пришли правильные фото страниц книги 👇", reply_markup=kb_photo())
        return WAIT_PHOTOS

    try:
        await prepare_check(cid, photos, "")
    except Exception as e:
        logger.error("prepare_check(photos) failed: %s", e, exc_info=True)
        session["photos"], session["photo_hashes"] = [], []
        await update.message.reply_text(
            "😔 Не получилось прочитать текст на фото. Пересними страницы ровно и при хорошем свете, "
            "и пришли фото заново 👇", reply_markup=kb_photo())
        return WAIT_PHOTOS

    save_photo_hashes(cid, session["book"], session["pages"], session.get("photo_hashes", []))
    session["photos"] = []        # фото больше не нужны — освобождаем память

    await update.message.reply_text(
        f"✅ Фото проверены: {len(photos)} шт., ~{check.get('actual_pages', len(photos))} стр.\n\n"
        "🎙 Теперь запиши голосовое сообщение — расскажи своими словами, что прочитал.",
        reply_markup=kb_none())
    return WAIT_AUDIO


async def ask_question(update: Update, session: dict):
    i = len(session["qa_answers"])
    q = session["qa"][i]
    session["qa_asked_at"] = time.time()
    intro = ""
    if i == 0:
        intro = (f"👍 Пересказ принят. Теперь {len(session['qa'])} вопросов по тексту.\n"
                 "Отвечай голосом и по существу: если я спрашиваю о событии — расскажи, что там было; "
                 "если мне кажется, что я что-то напутал — поправь меня.\n\n")
    await update.message.reply_text(
        f"{intro}❓ Вопрос {i + 1} из {len(session['qa'])}\n\n{q['question']}\n\n🎙 Ответь голосовым сообщением.",
        reply_markup=kb_none())


async def recv_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid     = update.effective_chat.id
    session = get_session(cid)
    if not session or not session.get("moments"):
        await update.message.reply_text("❗ Не нашёл твоё задание. Напиши /start")
        return ConversationHandler.END

    book, pages = session["book"], session["pages"]
    name = update.effective_user.first_name or "Ребёнок"

    await update.message.reply_text("⏳ Слушаю...", reply_markup=kb_none())
    try:
        voice = await dl_voice(ctx.bot, update.message.voice.file_id)
        await update.message.reply_text("🔤 Распознаю речь...")
        retelling = await asyncio.to_thread(transcribe, voice)
        logger.info("Retelling: %s", retelling)

        if len(retelling.split()) < 10:
            await update.message.reply_text(
                "🙂 Слишком коротко. Расскажи подробнее — хотя бы 20-30 секунд, что происходило в этих страницах.",
                reply_markup=kb_retry_audio())
            return WAIT_AUDIO

        await update.message.reply_text("🧠 Проверяю пересказ...")
        result = await asyncio.to_thread(
            check_retelling_against_moments, session["moments"], retelling, pages, book)

        score    = result.get("score", 0)
        missed_req = result.get("missed_required", [])
        missed_opt = result.get("missed_optional", [])
        attempt  = session.get("attempt", 1)
        stats    = reading_stats(cid, count_pages(pages))

        # Пересказ почти пустой → просим рассказать подробнее (без подсказок, что именно пропущено)
        if score < RETELL_MIN_SCORE:
            save_result(cid, book, pages, False, score)
            save_last(cid, book, pages, session.get("book_text", ""), session.get("source", ""))
            session["attempt"] = attempt + 1
            pending_update(cid, attempt=attempt + 1)
            await update.message.reply_text(
                "🤔 Пересказ получился слишком кратким или не по тексту. "
                "Расскажи подробнее своими словами, о чём были эти страницы 👇",
                reply_markup=kb_retry_audio())
            await notify_parent(ctx, build_parent_report(
                status="❌ Пересказ слишком скудный", name=name, cid=cid, book=book, pages=pages,
                stats=stats, score=score, attempt=attempt, retelling=retelling,
                summary=result.get("summary", ""), missed_req=missed_req, missed_opt=missed_opt))
            return WAIT_AUDIO

        notes = []
        # 1) Наводящие вопросы о том, что ребёнок пропустил (ребёнку список пропущенного не показываем)
        probes = []
        missed = (missed_req + missed_opt)[:PROBE_MAX]
        if missed:
            try:
                probes = await asyncio.to_thread(build_probe_items, missed, book)
            except Exception as e:
                logger.error("Probe generation failed: %s", e, exc_info=True)
                notes.append("Наводящие вопросы не сформированы (ошибка генерации).")

        # 2) Случайные вопросы по деталям из пула
        picked, used = pick_questions(session["qpool"], session.get("used", []))
        session["used"] = used
        pending_update(cid, used=used)
        details = [{**session["qpool"][i], "kind": "detail"} for i in picked]

        qa = probes + details
        random.shuffle(qa)          # ловушки перемешаны с обычными вопросами

        session.update({"retelling": retelling, "result": result, "qa": qa, "qa_answers": []})
        if not qa:
            return await finalize(update, ctx, session, qa_passed=True, qa_rows=[],
                                  notes=notes + ["Вопросов не сформировано — принято только по пересказу."])
        await ask_question(update, session)
        return WAIT_QA

    except Exception as e:
        logger.error("Audio error: %s", e, exc_info=True)
        await update.message.reply_text("😔 Ошибка. Попробуй записать пересказ ещё раз.",
                                        reply_markup=kb_retry_audio())
        return WAIT_AUDIO


async def recv_qa(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid     = update.effective_chat.id
    session = sessions.get(cid)
    if not session or not session.get("qa"):
        await update.message.reply_text("Сессия сбросилась. Напиши /start и запиши пересказ заново.")
        return ConversationHandler.END

    latency = round(time.time() - session.get("qa_asked_at", time.time()))
    try:
        voice = await dl_voice(ctx.bot, update.message.voice.file_id)
        text  = await asyncio.to_thread(transcribe, voice)
    except Exception as e:
        logger.error("QA transcribe error: %s", e)
        await update.message.reply_text("Не расслышал. Запиши ответ ещё раз 🎙")
        return WAIT_QA

    session["qa_answers"].append({"text": text, "latency": latency})
    if len(session["qa_answers"]) < len(session["qa"]):
        await ask_question(update, session)
        return WAIT_QA

    await update.message.reply_text("🧠 Проверяю ответы...")
    try:
        results = await asyncio.to_thread(check_detail_answers, session["qa"], session["qa_answers"])
    except Exception as e:
        logger.error("QA check error: %s", e, exc_info=True)
        session["qa_answers"] = []
        await update.message.reply_text("😔 Не смог проверить ответы. Давай ещё раз с первого вопроса.")
        await ask_question(update, session)
        return WAIT_QA

    rows = [{"kind": q.get("kind", "detail"), "premise_true": q.get("premise_true"),
             "question": q["question"], "expected": q["expected"],
             "answer": a["text"], "latency": a["latency"],
             "correct": r["correct"], "comment": r["comment"]}
            for q, a, r in zip(session["qa"], session["qa_answers"], results)]

    det   = [r for r in rows if r["kind"] != "probe"]
    prb   = [r for r in rows if r["kind"] == "probe"]
    det_ok = sum(r["correct"] for r in det) >= min(QA_MIN_CORRECT, len(det))
    prb_ok = sum(r["correct"] for r in prb) >= len(prb) - len(prb) // 3   # из 3 можно ошибиться в 1
    return await finalize(update, ctx, session, qa_passed=(det_ok and prb_ok), qa_rows=rows)


async def finalize(update: Update, ctx: ContextTypes.DEFAULT_TYPE, session: dict,
                   qa_passed: bool, qa_rows: list, notes: list | None = None) -> int:
    cid  = update.effective_chat.id
    name = update.effective_user.first_name or "Ребёнок"
    book, pages = session["book"], session["pages"]
    result    = session["result"]
    retelling = session["retelling"]
    score     = result.get("score", 0)
    attempt   = session.get("attempt", 1)
    stats     = reading_stats(cid, count_pages(pages))
    correct   = sum(1 for r in qa_rows if r["correct"])

    save_result(cid, book, pages, qa_passed, score)
    save_last(cid, book, pages, session.get("book_text", ""), session.get("source", ""))

    if qa_passed:
        pending_clear(cid)
        timer_clear(cid)
        sessions.pop(cid, None)
        await update.message.reply_text(
            f"🎉 Принято! Молодец!\n\n💬 {result.get('feedback', '')}\n\n⭐ {score}/100",
            reply_markup=kb_none())
        status = "✅ ПРИНЯТО"
    else:
        session["attempt"] = attempt + 1
        pending_update(cid, attempt=attempt + 1)
        for k in ("qa", "qa_answers", "retelling", "result"):
            session.pop(k, None)
        await update.message.reply_text(
            f"🤔 Не всё сошлось: верных ответов {correct} из {len(qa_rows)}.\n"
            "Перечитай страницы внимательнее и запиши пересказ заново 👇",
            reply_markup=kb_retry_audio())
        status = f"❌ НЕ ПРИНЯТО — верных ответов {correct} из {len(qa_rows)}"

    await notify_parent(ctx, build_parent_report(
        status=status, name=name, cid=cid, book=book, pages=pages, stats=stats,
        score=score, attempt=attempt, retelling=retelling, summary=result.get("summary", ""),
        missed_req=result.get("missed_required", []), missed_opt=result.get("missed_optional", []),
        qa_rows=qa_rows, notes=notes))
    return ConversationHandler.END if qa_passed else WAIT_AUDIO


async def audio_hint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🎙 Жду голосовое сообщение. Нажми на микрофон в Telegram и говори.")
    return WAIT_QA if sessions.get(update.effective_chat.id, {}).get("qa") else WAIT_AUDIO


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 Истории пока нет.")
        return
    lines = ["📊 История чтения:\n"]
    for r in rows:
        lines.append(f"{'✅' if r['passed'] else '❌'} {r['book']} стр. {r['pages']} — {r['score']}/100")
    await update.message.reply_text("\n".join(lines))


async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Только для родителя: /unlock <ID ребёнка> — снимает блокировку незачтённой сдачи и таймер."""
    if update.effective_chat.id != PARENT_CHAT_ID:
        return
    args = ctx.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /unlock <ID ребёнка>\nID указан в каждом отчёте.")
        return
    child = int(args[0])
    pending_clear(child)
    timer_clear(child)
    sessions.pop(child, None)
    await update.message.reply_text(f"🔓 Разблокировано (ID {child}). Ребёнок может начать заново через /start.")


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("❌ Отменено. Напиши /start.", reply_markup=kb_none())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    text_only = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_TIMER:      [MessageHandler(text_only, handle_timer)],
            WAIT_CONTINUE:   [MessageHandler(text_only, handle_continue)],
            WAIT_BOOK_TITLE: [MessageHandler(text_only, recv_title)],
            WAIT_PAGES:      [MessageHandler(text_only, recv_pages)],
            WAIT_PHOTOS: [
                MessageHandler(filters.PHOTO, recv_photo),
                MessageHandler(text_only, photos_done),
            ],
            WAIT_AUDIO: [
                MessageHandler(filters.VOICE, recv_audio),
                MessageHandler(text_only, audio_hint),
            ],
            WAIT_QA: [
                MessageHandler(filters.VOICE, recv_qa),
                MessageHandler(text_only, audio_hint),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),   # /start всегда сбрасывает любое состояние
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("unlock", cmd_unlock))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",   "▶️ Начать / сдать чтение"),
            BotCommand("history", "📊 История чтения"),
            BotCommand("cancel",  "❌ Отменить"),
        ])
    app.post_init = post_init

    logger.info("Bot v12 started. MAX_PAGES_PER_HOUR=%s QA=%s/%s ALLOW_ONLINE_TEXT=%s",
                MAX_PAGES_PER_HOUR, QA_MIN_CORRECT, QA_COUNT, ALLOW_ONLINE_TEXT)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
