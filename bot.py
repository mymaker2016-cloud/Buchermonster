import os
import re
import json
import logging
import base64
import tempfile
from pathlib import Path

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

import anthropic
from groq import Groq

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Env vars ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
GROQ_KEY       = os.environ["GROQ_API_KEY"]
PARENT_CHAT_ID = int(os.environ["PARENT_CHAT_ID"])

# ─── Clients ────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
groq   = Groq(api_key=GROQ_KEY)

# ─── Conversation states ─────────────────────────────────────────────────────
(
    WAIT_BOOK_TITLE,   # ребёнок вводит название книги
    WAIT_PAGES,        # вводит страницы
    WAIT_PHOTOS,       # отправляет фото (если книга не найдена онлайн)
    WAIT_MORE_PHOTOS,  # отправляет дополнительные фото
    WAIT_AUDIO,        # записывает голосовое
) = range(5)

# ─── In-memory sessions ──────────────────────────────────────────────────────
sessions: dict[int, dict] = {}

# ─── Keyboards ───────────────────────────────────────────────────────────────

def kb_send_photo():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📷 Я отправил все фото")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def kb_record_audio():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🎙 Я готов записать пересказ")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def kb_cancel():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def kb_remove():
    return ReplyKeyboardRemove()


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def download_photo_base64(bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    with open(tmp_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    Path(tmp_path).unlink(missing_ok=True)
    return data


async def download_voice_bytes(bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    with open(tmp_path, "rb") as f:
        data = f.read()
    Path(tmp_path).unlink(missing_ok=True)
    return data


def search_book_online(title: str, pages: str) -> dict:
    """
    Ask Claude to search for the book text online using web_search tool.
    Returns {"found": bool, "text": str, "source": str}
    """
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": (
                f"Найди текст книги «{title}» страницы {pages} в открытых источниках. "
                "Это может быть Lib.ru, Royallib, Litres preview, Wikisource, Flibusta или другие легальные источники. "
                "Если нашёл — верни ТОЛЬКО JSON без markdown:\n"
                '{"found": true, "text": "точный текст страниц", "source": "название сайта"}\n'
                "Если не нашёл — верни ТОЛЬКО:\n"
                '{"found": false, "text": "", "source": ""}'
            )
        }]
    )

    # Extract text from all content blocks
    raw = ""
    for block in response.content:
        if hasattr(block, "text"):
            raw += block.text

    raw = raw.strip()
    # Strip markdown fences if any
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        return json.loads(raw.strip())
    except Exception:
        logger.warning("Could not parse book search result: %s", raw[:200])
        return {"found": False, "text": "", "source": ""}


def extract_text_from_images(photo_b64_list: list[str], pages: str) -> str:
    content = []
    for b64 in photo_b64_list:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    content.append({
        "type": "text",
        "text": (
            f"Это фотографии страниц книги (страницы {pages}).\n"
            "Перепиши точно весь текст с этих страниц — сохрани порядок, абзацы и пунктуацию. "
            "Не добавляй ничего от себя."
        ),
    })
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def transcribe_audio(audio_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            result = groq.audio.transcriptions.create(
                file=("audio.ogg", f, "audio/ogg"),
                model="whisper-large-v3",
                language="ru",
                response_format="text",
            )
        return result.strip()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def check_comprehension(book_text: str, child_retelling: str, pages: str, title: str) -> dict:
    prompt = f"""Ты добрый и внимательный учитель начальной школы.

КНИГА: «{title}», страницы {pages}

ТЕКСТ КНИГИ:
\"\"\"
{book_text}
\"\"\"

ПЕРЕСКАЗ РЕБЁНКА:
\"\"\"
{child_retelling}
\"\"\"

Оцени пересказ:
1. Главные события и герои — упомянуты ли?
2. Общий смысл — понял ли ребёнок суть?
3. Достаточность деталей — основное должно быть.

Ответь СТРОГО в формате JSON (без markdown, только чистый JSON):
{{
  "passed": true или false,
  "score": число от 0 до 100,
  "feedback": "Похвала или подсказка для ребёнка (1-2 предложения, дружелюбно, на ты)",
  "summary": "Резюме для родителя: что понял, что упустил (2-3 предложения)"
}}"""

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ═══════════════════════════════════════════════════════════════════════════════
#  CONVERSATION HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    sessions[chat_id] = {"photos": [], "pages": "", "book_title": "", "book_text": "", "book_source": ""}

    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"📚 Привет, {name}! Давай проверим чтение.\n\n"
        "Шаг 1️⃣: Напиши название книги, которую ты читаешь.\n\n"
        "_Например: Гарри Поттер и философский камень_",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )
    return WAIT_BOOK_TITLE


async def receive_book_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    chat_id = update.effective_chat.id
    title = update.message.text.strip()
    sessions[chat_id]["book_title"] = title

    await update.message.reply_text(
        f"📖 Отлично! «{title}»\n\n"
        "Шаг 2️⃣: Напиши, какие страницы ты читал.\n"
        "_Например: 5-10 или 12_",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )
    return WAIT_PAGES


