import os
import logging
import asyncio
import base64
import tempfile
from pathlib import Path

from telegram import Update, Bot
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
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]       # Bot token from @BotFather
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]    # Anthropic key
GROQ_KEY         = os.environ["GROQ_API_KEY"]         # Groq key (free)
PARENT_CHAT_ID   = int(os.environ["PARENT_CHAT_ID"])  # Your personal Telegram chat ID

# ─── Clients ────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
groq   = Groq(api_key=GROQ_KEY)

# ─── Conversation states ─────────────────────────────────────────────────────
WAIT_PAGES, WAIT_AUDIO = range(2)

# ─── In-memory session storage ───────────────────────────────────────────────
# sessions[chat_id] = {"photos": [...base64...], "pages": "5-10"}
sessions: dict[int, dict] = {}


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def download_photo_base64(bot: Bot, file_id: str) -> str:
    """Download Telegram photo and return as base64 string."""
    file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    with open(tmp_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    Path(tmp_path).unlink(missing_ok=True)
    return data


async def download_voice_bytes(bot: Bot, file_id: str) -> bytes:
    """Download Telegram voice message and return raw bytes."""
    file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    with open(tmp_path, "rb") as f:
        data = f.read()
    Path(tmp_path).unlink(missing_ok=True)
    return data


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio using Groq Whisper (free tier)."""
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


def extract_text_from_images(photo_b64_list: list[str], pages: str) -> str:
    """Use Claude Vision to extract text from book page photos."""
    content = []
    for i, b64 in enumerate(photo_b64_list):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    content.append({
        "type": "text",
        "text": (
            f"Это фотографии страниц книги (страницы {pages}).\n"
            "Пожалуйста, точно перепиши весь текст с этих страниц — сохрани порядок, "
            "абзацы и пунктуацию. Не добавляй ничего от себя."
        ),
    })
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def check_comprehension(book_text: str, child_retelling: str, pages: str) -> dict:
    """
    Ask Claude to evaluate how well the child understood what they read.
    Returns {"passed": bool, "score": int, "feedback": str, "summary": str}
    """
    prompt = f"""Ты добрый и внимательный учитель начальной школы.

ТЕКСТ КНИГИ (страницы {pages}):
\"\"\"
{book_text}
\"\"\"

ПЕРЕСКАЗ РЕБЁНКА:
\"\"\"
{child_retelling}
\"\"\"

Оцени пересказ по следующим критериям:
1. Главные события и герои — упомянуты ли?
2. Общий смысл — понял ли ребёнок суть прочитанного?
3. Достаточность деталей — не обязательно всё, но основное должно быть.

Ответь СТРОГО в формате JSON (без markdown-блоков, только чистый JSON):
{{
  "passed": true или false,
  "score": число от 0 до 100,
  "feedback": "Короткая похвала или подсказка для ребёнка (1-2 предложения, дружелюбно)",
  "summary": "Краткое резюме для родителя: что ребёнок понял, что упустил (2-3 предложения)"
}}
"""
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    import json
    raw = response.content[0].text.strip()
    # Strip possible markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ═══════════════════════════════════════════════════════════════════════════════
#  CONVERSATION HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sessions[update.effective_chat.id] = {"photos": [], "pages": ""}
    await update.message.reply_text(
        "📚 Привет! Давай проверим чтение.\n\n"
        "Шаг 1️⃣: Отправь фото страниц книги, которые ты прочитал.\n"
        "Можно отправить несколько фото подряд.\n\n"
        "Когда отправишь все фото — напиши /done"
    )
    return WAIT_PAGES


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if chat_id not in sessions:
        sessions[chat_id] = {"photos": [], "pages": ""}

    photo = update.message.photo[-1]  # highest resolution
    b64 = await download_photo_base64(context.bot, photo.file_id)
    sessions[chat_id]["photos"].append(b64)

    count = len(sessions[chat_id]["photos"])
    await update.message.reply_text(
        f"✅ Фото {count} получено! Отправь ещё или напиши /done"
    )
    return WAIT_PAGES


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id, {})

    if not session.get("photos"):
        await update.message.reply_text("❗ Сначала отправь хотя бы одно фото страницы.")
        return WAIT_PAGES

    await update.message.reply_text(
        "📖 Шаг 2️⃣: Напиши, какие страницы ты читал.\n"
        "Например: *5-10* или *12-15*",
        parse_mode="Markdown",
    )
    return WAIT_PAGES


async def receive_pages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Simple validation: should look like "5-10" or "12"
    import re
    if not re.match(r"^\d+[\-–]\d+$|^\d+$", text):
        await update.message.reply_text(
            "❗ Напиши номера страниц в формате *5-10* или *12*",
            parse_mode="Markdown",
        )
        return WAIT_PAGES

    sessions[chat_id]["pages"] = text
    await update.message.reply_text(
        f"👍 Страницы {text} записаны!\n\n"
        "🎙 Шаг 3️⃣: Теперь запиши голосовое сообщение — "
        "расскажи своими словами, что ты прочитал."
    )
    return WAIT_AUDIO


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)

    if not session or not session.get("photos") or not session.get("pages"):
        await update.message.reply_text("❗ Сначала отправь фото и укажи страницы. Напиши /start")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Слушаю тебя... Сейчас проверю!")

    try:
        # 1. Download audio
        voice_bytes = await download_voice_bytes(context.bot, update.message.voice.file_id)

        # 2. Transcribe
        await update.message.reply_text("🔤 Распознаю речь...")
        retelling = transcribe_audio(voice_bytes)
        logger.info("Transcription: %s", retelling)

        # 3. Extract text from book photos
        await update.message.reply_text("📖 Читаю текст книги...")
        pages = session["pages"]
        book_text = extract_text_from_images(session["photos"], pages)

        # 4. Check comprehension
        await update.message.reply_text("🧠 Проверяю понимание...")
        result = check_comprehension(book_text, retelling, pages)

        passed  = result.get("passed", False)
        score   = result.get("score", 0)
        feedback = result.get("feedback", "")
        summary  = result.get("summary", "")

        # 5. Reply to child
        if passed:
            child_msg = (
                f"🎉 Отлично! Ты справился!\n\n"
                f"💬 {feedback}\n\n"
                f"⭐ Результат: {score}/100"
            )
        else:
            child_msg = (
                f"🤔 Почти! Попробуй ещё раз.\n\n"
                f"💬 {feedback}\n\n"
                f"⭐ Результат: {score}/100"
            )
        await update.message.reply_text(child_msg)

        # 6. Notify parent
        child_name = update.effective_user.first_name or "Ребёнок"
        status_emoji = "✅" if passed else "❌"
        parent_msg = (
            f"{status_emoji} *Отчёт о чтении*\n\n"
            f"👦 {child_name}\n"
            f"📖 Страницы: {pages}\n"
            f"⭐ Оценка: {score}/100\n\n"
            f"📝 *Пересказ ребёнка:*\n_{retelling}_\n\n"
            f"🔍 *Анализ:*\n{summary}"
        )
        await context.bot.send_message(
            chat_id=PARENT_CHAT_ID,
            text=parent_msg,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("Error processing session: %s", e, exc_info=True)
        await update.message.reply_text(
            "😔 Что-то пошло не так. Попробуй ещё раз — напиши /start"
        )

    finally:
        sessions.pop(chat_id, None)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("❌ Отменено. Напиши /start чтобы начать заново.")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_PAGES: [
                MessageHandler(filters.PHOTO, receive_photo),
                CommandHandler("done", photos_done),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pages),
            ],
            WAIT_AUDIO: [
                MessageHandler(filters.VOICE, receive_audio),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
