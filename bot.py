import os
import json
import logging
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# --- Optional AI (OpenAI) ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# =============================
# ENV
# =============================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# =============================
# SHEETS (русские названия)
# =============================

SHEET_DOCTORS = "Врачи"
SHEET_PRICES = "Цены"
SHEET_PREP = "Подготовка"
SHEET_SCHEDULE = "Расписание"
SHEET_REQUESTS = "Записи"
SHEET_INFO = "Инфо"
SHEET_SUBSCRIBERS = "Подписчики"

# =============================
# LOGGING
# =============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =============================
# GOOGLE SHEETS
# =============================

def gs_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_JSON),
        scopes=scopes
    )

    return gspread.authorize(creds)


def open_sheet(name):
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(name)


def get_records(sheet):
    try:
        ws = open_sheet(sheet)
        return ws.get_all_records()
    except Exception as e:
        logging.warning(f"Ошибка чтения {sheet}: {e}")
        return []

# =============================
# AI CLIENT
# =============================

ai_client = None

if OPENAI_API_KEY and OpenAI:
    try:
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        ai_client = None

# =============================
# MENU
# =============================

BTN_RECORD = "📅 Запись на приём"
BTN_PRICES = "🧾 Цены и анализы"
BTN_PREP = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RECORD, callback_data="RECORD")],
        [InlineKeyboardButton(BTN_PRICES, callback_data="PRICES")],
        [InlineKeyboardButton(BTN_PREP, callback_data="PREP")],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data="CONTACTS")]
    ])

# =============================
# START
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\nВыберите раздел:",
        reply_markup=main_menu()
    )

# =============================
# ВРАЧИ
# =============================

def doctors_search(query):

    rows = get_records(SHEET_DOCTORS)

    q = query.lower()

    result = []

    for r in rows:

        fio = str(r.get("ФИО",""))
        spec = str(r.get("Специальность",""))

        if q in fio.lower() or q in spec.lower():

            result.append(r)

    return result[:5]


def format_doctor(r):

    return f"""
👨‍⚕️ {r.get("ФИО","")}

Специальность: {r.get("Специальность","")}
Стаж: {r.get("Стаж","")}
Кабинет: {r.get("Кабинет","")}
График: {r.get("График приёма","")}

{r.get("Краткое био","")}
"""

# =============================
# АНАЛИЗЫ
# =============================

def prices_search(query):

    rows = get_records(SHEET_PRICES)

    q = query.lower()

    result = []

    for r in rows:

        name = str(r.get("Название",""))
        code = str(r.get("Код",""))

        if q in name.lower() or q == code.lower():

            result.append(r)

    return result[:10]


def format_price(r):

    return f"""
🧾 {r.get("Название","")}

Код: {r.get("Код","")}
Цена: {r.get("Цена","")}
Срок готовности: {r.get("Срок готовности","")}
{r.get("Примечание","")}
"""

# =============================
# ПОДГОТОВКА
# =============================

def prep_search(query):

    rows = get_records(SHEET_PREP)

    q = query.lower()

    result = []

    for r in rows:

        name = str(r.get("Анализ",""))

        if q in name.lower():

            result.append(r)

    return result[:5]

# =============================
# INFO
# =============================

def info_get(key):

    rows = get_records(SHEET_INFO)

    for r in rows:

        if r.get("Ключ") == key:
            return r.get("Значение","")

    return ""

# =============================
# AI ОТВЕТ
# =============================

def ai_answer(question):

    if not ai_client:
        return ""

    try:

        context = f"""
Ты справочный бот клиники.

Отвечай только по:
- анализам
- подготовке
- врачам
- записи
- контактам

Не ставь диагнозы и не назначай лечение.
"""

        r = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":context},
                {"role":"user","content":question}
            ],
            temperature=0.3
        )

        return r.choices[0].message.content.strip()

    except Exception as e:
        logging.warning(e)
        return ""

# =============================
# ROUTER
# =============================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()
    tl = text.lower()

    # поиск анализов
    prices = prices_search(text)

    if prices:

        for p in prices:
            await update.message.reply_text(format_price(p))

        return

    # подготовка
    prep = prep_search(text)

    if prep:

        for p in prep:

            await update.message.reply_text(
                f"ℹ️ {p.get('Анализ')}\n\n{p.get('Подготовка')}"
            )

        return

    # врачи
    docs = doctors_search(text)

    if docs:

        for d in docs:
            await update.message.reply_text(format_doctor(d))

        return

    # контакты
    if "адрес" in tl or "контакт" in tl:

        addr = info_get("clinic_address")
        phone = info_get("clinic_phone")
        hours = info_get("clinic_hours")

        await update.message.reply_text(

f"""📍 РГ Клиник

Адрес: {addr}
Телефон: {phone}
Режим работы: {hours}
"""
        )

        return

    # AI fallback
    ai = ai_answer(text)

    if ai:
        await update.message.reply_text(ai)
        return

    await update.message.reply_text(
        "Я не нашёл информацию. Попробуйте уточнить вопрос."
    )

# =============================
# MENU
# =============================

async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    data = q.data

    if data == "PRICES":

        await q.message.reply_text(
            "Введите название анализа или код."
        )

    if data == "PREP":

        await q.message.reply_text(
            "Введите название анализа."
        )

    if data == "CONTACTS":

        addr = info_get("clinic_address")
        phone = info_get("clinic_phone")
        hours = info_get("clinic_hours")

        await q.message.reply_text(
f"""📍 РГ Клиник

Адрес: {addr}
Телефон: {phone}
Режим работы: {hours}
"""
        )

# =============================
# MAIN
# =============================

def main():

    if not BOT_TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        CallbackQueryHandler(
            menu_click,
            pattern="^(PRICES|PREP|CONTACTS|RECORD)$"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    logging.info("Bot started")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