async def receive_pages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if not re.match(r"^\d+[\-–]\d+$|^\d+$", text):
        await update.message.reply_text(
            "❗ Напиши номера страниц в формате *5-10* или *12*",
            parse_mode="Markdown",
        )
        return WAIT_PAGES

    sessions[chat_id]["pages"] = text
    title = sessions[chat_id]["book_title"]

    # Search book online
    search_msg = await update.message.reply_text(
        "🔍 Ищу текст книги в интернете...",
        reply_markup=kb_remove(),
    )

    result = search_book_online(title, text)

    if result.get("found") and result.get("text"):
        sessions[chat_id]["book_text"]   = result["text"]
        sessions[chat_id]["book_source"] = result.get("source", "интернет")

        await search_msg.edit_text(
            f"✅ Нашёл книгу онлайн ({result.get('source', '')})!\n\n"
            "Шаг 3️⃣: Теперь запиши голосовое сообщение — "
            "расскажи своими словами, что ты прочитал на этих страницах.\n\n"
            "🎙 Нажми на микрофон в Telegram и говори!",
        )
        return WAIT_AUDIO

    else:
        # Book not found — ask for photos
        await search_msg.edit_text(
            "😕 Не нашёл эту книгу в интернете.\n\n"
            "Шаг 3️⃣: Сфотографируй страницы книги и отправь фото сюда.\n"
            "Можно отправить несколько фото подряд.\n\n"
            "📷 Когда отправишь все — нажми кнопку ниже.",
        )
        await update.message.reply_text(
            "Отправляй фото 👇",
            reply_markup=kb_send_photo(),
        )
        return WAIT_PHOTOS


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if chat_id not in sessions:
        await update.message.reply_text("Напиши /start чтобы начать заново.")
        return ConversationHandler.END

    photo = update.message.photo[-1]
    b64 = await download_photo_base64(context.bot, photo.file_id)
    sessions[chat_id]["photos"].append(b64)
    count = len(sessions[chat_id]["photos"])

    await update.message.reply_text(
        f"✅ Фото {count} получено!\n"
        "Отправь ещё или нажми кнопку, если все фото готовы.",
        reply_markup=kb_send_photo(),
    )
    return WAIT_PHOTOS


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    session = sessions.get(chat_id, {})
    if not session.get("photos"):
        await update.message.reply_text(
            "❗ Сначала отправь хотя бы одно фото страницы.",
            reply_markup=kb_send_photo(),
        )
        return WAIT_PHOTOS

    pages = session["pages"]
    await update.message.reply_text(
        "⏳ Читаю текст с фото...",
        reply_markup=kb_remove(),
    )
    book_text = extract_text_from_images(session["photos"], pages)
    sessions[chat_id]["book_text"]   = book_text
    sessions[chat_id]["book_source"] = "фото"

    await update.message.reply_text(
        "✅ Текст распознан!\n\n"
        "Шаг 4️⃣: Запиши голосовое сообщение — "
        "расскажи своими словами, что ты прочитал.\n\n"
        "🎙 Нажми на микрофон в Telegram и говори!",
        reply_markup=kb_remove(),
    )
    return WAIT_AUDIO


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)

    if not session or not session.get("book_text"):
        await update.message.reply_text("❗ Что-то пошло не так. Напиши /start")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Слушаю тебя...", reply_markup=kb_remove())

    try:
        # Transcribe audio
        voice_bytes = await download_voice_bytes(context.bot, update.message.voice.file_id)
        await update.message.reply_text("🔤 Распознаю речь...")
        retelling = transcribe_audio(voice_bytes)
        logger.info("Transcription: %s", retelling)

        # Check comprehension
        await update.message.reply_text("🧠 Проверяю понимание...")
        pages  = session["pages"]
        title  = session["book_title"]
        result = check_comprehension(session["book_text"], retelling, pages, title)

        passed   = result.get("passed", False)
        score    = result.get("score", 0)
        feedback = result.get("feedback", "")
        summary  = result.get("summary", "")

        # Reply to child
        if passed:
            child_msg = (
                f"🎉 *Отлично! Ты справился!*\n\n"
                f"💬 {feedback}\n\n"
                f"⭐ Результат: *{score}/100*"
            )
        else:
            child_msg = (
                f"🤔 *Почти! Попробуй ещё раз.*\n\n"
                f"💬 {feedback}\n\n"
                f"⭐ Результат: *{score}/100*\n\n"
                f"Напиши /start чтобы попробовать снова."
            )
        await update.message.reply_text(child_msg, parse_mode="Markdown")

        # Notify parent
        child_name   = update.effective_user.first_name or "Ребёнок"
        status_emoji = "✅" if passed else "❌"
        source_note  = f"(источник: {session['book_source']})" if session.get("book_source") else ""
        parent_msg = (
            f"{status_emoji} *Отчёт о чтении* {source_note}\n\n"
            f"👦 {child_name}\n"
            f"📖 Книга: _{title}_\n"
            f"📄 Страницы: {pages}\n"
            f"⭐ Оценка: *{score}/100*\n\n"
            f"🗣 *Пересказ:*\n_{retelling}_\n\n"
            f"🔍 *Анализ:*\n{summary}"
        )
        await context.bot.send_message(
            chat_id=PARENT_CHAT_ID,
            text=parent_msg,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        await update.message.reply_text(
            "😔 Что-то пошло не так. Попробуй ещё раз — напиши /start"
        )
    finally:
        sessions.pop(chat_id, None)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "❌ Отменено. Напиши /start чтобы начать заново.",
        reply_markup=kb_remove(),
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_BOOK_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_book_title),
            ],
            WAIT_PAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pages),
            ],
            WAIT_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, photos_done),
            ],
            WAIT_AUDIO: [
                MessageHandler(filters.VOICE, receive_audio),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("Bot v2 started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
