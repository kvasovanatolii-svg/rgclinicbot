# bot.py — МедНавигатор РГ Клиник (v7.2, стабильная версия)
# Полная поддержка голосового и текстового режимов + защита от пустых сообщений

import os
import re
import json
import time
import logging
from io import BytesIO
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

# ---- Голос (опционально)
try:
    from gtts import gTTS
    TTS_AVAILABLE = True
except Exception:
    TTS_AVAILABLE = False

from openai import OpenAI

# --------- ENV ----------
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID   = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON     = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_CHAT_ID    = os.getenv("ADMIN_CHAT_ID")

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
VOICE_TEXT_DUP   = os.getenv("VOICE_TEXT_DUPLICATE", "1")

SCHEDULE_SHEET = os.getenv("GOOGLE_SCHEDULE_SHEET", "Schedule")
REQUESTS_SHEET = os.getenv("GOOGLE_REQUESTS_SHEET", "Requests")
PRICES_SHEET   = os.getenv("GOOGLE_PRICES_SHEET", "Prices")
PREP_SHEET     = os.getenv("GOOGLE_PREP_SHEET", "Prep")
DOCTORS_SHEET  = "Doctors"
INFO_SHEET     = "Info"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\nВыберите раздел ниже:"
BTN_RECORD   = "📅 Запись на приём"
BTN_PRICES   = "🧾 Цены и анализы"
BTN_PREP     = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RECORD,   callback_data="RECORD")],
        [InlineKeyboardButton(BTN_PRICES,   callback_data="PRICES")],
        [InlineKeyboardButton(BTN_PREP,     callback_data="PREP")],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data="CONTACTS")],
    ])

# --------- Google Sheets ----------
def gs_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(SERVICE_JSON), scopes=scopes)
    return gspread.authorize(creds)

def open_ws(name):
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        sh.add_worksheet(name, 200, 30)
        return sh.worksheet(name)

def read_all(ws):
    vals = ws.get_all_values()
    if not vals: return [], []
    return vals[0], vals[1:]

# --------- Безопасные ответы ----------
DEFAULT_EMPTY_REPLY = "Извините, не нашёл информации по запросу. Попробуйте уточнить формулировку 🙏"

def _pick_target(update: Update):
    if getattr(update, "message", None):
        return update.message.reply_text, update.message
    if getattr(update, "callback_query", None) and update.callback_query.message:
        return update.callback_query.message.reply_text, update.callback_query.message
    return None, None

async def _safe_text(update: Update, text: str | None):
    send, _ = _pick_target(update)
    if not send: return
    txt = (text or "").strip() or DEFAULT_EMPTY_REPLY
    await send(txt)

async def _safe_text_kb(update: Update, text: str | None, kb=None):
    send, _ = _pick_target(update)
    if not send: return
    txt = (text or "").strip() or DEFAULT_EMPTY_REPLY
    await send(txt, reply_markup=kb)

# --------- Голос (STT / TTS) ----------
oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
VOICE_MODE_USERS = set()

def is_voice_enabled(uid: int): return uid in VOICE_MODE_USERS

async def stt_transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not oa_client:
        await _safe_text(update, "Распознавание недоступно — нет OPENAI_API_KEY.")
        return ""
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        bio = BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        resp = oa_client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", bio, "audio/ogg")
        )
        return getattr(resp, "text", "").strip()
    except Exception as e:
        await _safe_text(update, f"Ошибка распознавания речи: {e}")
        return ""

async def tts_send(update: Update, text: str):
    if not TTS_AVAILABLE:
        await _safe_text(update, text)
        return
    try:
        mp3 = BytesIO()
        gTTS(text=(text or " "), lang="ru").write_to_fp(mp3)
        mp3.seek(0)
        _, msg = _pick_target(update)
        await msg.chat.send_audio(audio=mp3, filename="reply.mp3", title="Ответ")
    except Exception:
        await _safe_text(update, text)

async def smart_reply(update: Update, text: str):
    send, _ = _pick_target(update)
    if not send: return
    txt = (text or "").strip() or DEFAULT_EMPTY_REPLY
    uid = update.effective_user.id if update.effective_user else 0
    if uid and is_voice_enabled(uid):
        if VOICE_TEXT_DUP == "1":
            await send(txt)
            await tts_send(update, txt)
        else:
            await tts_send(update, txt)
    else:
        await send(txt)

# --------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await smart_reply(update, WELCOME)
    await _safe_text_kb(update, "Главное меню:", main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_text_kb(update, "Главное меню:", main_menu())

# Голосовой режим
async def voice_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    VOICE_MODE_USERS.add(update.effective_user.id)
    await smart_reply(update, "🔊 Голосовой помощник включён.")

async def voice_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    VOICE_MODE_USERS.discard(update.effective_user.id)
    await smart_reply(update, "🔕 Голосовой помощник выключен.")

async def voice_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    on = "включён" if is_voice_enabled(update.effective_user.id) else "выключен"
    mode = "голос+текст" if VOICE_TEXT_DUP == "1" else "только голос"
    await smart_reply(update, f"ℹ️ Режим: {on} ({mode})")

# Меню кнопки
async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "PRICES":
        await _safe_text(update, "🧾 Напишите название услуги или код (например, SRV-003)")
    elif data == "PREP":
        await _safe_text(update, "ℹ️ Напишите название анализа — пришлю памятку.")
    elif data == "CONTACTS":
        await _safe_text(update, "📍 РГ Клиник\nТелефон: +7 (000) 000-00-00\nРежим работы: 08:00–20:00")

# FAQ — универсальные ответы
async def faq_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text: return
    tl = text.lower()
    if "глюкоз" in tl:
        await smart_reply(update, "Анализ на глюкозу: 250 ₽, срок выполнения — 1 день.")
    elif "врач" in tl or "доктор" in tl:
        await smart_reply(update, "Наши врачи принимают ежедневно с 08:00 до 20:00.")
    else:
        await _safe_text_kb(update, "Я вас понял. Выберите раздел ниже 👇", main_menu())

# Голосовые
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await stt_transcribe_voice(update, context)
    if not text:
        return
    await _safe_text(update, f"🗣 Распознал: {text}")
    context.user_data["_override_text"] = text
    await faq_router(update, context)
    context.user_data.pop("_override_text", None)

# --------- Ошибки ----------
async def error_handler(update, context):
    err = context.error
    logging.error(f"Ошибка: {err}")
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=f"⚠️ Ошибка: {err}")
        except Exception:
            pass

# --------- Init ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))

    app.add_handler(CommandHandler("voice_on", voice_on))
    app.add_handler(CommandHandler("voice_off", voice_off))
    app.add_handler(CommandHandler("voice_status", voice_status))

    app.add_handler(CallbackQueryHandler(menu_click, pattern="^(PRICES|PREP|CONTACTS)$"))

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, faq_router))
    app.add_error_handler(error_handler)

    logging.info("✅ Бот запущен (polling)…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
