import os
import re
import json
import sqlite3
import logging
import base64
import tempfile
import urllib.parse
from pathlib import Path

import httpx
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
)
import anthropic
from groq import Groq

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Env ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
GROQ_KEY       = os.environ["GROQ_API_KEY"]
PARENT_CHAT_ID = int(os.environ["PARENT_CHAT_ID"])

# ─── Clients ────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
groq   = Groq(api_key=GROQ_KEY)

# ─── States ─────────────────────────────────────────────────────────────────
(WAIT_CONTINUE, WAIT_BOOK_TITLE, WAIT_PAGES, WAIT_PHOTOS, WAIT_AUDIO) = range(5)

# ─── Sessions ────────────────────────────────────────────────────────────────
sessions: dict[int, dict] = {}

# ════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════════════════

DB_PATH = "/app/reading_progress.db"

def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            chat_id     INTEGER NOT NULL,
            book_title  TEXT NOT NULL,
            pages       TEXT NOT NULL,
            passed      INTEGER NOT NULL DEFAULT 0,
            score       INTEGER NOT NULL DEFAULT 0,
            ts          DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, book_title, pages)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS last_book (
            chat_id    INTEGER PRIMARY KEY,
            book_title TEXT NOT NULL,
            last_pages TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def db_get_last_book(chat_id: int) -> dict | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT book_title, last_pages FROM last_book WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return {"title": row[0], "pages": row[1]} if row else None


def db_save_last_book(chat_id: int, title: str, pages: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO last_book (chat_id, book_title, last_pages) VALUES (?,?,?)",
            (chat_id, title, pages)
        )
        conn.commit()


def db_pages_already_done(chat_id: int, title: str, pages: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT passed FROM progress WHERE chat_id=? AND book_title=? AND pages=?",
            (chat_id, title.lower().strip(), pages.strip())
        ).fetchone()
    return bool(row and row[0] == 1)


def db_save_result(chat_id: int, title: str, pages: str, passed: bool, score: int):
    with db_connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO progress (chat_id, book_title, pages, passed, score)
               VALUES (?,?,?,?,?)""",
            (chat_id, title.lower().strip(), pages.strip(), 1 if passed else 0, score)
        )
        conn.commit()


def db_get_history(chat_id: int) -> list[dict]:
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT book_title, pages, passed, score, ts
               FROM progress WHERE chat_id=? ORDER BY ts DESC LIMIT 10""",
            (chat_id,)
        ).fetchall()
    return [{"title": r[0], "pages": r[1], "passed": r[2], "score": r[3], "ts": r[4]} for r in rows]


# ════════════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ════════════════════════════════════════════════════════════════════════════

def kb_continue(title: str):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(f'📖 Продолжаю «{title[:30]}»'), KeyboardButton("📚 Другая книга")]],
        resize_keyboard=True, one_time_keyboard=True,
    )

def kb_send_photo():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📷 Все фото отправил")]],
        resize_keyboard=True, one_time_keyboard=True,
    )

def kb_cancel():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Отмена")]],
        resize_keyboard=True, one_time_keyboard=True,
    )

def kb_remove():
    return ReplyKeyboardRemove()


# ════════════════════════════════════════════════════════════════════════════
#  BOOK SEARCH  (без web_search — прямые запросы к библиотекам)
# ════════════════════════════════════════════════════════════════════════════

SEARCH_TIMEOUT = 10  # секунд на каждый запрос

def _fetch(url: str) -> str:
    """Загрузить страницу, вернуть текст или ''."""
    try:
        r = httpx.get(url, timeout=SEARCH_TIMEOUT,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; ReadingBot/1.0)"})
        if r.status_code == 200:
            return r.text[:8000]
    except Exception as e:
        logger.warning("fetch %s: %s", url, e)
    return ""


