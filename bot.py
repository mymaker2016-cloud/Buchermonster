import os
import re
import json
import sqlite3
import logging
import base64
import tempfile
from pathlib import Path

import httpx
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
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

(WAIT_CONTINUE, WAIT_BOOK_TITLE, WAIT_PAGES, WAIT_PHOTOS, WAIT_AUDIO) = range(5)
sessions: dict[int, dict] = {}

# ══════════════════════════════════════════════════════════════════
#  DB
# ══════════════════════════════════════════════════════════════════

DB_PATH = "/app/data/reading.db"

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS progress (
        chat_id INTEGER, book TEXT, pages TEXT, passed INTEGER, score INTEGER,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, book, pages))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS last_book (
        chat_id INTEGER PRIMARY KEY, book TEXT, pages TEXT)""")
    conn.commit()
    return conn

def get_last(chat_id):
    r = db().execute("SELECT book, pages FROM last_book WHERE chat_id=?", (chat_id,)).fetchone()
    return {"book": r[0], "pages": r[1]} if r else None

def save_last(chat_id, book, pages):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO last_book VALUES (?,?,?)", (chat_id, book, pages))

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

async def dl_photo(bot, file_id) -> str:
    f = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as t:
        await f.download_to_drive(t.name); p = t.name
    with open(p, "rb") as fh: data = base64.b64encode(fh.read()).decode()
    Path(p).unlink(missing_ok=True)
    return data

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

def check_by_text(book_text: str, retelling: str, pages: str, title: str) -> dict:
    """Проверка пересказа по тексту книги (найден онлайн)."""
    prompt = f"""Ты добрый учитель начальной школы.

КНИГА: «{title}», страницы {pages}
ТЕКСТ КНИГИ:
\"\"\"{book_text[:3500]}\"\"\"

ПЕРЕСКАЗ РЕБЁНКА:
\"\"\"{retelling}\"\"\"

Оцени: главные события, смысл, детали.
Ответь ТОЛЬКО чистым JSON без markdown:
{{"passed": true/false, "score": 0-100, "feedback": "для ребёнка — дружелюбно", "summary": "для родителя — 2-3 предложения"}}"""
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return _parse_json(resp.content[0].text)

def check_by_photos(photos: list[str], retelling: str, pages: str, title: str) -> dict:
    """OCR + проверка пересказа по фото страниц — один вызов Claude."""
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
        for b in photos
    ]
    content.append({"type": "text", "text": f"""
Это фото страниц {pages} книги «{title}».

1. Прочитай текст с фото.
2. Сравни с пересказом ребёнка.
3. Оцени понимание.

ПЕРЕСКАЗ РЕБЁНКА:
\"\"\"{retelling}\"\"\"

