# -*- coding: utf-8 -*-
"""
bot.py — МедНавигатор РГ Клиник
Вариант: один файл, polling, голос (SpeechKit), базовые команды.
Важно: это справочная интеграция, мед.консультаций не даём.
"""

import os
import json
import logging
import tempfile
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------
# ENV
# ---------------------------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Google Sheets
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")

# Admin
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Yandex SpeechKit (API Key)
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
URL_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
URL_TTS = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

# ---------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rgclinicbot")

# ---------------------------------------------------------------------
# CONST / UI
# ---------------------------------------------------------------------
WELCOME = (
    "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\n"
    "Помогаю узнать цены и сроки анализов, подготовку, режим работы и оформить запись.\n"
    "Отправьте сообщение или голос — подскажу 😊"
)

BTN_RECORD = "📅 Запись на приём"
BTN_PRICES = "🧾 Цены и анализы"
BTN_PREP = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BTN_RECORD, callback_data="RECORD")],
            [InlineKeyboardButton(BTN_PRICES, callback_data="PRICES")],
            [InlineKeyboardButton(BTN_PREP, callback_data="PREP")],
            [InlineKeyboardButton(BTN_CONTACTS, callback_data="CONTACTS")],
        ]
    )


# ---------------------------------------------------------------------
# Google Sheets helpers (минимум, чтобы не падал код)
# ---------------------------------------------------------------------
def gs_client():
    if not SERVICE_JSON or not SPREADSHEET_ID:
        return None
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(json.loads(SERVICE_JSON), scopes=scopes)
    return gspread.authorize(creds)


def info_get(key: str, default: str = "") -> str:
    """Простая справка из листа Info (можно потом расширить)."""
    try:
        gc = gs_client()
        if not gc:
            return default
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet("Info")
        rows = ws.get_all_records()
        for r in rows:
            if str(r.get("key", "")).strip().lower() == key.lower():
                return str(r.get("value", "")).strip()
    except Exception as e:
        log.warning("Info sheet read error: %s", e)
    return default


# ---------------------------------------------------------------------
# SpeechKit helpers
# ---------------------------------------------------------------------
async def stt_yandex_ogg(ogg_bytes: bytes) -> str:
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        log.warning("SpeechKit env not set")
        return ""
    params = {
        "folderId": YANDEX_FOLDER_ID,
        "lang": "ru-RU",
        "format": "oggopus",
        "profanityFilter": "false",
    }
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    try:
        r = requests.post(
            URL_STT,
            headers=headers,
            params=params,
            data=ogg_bytes,
            timeout=60,
        )
        r.raise_for_status()
        for line in r.text.splitlines():
            if line.startswith("result="):
                return line.split("=", 1)[1].strip()
        return ""
    except Exception as e:
        log.exception("STT error: %s", e)
        return ""


async def tts_yandex_ogg(text: str) -> bytes:
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        return b""
    data = {
        "text": text,
        "lang": "ru-RU",
        "voice": "ermil",
        "emotion": "neutral",
        "speed": "1.0",
        "format": "oggopus",
        "folderId": YANDEX_FOLDER_ID,
    }
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    try:
        with requests.post(
            URL_TTS,
            headers=headers,
            data=data,
            stream=True,
            timeout=60,
        ) as r:
            r.raise_for_status()
            return b"".join(r.iter_content(4096))
    except Exception as e:
        log.exception("TTS error: %s", e)
        return b""


def route_intent(text: str) -> str:
    t = (text or "").lower()
    if "цена" in t or "стоит" in t or "прайс" in t:
        return "Скажите название или код анализа — подскажу ориентировочную стоимость (справочно)."
    if "запис" in t:
        return "Могу оформить предварительную запись. Напишите специализацию и удобное время."
    if "режим" in t or "график" in t or "часы" in t:
        hours = info_get("clinic_hours", "пн–пт 08:00–20:00, сб–вс 09:00–18:00")
        return f"Режим работы (справочно): {hours}. Уточните филиал, если их несколько."
    if "подготов" in t or "натощак" in t:
        return "Напишите название анализа — дам общие требования по подготовке (справочно)."
    return "Я помогу с услугами, ценами и записью. Сформулируйте запрос в одном предложении."


# ---------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------
async def on_startup(app):
    # снимаем вебхук, чтобы polling не конфликтовал
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook снят, очередь очищена")
    except Exception as e:
        log.warning("Не удалось снять webhook: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=main_menu())


async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "PRICES":
        await q.message.reply_text("🧾 Напишите название анализа или код — попробую найти в базе (справочно).")
    elif q.data == "PREP":
        await q.message.reply_text("ℹ️ Напишите название анализа — пришлю памятку по подготовке (справочно).")
    elif q.data == "CONTACTS":
        hours = info_get("clinic_hours", "пн–пт 08:00–20:00, сб–вс 09:00–18:00")
        addr = info_get("clinic_address", "Адрес уточняется")
        phone = info_get("clinic_phone", "+7 (000) 000-00-00")
        await q.message.reply_text(
            f"📍 РГ Клиник\nАдрес: {addr}\nТел.: {phone}\nРежим работы: {hours}",
            reply_markup=main_menu(),
        )
    elif q.data == "RECORD":
        await q.message.reply_text("Напишите, к какому специалисту хотите записаться и дату — передам админу.")
    else:
        await q.message.reply_text("Выберите раздел ниже 👇", reply_markup=main_menu())


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    reply = route_intent(user_text)
    await update.message.reply_text(reply, reply_markup=main_menu())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
        log.info("Получено голосовое: %s байт", len(ogg_bytes))

        user_text = await stt_yandex_ogg(bytes(ogg_bytes))
        if not user_text:
            await update.message.reply_text("Не удалось распознать речь. Повторите, пожалуйста 🙏")
            return

        reply_text = route_intent(user_text)
        tts_bytes = await tts_yandex_ogg(reply_text)

        if not tts_bytes:
            text_out = "Вы сказали: «{}»\n\n{}".format(user_text, reply_text)
            await update.message.reply_text(text_out)
            return

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(tts_bytes)
            tmp.flush()
            caption_out = "Вы сказали: «{}»\n\n{}".format(user_text, reply_text)
            await update.message.reply_voice(
                voice=open(tmp.name, "rb"),
                caption=caption_out,
            )

    except Exception as e:
        log.exception("Ошибка голосового обработчика: %s", e)
        await update.message.reply_text("Тех. ошибка при обработке голосового.")


async def log_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = update.to_dict()
        log.info("UPDATE: %s", json.dumps(data, ensure_ascii=False)[:2000])
    except Exception:
        pass


# ---------------------------------------------------------------------
# Build app
# ---------------------------------------------------------------------
def build_app():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # 0. лог всех апдейтов
    app.add_handler(MessageHandler(filters.ALL, log_all), group=0)

    # 1. голос
    app.add_handler(MessageHandler(filters.VOICE, handle_voice), group=1)

    # 2. команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_click))

    # 3. текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text), group=5)

    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    app = build_app()
    log.info("Бот запускается (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