def search_book_online(title: str, pages: str) -> dict:
    """
    Пытается найти текст книги через Wikisource и Google Books.
    Возвращает {"found": bool, "text": str, "source": str}
    """
    encoded = urllib.parse.quote(title)

    # 1. Wikisource (русский)
    ws_url = f"https://ru.wikisource.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
    html = _fetch(ws_url)
    if html and len(html) > 500:
        # Извлечь чистый текст через Claude (быстро — без поиска)
        text = _extract_text_from_html(html, title, pages, "Wikisource")
        if text:
            return {"found": True, "text": text, "source": "Wikisource"}

    # 2. Lib.ru поиск
    lib_url = f"http://lib.ru/cgi-bin/seek_doc.cgi?term={encoded}&sort=1"
    html = _fetch(lib_url)
    if html and "href" in html:
        # Найти первую ссылку на текст
        match = re.search(r'href="(/\w+/\w+\.txt)"', html)
        if match:
            txt_url = "http://lib.ru" + match.group(1)
            content = _fetch(txt_url)
            if content and len(content) > 300:
                text = _extract_text_from_html(content, title, pages, "Lib.ru")
                if text:
                    return {"found": True, "text": text, "source": "Lib.ru"}

    return {"found": False, "text": "", "source": ""}


def _extract_text_from_html(html: str, title: str, pages: str, source: str) -> str:
    """Claude извлекает нужные страницы из HTML/текста (быстро, без поиска)."""
    prompt = (
        f"Из текста ниже извлеки содержимое страниц {pages} книги «{title}».\n"
        f"Если страниц нет или текст не соответствует книге — ответь словом: НЕТ\n"
        f"Иначе — верни только текст страниц без пояснений.\n\n"
        f"ТЕКСТ:\n{html[:4000]}"
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.content[0].text.strip()
        if result == "НЕТ" or len(result) < 50:
            return ""
        return result
    except Exception as e:
        logger.error("extract_text error: %s", e)
        return ""


# ════════════════════════════════════════════════════════════════════════════
#  OTHER HELPERS
# ════════════════════════════════════════════════════════════════════════════

async def download_photo_b64(bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        path = tmp.name
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    Path(path).unlink(missing_ok=True)
    return data


async def download_voice(bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        path = tmp.name
    with open(path, "rb") as f:
        data = f.read()
    Path(path).unlink(missing_ok=True)
    return data


def transcribe(audio_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_bytes)
        path = tmp.name
    try:
        with open(path, "rb") as f:
            result = groq.audio.transcriptions.create(
                file=("audio.ogg", f, "audio/ogg"),
                model="whisper-large-v3", language="ru", response_format="text",
            )
        return result.strip()
    finally:
        Path(path).unlink(missing_ok=True)


def ocr_images(photos_b64: list[str], pages: str) -> str:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
        for b in photos_b64
    ]
    content.append({"type": "text", "text":
        f"Это фото страниц {pages} книги. Перепиши точно весь текст, сохрани порядок и пунктуацию."
    })
    resp = claude.messages.create(model="claude-opus-4-5", max_tokens=4096,
                                   messages=[{"role": "user", "content": content}])
    return resp.content[0].text


def check_comprehension(book_text: str, retelling: str, pages: str, title: str) -> dict:
    prompt = f"""Ты добрый учитель начальной школы.

КНИГА: «{title}», страницы {pages}
ТЕКСТ: \"\"\"{book_text[:3000]}\"\"\"
ПЕРЕСКАЗ: \"\"\"{retelling}\"\"\"

Оцени: главные события, смысл, детали.
Ответь ТОЛЬКО чистым JSON без markdown:
{{"passed": true/false, "score": 0-100, "feedback": "для ребёнка (дружелюбно)", "summary": "для родителя (2-3 предложения)"}}"""

    resp = claude.messages.create(model="claude-opus-4-5", max_tokens=1024,
                                   messages=[{"role": "user", "content": prompt}])
    raw = resp.content[0].text.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                raw = part
                break
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(m.group() if m else raw)


# ════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or "друг"
    sessions[chat_id] = {}

    last = db_get_last_book(chat_id)
    if last:
        history = db_get_history(chat_id)
        done_pages = [h["pages"] for h in history if h["title"] == last["title"].lower().strip() and h["passed"]]
        done_str = ", ".join(done_pages) if done_pages else "нет"
        await update.message.reply_text(
            f"📚 Привет, {name}!\n\n"
            f"В прошлый раз ты читал *«{last['title']}»*\n"
            f"Последние страницы: *{last['pages']}*\n"
            f"Уже сдал: {done_str}\n\n"
            f"Продолжаем или другая книга?",
            parse_mode="Markdown",
            reply_markup=kb_continue(last["title"]),
        )
        sessions[chat_id]["last_book"] = last
        return WAIT_CONTINUE

    await update.message.reply_text(
        f"📚 Привет, {name}! Давай проверим чтение.\n\n"
        "Напиши *название книги* которую ты читаешь.\n"
        "_Например: Гарри Поттер и философский камень_",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )
    return WAIT_BOOK_TITLE


async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = update.message.text

    if "Продолжаю" in text:
        last = sessions[chat_id].get("last_book", {})
        sessions[chat_id]["book_title"]  = last["title"]
        sessions[chat_id]["book_text"]   = ""
        sessions[chat_id]["book_source"] = ""
        sessions[chat_id]["photos"]      = []
        await update.message.reply_text(
            f"📖 *«{last['title']}»*\n\n"
            "Напиши *страницы* которые ты читал сегодня.\n"
            "_Например: 141-155_",
            parse_mode="Markdown",
            reply_markup=kb_cancel(),
        )
        return WAIT_PAGES

    if "Другая книга" in text or "❌" in text:
        sessions[chat_id] = {"photos": []}
        await update.message.reply_text(
            "Напиши *название новой книги*:",
            parse_mode="Markdown",
            reply_markup=kb_cancel(),
        )
        return WAIT_BOOK_TITLE

    return WAIT_CONTINUE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    chat_id = update.effective_chat.id
    title = update.message.text.strip()
    sessions[chat_id] = {"book_title": title, "photos": [], "book_text": "", "book_source": ""}
    await update.message.reply_text(
        f"📖 *«{title}»*\n\n"
        "Напиши *страницы* которые ты читал.\n"
        "_Например: 5-10 или 12_",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )
    return WAIT_PAGES


async def receive_pages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    chat_id = update.effective_chat.id
    pages = update.message.text.strip()

    if not re.match(r"^\d+[\-–]\d+$|^\d+$", pages):
        await update.message.reply_text("❗ Напиши страницы в формате *5-10* или *12*", parse_mode="Markdown")
        return WAIT_PAGES

    title = sessions[chat_id].get("book_title", "")

    # Блокировка повтора
    if db_pages_already_done(chat_id, title, pages):
        await update.message.reply_text(
            f"🚫 Страницы *{pages}* книги «{title}» ты уже сдавал!\n\n"
            "Напиши *другие страницы* которые ты читал.",
            parse_mode="Markdown",
        )
        return WAIT_PAGES

    sessions[chat_id]["pages"] = pages

    # Поиск книги онлайн (быстрый, без зависания)
    search_msg = await update.message.reply_text(
        "🔍 Ищу книгу онлайн...", reply_markup=kb_remove()
    )

    try:
        result = search_book_online(title, pages)
    except Exception as e:
        logger.error("Search error: %s", e)
        result = {"found": False, "text": "", "source": ""}

    if result.get("found") and result.get("text"):
        sessions[chat_id]["book_text"]   = result["text"]
        sessions[chat_id]["book_source"] = result.get("source", "интернет")
        await search_msg.edit_text(
            f"✅ Нашёл книгу ({result['source']})!\n\n"
            "🎙 Запиши *голосовое сообщение* — расскажи своими словами что прочитал.",
            parse_mode="Markdown",
        )
        return WAIT_AUDIO
    else:
        await search_msg.edit_text(
            "😕 Не нашёл онлайн.\n\n"
            "📷 Сфотографируй страницы книги и отправь фото сюда.\n"
            "Когда отправишь все — нажми кнопку 👇"
        )
        await update.message.reply_text("Отправляй фото:", reply_markup=kb_send_photo())
        return WAIT_PHOTOS


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if chat_id not in sessions:
        await update.message.reply_text("Напиши /start")
        return ConversationHandler.END
    b64 = await download_photo_b64(context.bot, update.message.photo[-1].file_id)
    sessions[chat_id]["photos"].append(b64)
    count = len(sessions[chat_id]["photos"])
    await update.message.reply_text(f"✅ Фото {count} получено! Ещё или нажми кнопку.", reply_markup=kb_send_photo())
    return WAIT_PHOTOS


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id, {})
    if not session.get("photos"):
        await update.message.reply_text("❗ Сначала отправь хотя бы одно фото.", reply_markup=kb_send_photo())
        return WAIT_PHOTOS
    await update.message.reply_text("⏳ Читаю текст с фото...", reply_markup=kb_remove())
    text = ocr_images(session["photos"], session["pages"])
    sessions[chat_id]["book_text"]   = text
    sessions[chat_id]["book_source"] = "фото"
    await update.message.reply_text(
        "✅ Готово!\n\n🎙 Запиши *голосовое сообщение* — расскажи что прочитал.",
        parse_mode="Markdown",
    )
    return WAIT_AUDIO


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session or not session.get("book_text"):
        await update.message.reply_text("❗ Что-то пошло не так. Напиши /start")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Слушаю...", reply_markup=kb_remove())
    try:
        voice = await download_voice(context.bot, update.message.voice.file_id)
        await update.message.reply_text("🔤 Распознаю речь...")
        retelling = transcribe(voice)
        logger.info("Retelling: %s", retelling)

        await update.message.reply_text("🧠 Проверяю понимание...")
        title  = session["book_title"]
        pages  = session["pages"]
        result = check_comprehension(session["book_text"], retelling, pages, title)

        passed   = result.get("passed", False)
        score    = result.get("score", 0)
        feedback = result.get("feedback", "")
        summary  = result.get("summary", "")

        # Сохранить результат и последнюю книгу
        db_save_result(chat_id, title, pages, passed, score)
        db_save_last_book(chat_id, title, pages)

        # Ответ ребёнку
        if passed:
            child_msg = f"🎉 *Отлично! Молодец!*\n\n💬 {feedback}\n\n⭐ Результат: *{score}/100*"
        else:
            child_msg = (
                f"🤔 *Почти! Попробуй ещё раз.*\n\n💬 {feedback}\n\n"
                f"⭐ Результат: *{score}/100*\n\nНапиши /start чтобы попробовать снова."
            )
        await update.message.reply_text(child_msg, parse_mode="Markdown")

        # Уведомление родителю
        child_name = update.effective_user.first_name or "Ребёнок"
        emoji = "✅" if passed else "❌"
        await context.bot.send_message(
            chat_id=PARENT_CHAT_ID,
            text=(
                f"{emoji} *Отчёт о чтении*\n\n"
                f"👦 {child_name}\n"
                f"📖 _{title}_\n"
                f"📄 Страницы: {pages}\n"
                f"⭐ Оценка: *{score}/100*\n\n"
                f"🗣 *Пересказ:*\n_{retelling}_\n\n"
                f"🔍 *Анализ:*\n{summary}"
            ),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("Audio error: %s", e, exc_info=True)
        await update.message.reply_text("😔 Ошибка. Напиши /start и попробуй снова.")
    finally:
        sessions.pop(chat_id, None)

    return ConversationHandler.END


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = db_get_history(chat_id)
    if not rows:
        await update.message.reply_text("📭 Истории пока нет.")
        return
    lines = ["📊 *История чтения:*\n"]
    for r in rows:
        emoji = "✅" if r["passed"] else "❌"
        lines.append(f"{emoji} _{r['title']}_ стр. {r['pages']} — {r['score']}/100")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("❌ Отменено. Напиши /start.", reply_markup=kb_remove())
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_CONTINUE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_continue)],
            WAIT_BOOK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            WAIT_PAGES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pages)],
            WAIT_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, photos_done),
            ],
            WAIT_AUDIO:      [MessageHandler(filters.VOICE, receive_audio)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("history", cmd_history))
    logger.info("Bot v4 started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