Ответь ТОЛЬКО чистым JSON без markdown:
{{"passed": true/false, "score": 0-100, "feedback": "для ребёнка — дружелюбно", "summary": "для родителя — 2-3 предложения"}}
"""})
    resp = claude.messages.create(
        model="claude-opus-4-5", max_tokens=1024,
        messages=[{"role": "user", "content": content}]
    )
    return _parse_json(resp.content[0].text)

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
#  HANDLERS
# ══════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid  = update.effective_chat.id
    name = update.effective_user.first_name or "друг"
    sessions[cid] = {"photos": []}
    last = get_last(cid)
    if last:
        hist = get_history(cid)
        done = [h["pages"] for h in hist if h["book"] == last["book"].lower().strip() and h["passed"]]
        await update.message.reply_text(
            f"📚 Привет, {name}!\n\n"
            f"В прошлый раз: *«{last['book']}»*, стр. *{last['pages']}*\n"
            f"Уже сданы: {', '.join(done) if done else 'нет'}\n\n"
            "Продолжаем или другая книга?",
            parse_mode="Markdown", reply_markup=kb_continue(last["book"]))
        sessions[cid]["last"] = last
        return WAIT_CONTINUE
    await update.message.reply_text(
        f"📚 Привет, {name}!\n\nНапиши *название книги* которую читаешь:",
        parse_mode="Markdown", reply_markup=kb_cancel())
    return WAIT_BOOK_TITLE

async def handle_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid = update.effective_chat.id
    if "Продолжаю" in update.message.text:
        last = sessions[cid].get("last", {})
        sessions[cid] = {"photos": [], "book": last["book"], "book_text": "", "source": ""}
        await update.message.reply_text(
            f"📖 *«{last['book']}»*\n\nНапиши *страницы* которые читал сегодня:",
            parse_mode="Markdown", reply_markup=kb_cancel())
        return WAIT_PAGES
    sessions[cid] = {"photos": []}
    await update.message.reply_text("Напиши *название новой книги*:", parse_mode="Markdown", reply_markup=kb_cancel())
    return WAIT_BOOK_TITLE

async def recv_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "❌" in update.message.text: return await cancel(update, ctx)
    cid = update.effective_chat.id
    sessions[cid] = {"photos": [], "book": update.message.text.strip(), "book_text": "", "source": ""}
    await update.message.reply_text(
        f"📖 *«{sessions[cid]['book']}»*\n\nНапиши *страницы* которые читал:\n_Например: 5-10 или 12_",
        parse_mode="Markdown", reply_markup=kb_cancel())
    return WAIT_PAGES

async def recv_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "❌" in update.message.text: return await cancel(update, ctx)
    cid   = update.effective_chat.id
    pages = update.message.text.strip()
    if not re.match(r"^\d+[\-–]\d+$|^\d+$", pages):
        await update.message.reply_text("❗ Формат: *5-10* или *12*", parse_mode="Markdown")
        return WAIT_PAGES
    book = sessions[cid].get("book", "")
    if already_passed(cid, book, pages):
        await update.message.reply_text(
            f"🚫 Страницы *{pages}* ты уже сдавал!\nНапиши *другие страницы*.",
            parse_mode="Markdown")
        return WAIT_PAGES
    sessions[cid]["pages"] = pages

    # ── Поиск книги онлайн ───────────────────────────────────────
    await update.message.reply_text(
        "🔍 Ищу книгу онлайн...", reply_markup=kb_none())
    try:
        result = search_book_text(book, pages)
    except Exception as e:
        logger.error("Search failed: %s", e)
        result = {"found": False, "text": "", "source": ""}

    if result["found"]:
        sessions[cid]["book_text"] = result["text"]
        sessions[cid]["source"]    = result["source"]
        await update.message.reply_text(
            f"✅ Нашёл книгу ({result['source']})!\n\n"
            "🎙 Запиши *голосовое сообщение* — расскажи своими словами что прочитал на этих страницах.",
            parse_mode="Markdown")
        return WAIT_AUDIO
    else:
        sessions[cid]["book_text"] = ""
        sessions[cid]["source"]    = "фото"
        sessions[cid]["photos"]    = []
        await update.message.reply_text(
            "😕 Не нашёл книгу в интернете.\n\n"
            "📷 Сфотографируй страницы книги которые ты читал и отправь фото сюда.\n"
            "Можно несколько фото подряд.\n"
            "Когда отправишь все — нажми кнопку 👇")
        await update.message.reply_text("Отправляй фото:", reply_markup=kb_photo())
        return WAIT_PHOTOS

async def recv_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid = update.effective_chat.id
    b64 = await dl_photo(ctx.bot, update.message.photo[-1].file_id)
    sessions[cid]["photos"].append(b64)
    n = len(sessions[cid]["photos"])
    await update.message.reply_text(f"✅ Фото {n} получено! Ещё или нажми кнопку.", reply_markup=kb_photo())
    return WAIT_PHOTOS

async def photos_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "❌" in update.message.text: return await cancel(update, ctx)
    cid = update.effective_chat.id
    if not sessions.get(cid, {}).get("photos"):
        await update.message.reply_text("❗ Сначала отправь хотя бы одно фото.", reply_markup=kb_photo())
        return WAIT_PHOTOS
    n = len(sessions[cid]["photos"])
    await update.message.reply_text(
        f"✅ Получено фото: {n} шт.\n\n"
        "🎙 Теперь запиши *голосовое сообщение* — расскажи своими словами что прочитал.",
        parse_mode="Markdown", reply_markup=kb_none())
    return WAIT_AUDIO

async def recv_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cid     = update.effective_chat.id
    session = sessions.get(cid, {})
    has_text   = bool(session.get("book_text"))
    has_photos = bool(session.get("photos"))

    if not has_text and not has_photos:
        await update.message.reply_text("❗ Нет текста или фото. Напиши /start")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Слушаю...", reply_markup=kb_none())
    try:
        voice = await dl_voice(ctx.bot, update.message.voice.file_id)
        await update.message.reply_text("🔤 Распознаю речь...")
        retelling = transcribe(voice)
        logger.info("Retelling: %s", retelling)

        book  = session["book"]
        pages = session["pages"]

        await update.message.reply_text("🧠 Проверяю понимание...")
        if has_text:
            # Книга найдена онлайн — сравниваем с текстом
            result = check_by_text(session["book_text"], retelling, pages, book)
        else:
            # Книга не найдена — OCR фото + сравнение (один вызов Claude)
            result = check_by_photos(session["photos"], retelling, pages, book)

        passed   = result.get("passed", False)
        score    = result.get("score", 0)
        feedback = result.get("feedback", "")
        summary  = result.get("summary", "")

        save_result(cid, book, pages, passed, score)
        save_last(cid, book, pages)

        if passed:
            child_msg = f"🎉 *Отлично! Молодец!*\n\n💬 {feedback}\n\n⭐ *{score}/100*"
        else:
            child_msg = (f"🤔 *Почти! Попробуй ещё раз.*\n\n💬 {feedback}\n\n"
                         f"⭐ *{score}/100*\n\nНапиши /start чтобы попробовать снова.")
        await update.message.reply_text(child_msg, parse_mode="Markdown")

        name  = update.effective_user.first_name or "Ребёнок"
        emoji = "✅" if passed else "❌"
        source_note = f"(источник: {session.get('source', '?')})"
        await ctx.bot.send_message(
            chat_id=PARENT_CHAT_ID,
            text=(f"{emoji} *Отчёт о чтении* {source_note}\n\n"
                  f"👦 {name}\n📖 _{book}_\n📄 Стр. {pages}\n⭐ *{score}/100*\n\n"
                  f"🗣 *Пересказ:*\n_{retelling}_\n\n🔍 *Анализ:*\n{summary}"),
            parse_mode="Markdown")
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        await update.message.reply_text("😔 Ошибка. Напиши /start и попробуй снова.")
    finally:
        sessions.pop(cid, None)
    return ConversationHandler.END

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 Истории пока нет."); return
    lines = ["📊 *История чтения:*\n"]
    for r in rows:
        lines.append(f"{'✅' if r['passed'] else '❌'} _{r['book']}_ стр. {r['pages']} — {r['score']}/100")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("❌ Отменено. Напиши /start.", reply_markup=kb_none())
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_CONTINUE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_continue)],
            WAIT_BOOK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_title)],
            WAIT_PAGES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_pages)],
            WAIT_PHOTOS: [
                MessageHandler(filters.PHOTO, recv_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, photos_done),
            ],
            WAIT_AUDIO: [MessageHandler(filters.VOICE, recv_audio)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("history", cmd_history))
    logger.info("Bot v6 started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
