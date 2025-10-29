# bot.py — МедНавигатор РГ Клиник (интегрирован с голосовым помощником)
# --------------------------------------------------------------
# ✔ Поддержка голосовых сообщений (STT + TTS через Yandex SpeechKit)
# ✔ Основной функционал (Google Sheets, расписание, запись, цены и т.д.)
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

YANDEX_API_KEY  = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

URL_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
URL_TTS = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
HEADERS = {"Authorization": f"Api-Key {YANDEX_API_KEY}"} if YANDEX_API_KEY else {}

SCHEDULE_SHEET  = "Schedule"
REQUESTS_SHEET  = "Requests"
PRICES_SHEET    = "Prices"
PREP_SHEET      = "Prep"
INFO_SHEET      = "Info"
DOCTORS_SHEET   = "Doctors"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# --------- UI ----------
WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\nВыберите раздел ниже:"
HELP    = ("ℹ️ Команды:\n"
           "/menu — меню\n"
           "/init_sheets — создать листы и шапки\n"
           "/fix_headers — обновить шапки\n"
           "/debug_slots — показать видимые слоты\n"
           "/doctor <фамилия|спец> — карточка врача\n"
           "/hours /manager /promos /services /contacts\n")

BTN_RECORD   = "📅 Запись на приём"
BTN_PRICES   = "🧾 Цены и анализы"
BTN_PREP     = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

# ================================================================
# === Голосовые функции (SpeechKit)
# ================================================================

async def stt_yandex_ogg(ogg_bytes: bytes) -> str:
    params = {
        "folderId": YANDEX_FOLDER_ID,
        "lang": "ru-RU",
        "format": "oggopus",
        "profanityFilter": "false",
    }
    try:
        r = requests.post(URL_STT, headers=HEADERS, params=params, data=ogg_bytes, timeout=60)
        r.raise_for_status()
        for line in r.text.splitlines():
            if line.startswith("result="):
                return line.split("=", 1)[1].strip()
        return ""
    except Exception as e:
        logging.error(f"STT error: {e}")
        return ""

async def tts_yandex_ogg(text: str) -> bytes:
    data = {
        "text": text,
        "lang": "ru-RU",
        "voice": "ermil",
        "emotion": "neutral",
        "speed": "1.0",
        "format": "oggopus",
        "folderId": YANDEX_FOLDER_ID,
    }
    try:
        with requests.post(URL_TTS, headers=HEADERS, data=data, stream=True, timeout=60) as r:
            r.raise_for_status()
            return b"".join(r.iter_content(4096))
    except Exception as e:
        logging.error(f"TTS error: {e}")
        return b""

def route_intent(text: str) -> str:
    t = (text or "").lower()
    if "цена" in t or "стоит" in t:
        return "Цены на анализы можно уточнить по названию или коду. Какой анализ интересует?"
    if "запис" in t:
        return "Готов оформить запись. Укажите направление и удобное время."
    if "график" in t or "режим" in t or "часы" in t:
        return "Мы работаем ежедневно. Уточните клинику и дату, чтобы сказать точнее."
    return "Я помогу с услугами, ценами и записью. Сформулируйте запрос одним предложением."

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
        logging.info(f"Получено голосовое: {len(ogg_bytes)} байт")

        user_text = await stt_yandex_ogg(ogg_bytes)
        if not user_text:
            await update.message.reply_text("Не удалось распознать речь. Повторите, пожалуйста 🙏")
            return

        reply_text = route_intent(user_text)
        tts_bytes = await tts_yandex_ogg(reply_text)

        if not tts_bytes:
            await update.message.reply_text(f"Вы сказали: «{user_text}»\n\n{reply_text}")
            return

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(tts_bytes)
            tmp.flush()
            await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=f"Вы сказали: «{user_text}»\n\n{reply_text}")

    except Exception as e:
        logging.exception(f"Ошибка при обработке голосового: {e}")
        await update.message.reply_text("Произошла ошибка при обработке голосового сообщения.")

# ================================================================
# === Основной функционал (Google Sheets, команды, меню и т.п.)
# ================================================================

# --- (сюда интегрируется остальной код из вашего рабочего bot.py: Google Sheets, расписание, команды и т.д.) ---
# Для краткости не повторяем весь код, он остаётся без изменений.
# Главное — добавить регистрацию голосового обработчика при сборке приложения.

# ================================================================
# === Инициализация и запуск
# ================================================================

def build_app():
    from telegram.ext import ApplicationBuilder
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Добавляем голосовой обработчик
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Здесь добавьте все остальные обработчики из вашего исходного bot.py (команды, FSM, меню и т.д.)

    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    logging.info("Бот запускается (polling)…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
