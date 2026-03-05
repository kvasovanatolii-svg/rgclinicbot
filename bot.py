import os
import json
import logging

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# -------------------------
# ENV
# -------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# -------------------------
# SHEETS
# -------------------------

SHEET_SCHEDULE = "Расписание"
SHEET_INFO = "Инфо"

# -------------------------
# LOGGING
# -------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# -------------------------
# GOOGLE
# -------------------------

def gs():

    try:

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        creds = Credentials.from_service_account_info(
            json.loads(SERVICE_JSON),
            scopes=scopes
        )

        return gspread.authorize(creds)

    except Exception as e:

        logging.error("Google auth error", e)

        return None


def sheet(name):

    try:

        gc = gs()

        if not gc:
            return None

        return gc.open_by_key(SPREADSHEET_ID).worksheet(name)

    except Exception as e:

        logging.error("Sheet error", e)

        return None


def records(name):

    try:

        ws = sheet(name)

        if not ws:
            return []

        return ws.get_all_records()

    except:

        return []

# -------------------------
# INFO
# -------------------------

def info(key):

    rows = records(SHEET_INFO)

    for r in rows:

        k = str(r.get("Ключ","")).strip()

        if k == key:

            return str(r.get("Значение","")).strip()

    return ""

# -------------------------
# MENU
# -------------------------

def menu():

    return InlineKeyboardMarkup([

        [InlineKeyboardButton("📅 Запись",callback_data="record")],
        [InlineKeyboardButton("📍 Контакты",callback_data="contacts")]

    ])

# -------------------------
# START
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(

        "👋 МедНавигатор РГ Клиник\n\n"
        "Выберите действие:",

        reply_markup=menu()

    )

# -------------------------
# DOCTORS
# -------------------------

def doctors():

    rows = records(SHEET_SCHEDULE)

    docs = []

    for r in rows:

        if str(r.get("status","")).upper() == "FREE":

            d = r.get("doctor_name")

            if d and d not in docs:

                docs.append(d)

    return docs

# -------------------------
# DATES
# -------------------------

def dates(doctor):

    rows = records(SHEET_SCHEDULE)

    result = []

    for r in rows:

        if r.get("doctor_name") == doctor and str(r.get("status")).upper() == "FREE":

            d = r.get("date")

            if d not in result:

                result.append(d)

    return result

# -------------------------
# TIMES
# -------------------------

def times(doctor,date):

    rows = records(SHEET_SCHEDULE)

    result = []

    for r in rows:

        if (
            r.get("doctor_name") == doctor
            and r.get("date") == date
            and str(r.get("status")).upper() == "FREE"
        ):

            result.append(r)

    return result

# -------------------------
# BOOK
# -------------------------

def book(slot_id,name,phone):

    try:

        ws = sheet(SHEET_SCHEDULE)

        rows = ws.get_all_records()

        for i,r in enumerate(rows,start=2):

            if str(r.get("slot_id")) == str(slot_id):

                if str(r.get("status")).upper() != "FREE":

                    return False

                ws.update_cell(i,8,"BOOKED")
                ws.update_cell(i,9,name)
                ws.update_cell(i,10,phone)

                return True

        return False

    except Exception as e:

        logging.error("Booking error",e)

        return False

# -------------------------
# MENU CLICK
# -------------------------

async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    data = q.data

    try:

        if data == "contacts":

            addr = info("clinic_address")
            phone = info("clinic_phone")
            hours = info("clinic_hours")

            await q.message.reply_text(

f"""📍 РГ Клиник

Адрес: {addr}
Телефон: {phone}
Режим работы: {hours}
"""
            )

        if data == "record":

            docs = doctors()

            if not docs:

                await q.message.reply_text("Нет свободных слотов")

                return

            kb = []

            for d in docs:

                kb.append([InlineKeyboardButton(d,callback_data=f"doc_{d}")])

            await q.message.reply_text(

                "Выберите врача",

                reply_markup=InlineKeyboardMarkup(kb)

            )

    except Exception as e:

        logging.error(e)

# -------------------------
# DOCTOR CLICK
# -------------------------

async def doctor_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    doctor = q.data.replace("doc_","")

    context.user_data["doctor"] = doctor

    ds = dates(doctor)

    kb = []

    for d in ds:

        kb.append([InlineKeyboardButton(d,callback_data=f"date_{d}")])

    await q.message.reply_text(

        "Выберите дату",

        reply_markup=InlineKeyboardMarkup(kb)

    )

# -------------------------
# DATE CLICK
# -------------------------

async def date_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    date = q.data.replace("date_","")

    context.user_data["date"] = date

    doctor = context.user_data.get("doctor")

    ts = times(doctor,date)

    kb = []

    for t in ts:

        kb.append([
            InlineKeyboardButton(
                t.get("time"),
                callback_data=f"slot_{t.get('slot_id')}"
            )
        ])

    await q.message.reply_text(

        "Выберите время",

        reply_markup=InlineKeyboardMarkup(kb)

    )

# -------------------------
# SLOT CLICK
# -------------------------

async def slot_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    context.user_data["slot"] = q.data.replace("slot_","")

    await q.message.reply_text("Введите ФИО")

# -------------------------
# TEXT
# -------------------------

async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        if "slot" in context.user_data and "name" not in context.user_data:

            context.user_data["name"] = update.message.text

            await update.message.reply_text("Введите телефон")

            return

        if "slot" in context.user_data and "phone" not in context.user_data:

            context.user_data["phone"] = update.message.text

            ok = book(

                context.user_data["slot"],
                context.user_data["name"],
                context.user_data["phone"]

            )

            if ok:

                await update.message.reply_text("✅ Вы записаны")

                if ADMIN_ID:

                    await context.bot.send_message(

                        ADMIN_ID,

f"""Новая запись

Пациент: {context.user_data['name']}
Телефон: {context.user_data['phone']}
"""
                    )

            else:

                await update.message.reply_text("Слот уже занят")

            context.user_data.clear()

            return

        await update.message.reply_text(

            "Используйте кнопки меню 👇",

            reply_markup=menu()

        )

    except Exception as e:

        logging.error(e)

# -------------------------
# MAIN
# -------------------------

def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    app.add_handler(
        CallbackQueryHandler(menu_click,pattern="^(record|contacts)$")
    )

    app.add_handler(
        CallbackQueryHandler(doctor_click,pattern="^doc_")
    )

    app.add_handler(
        CallbackQueryHandler(date_click,pattern="^date_")
    )

    app.add_handler(
        CallbackQueryHandler(slot_click,pattern="^slot_")
    )

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND,text)
    )

    print("Bot started")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":

    main()
