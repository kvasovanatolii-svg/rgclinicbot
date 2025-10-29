# bot.py — МедНавигатор РГ Клиник (интегрирован с голосовым помощником)
# --------------------------------------------------------------
# ✔ Поддержка голосовых сообщений (STT + TTS через Yandex SpeechKit)
# ✔ Основной функционал (Google Sheets, расписание, запись, цены и т.д.)
# ✔ Подробный лог входящих апдейтов и фолбэк-ответчик
# Требования: python-telegram-bot>=20.8, gspread, google-auth, requests

import os
import re
import json
import time
import logging
import tempfile
import requests
from datetime import datetime
from dateutil.parser import parse as dt_parse

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# --------- ENV ----------
BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID  = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON    = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_CHAT_ID   = os.getenv("ADMIN_CHAT_ID")

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

URL_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
URL_TTS = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
HEADERS_SK = {"Authorization": f"Api-Key {YANDEX_API_KEY}"} if YANDEX_API_KEY else {}

SCHEDULE_SHEET  = "Schedule"
REQUESTS_SHEET  = "Requests"
PRICES_SHEET    = "Prices"
PREP_SHEET      = "Prep"
INFO_SHEET      = "Info"
DOCTORS_SHEET   = "Doctors"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# --------- UI ----------
WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.
Выберите раздел ниже:"
HELP    = ("ℹ️ Команды:
"
           "/menu — меню
"
           "/init_sheets — создать листы и шапки
"
           "/fix_headers — обновить шапки
"
           "/debug_slots — показать видимые слоты
"
           "/doctor <фамилия|спец> — карточка врача
"
           "/hours /manager /promos /services /contacts
")

BTN_RECORD   = "📅 Запись на приём"
BTN_PRICES   = "🧾 Цены и анализы"
BTN_PREP     = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

# ================================================================
# === Голосовые функции (SpeechKit)
# ================================================================

async def stt_yandex_ogg(ogg_bytes: bytes) -> str:
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        logging.warning("SpeechKit env vars missing")
        return ""
    params = {"folderId": YANDEX_FOLDER_ID, "lang": "ru-RU", "format": "oggopus", "profanityFilter": "false"}
    try:
        r = requests.post(URL_STT, headers=HEADERS_SK, params=params, data=ogg_bytes, timeout=60)
        r.raise_for_status()
        for line in r.text.splitlines():
            if line.startswith("result="):
                return line.split("=", 1)[1].strip()
        return ""
    except Exception as e:
        logging.exception("STT error: %s", e)
        return ""

async def tts_yandex_ogg(text: str) -> bytes:
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        return b""
    data = {"text": text, "lang": "ru-RU", "voice": "ermil", "emotion": "neutral", "speed": "1.0",
            "format": "oggopus", "folderId": YANDEX_FOLDER_ID}
    try:
        with requests.post(URL_TTS, headers=HEADERS_SK, data=data, stream=True, timeout=60) as r:
            r.raise_for_status()
            return b"".join(r.iter_content(4096))
    except Exception as e:
        logging.exception("TTS error: %s", e)
        return b""

def route_intent(text: str) -> str:
    t = (text or "").lower()
    if "цена" in t or "стоит" in t:
        return "Цены на анализы можно уточнить по коду/названию. Какой анализ интересует?"
    if "запис" in t:
        return "Готов оформить запись. Укажите направление и удобное время."
    if any(k in t for k in ["график", "режим", "часы"]):
        return "Мы работаем ежедневно. Уточните клинику и дату."
    return "Я помогу с услугами, ценами и записью. Сформулируйте запрос одним предложением, пожалуйста."

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
        logging.info("Получено голосовое: %s байт", len(ogg_bytes))
        user_text = await stt_yandex_ogg(bytes(ogg_bytes))
        if not user_text:
            await update.message.reply_text("Не удалось распознать речь. Повторите, пожалуйста 🙏")
            return
        reply_text = route_intent(user_text)
        tts_bytes = await tts_yandex_ogg(reply_text)
        if not tts_bytes:
            await update.message.reply_text(f"Вы сказали: «{user_text}»

{reply_text}")
            return
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(tts_bytes); tmp.flush()
            await update.message.reply_voice(voice=open(tmp.name, "rb"),
                                             caption=f"Вы сказали: «{user_text}»

{reply_text}")
    except Exception as e:
        logging.exception("Ошибка голосового обработчика: %s", e)
        await update.message.reply_text("Тех. ошибка при обработке голосового.")

# ================================================================
# === Прочие функции и логика бота (оставьте вашу реализацию)
# ================================================================
# ... здесь остаются ваши функции Google Sheets, запись на приём, роутеры и т.д. ...

# --------- Универсальный логгер всех апдейтов ----------
async def _log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        as_json = update.to_dict()
        logging.info("UPDATE: %s", json.dumps(as_json, ensure_ascii=False)[:2000])
    except Exception as e:
        logging.warning("Не удалось сериализовать update: %s", e)

# --------- Простейший фолбэк для текста ----------
async def _fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Принял сообщение. Уточните запрос, пожалуйста 🙌")

# ================================================================
# === Инициализация и запуск
# ================================================================

async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook снят, очередь очищена")
    except Exception as e:
        logging.warning("Не удалось снять webhook: %s", e)


def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    # 0) Лог всех апдейтов — всегда первым
    app.add_handler(MessageHandler(filters.ALL, _log_all_updates), group=0)

    # 1) Голосовые сообщения — до текстового роутера
    app.add_handler(MessageHandler(filters.VOICE, handle_voice), group=1)

    # 2) Здесь добавьте остальные ваши обработчики: команды, FSM, кнопки, и т.д.
    # Пример (оставьте ваши реальные функции):
    # app.add_handler(CommandHandler("start", start))
    # app.add_handler(CommandHandler("menu", menu))
    # app.add_handler(CallbackQueryHandler(menu_click, pattern="^(PRICES|PREP|CONTACTS)$"))
    # app.add_handler(conv)

    # 3) Ваш основной текстовый роутер (FAQ)
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, faq_router), group=2)

    # 99) Фолбэк на случай, если текст не обработался
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _fallback_text), group=99)

    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    logging.info("Бот запускается (polling)…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
