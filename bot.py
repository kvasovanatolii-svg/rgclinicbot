import os
import logging
import json
import re
from datetime import datetime

import gspread
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

if not SPREADSHEET_ID:
    raise RuntimeError("GOOGLE_SHEETS_ID not set")

if not SERVICE_JSON:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")


creds = json.loads(SERVICE_JSON)
gc = gspread.service_account_from_dict(creds)
sh = gc.open_by_key(SPREADSHEET_ID)

sheet_doctors = sh.worksheet("Врачи")
sheet_prices = sh.worksheet("Цены")
sheet_prep = sh.worksheet("Подготовка")
sheet_schedule = sh.worksheet("Расписание")
sheet_requests = sh.worksheet("Записи")
sheet_info = sh.worksheet("Инфо")

BOOK_DOCTOR, BOOK_SLOT, BOOK_NAME, BOOK_PHONE = range(4)


menu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📅 Запись"), KeyboardButton("👨‍⚕️ Врачи")],
        [KeyboardButton("🧾 Цены"), KeyboardButton("ℹ️ Подготовка")],
        [KeyboardButton("📍 Контакты")],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 МедНавигатор РГ Клиник\n\nВыберите действие:",
        reply_markup=menu,
    )


def get_info(key):
    rows = sheet_info.get_all_records()
    for r in rows:
        if r["Ключ"] == key:
            return r["Значение"]
    return None


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):

    address = get_info("clinic_address")
    phone = get_info("clinic_phone")
    hours = get_info("clinic_hours")

    text = f"""
📍 Контакты

Адрес: {address}
Телефон: {phone}
Режим работы: {hours}
"""

    await update.message.reply_text(text, reply_markup=menu)


async def doctors(update: Update, context: ContextTypes.DEFAULT_TYPE):

    rows = sheet_doctors.get_all_records()

    text = "👨‍⚕️ Врачи клиники:\n\n"

    for r in rows:

        text += f"""
{r["ФИО"]}
Специальность: {r["Специальность"]}
Стаж: {r["Стаж"]}
"""

    await update.message.reply_text(text, reply_markup=menu)


async def prices(update: Update, context: ContextTypes.DEFAULT_TYPE):

    rows = sheet_prices.get_all_records()[:10]

    text = "🧾 Цены:\n\n"

    for r in rows:
        text += f'{r["Название"]} — {r["Цена"]}\n'

    await update.message.reply_text(text, reply_markup=menu)


async def prep(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Введите название анализа или услуги",
    )

    context.user_data["prep"] = True


async def prep_search(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.user_data.get("prep"):
        return

    query = update.message.text.lower()

    rows = sheet_prep.get_all_records()

    for r in rows:
        if query in r["Анализ"].lower():

            await update.message.reply_text(
                f"""
{r["Анализ"]}

{r["Подготовка"]}
""",
                reply_markup=menu,
            )

            context.user_data["prep"] = False
            return

    await update.message.reply_text(
        "Подготовка не найдена. Уточните у администратора.",
        reply_markup=menu,
    )

    context.user_data["prep"] = False


def free_slots():

    rows = sheet_schedule.get_all_records()

    slots = []

    for r in rows:
        if r["Статус"] == "FREE":

            slots.append(r)

    return slots


async def booking(update: Update, context: ContextTypes.DEFAULT_TYPE):

    slots = free_slots()

    doctors = list(set([s["Врач"] for s in slots]))

    keyboard = []

    for i, d in enumerate(doctors):
        keyboard.append(
            [InlineKeyboardButton(d, callback_data=f"doc_{i}")]
        )

    context.user_data["doctors"] = doctors

    await update.message.reply_text(
        "Выберите врача",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return BOOK_DOCTOR


async def choose_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    i = int(query.data.split("_")[1])

    doctor = context.user_data["doctors"][i]

    slots = [s for s in free_slots() if s["Врач"] == doctor]

    context.user_data["slots"] = slots

    keyboard = []

    for i, s in enumerate(slots):

        keyboard.append(
            [
                InlineKeyboardButton(
                    f'{s["Дата"]} {s["Время"]}',
                    callback_data=f"slot_{i}",
                )
            ]
        )

    await query.edit_message_text(
        "Выберите время",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return BOOK_SLOT


async def choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    i = int(query.data.split("_")[1])

    slot = context.user_data["slots"][i]

    context.user_data["slot"] = slot

    await query.edit_message_text(
        f'Вы выбрали {slot["Дата"]} {slot["Время"]}\n\nВведите ФИО пациента'
    )

    return BOOK_NAME


async def name(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["name"] = update.message.text

    await update.message.reply_text("Введите телефон")

    return BOOK_PHONE


async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):

    phone = update.message.text
    name = context.user_data["name"]
    slot = context.user_data["slot"]

    rows = sheet_schedule.get_all_records()

    for i, r in enumerate(rows, start=2):

        if r["ID слота"] == slot["ID слота"]:

            sheet_schedule.update(
                f"F{i}:H{i}",
                [["BOOKED", name, phone]],
            )

    sheet_requests.append_row(
        [
            datetime.now().isoformat(),
            name,
            phone,
            slot["Врач"],
            slot["Дата"],
            slot["Время"],
            "Новая",
        ]
    )

    await update.message.reply_text(
        "✅ Вы успешно записаны",
        reply_markup=menu,
    )

    return ConversationHandler.END


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "👨‍⚕️ Врачи":
        await doctors(update, context)

    elif text == "🧾 Цены":
        await prices(update, context)

    elif text == "📍 Контакты":
        await contacts(update, context)

    elif text == "ℹ️ Подготовка":
        await prep(update, context)

    elif text == "📅 Запись":
        return await booking(update, context)

    else:
        await prep_search(update, context)


def main():

    app = Application.builder().token(TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📅 Запись"), booking)],
        states={
            BOOK_DOCTOR: [CallbackQueryHandler(choose_doctor)],
            BOOK_SLOT: [CallbackQueryHandler(choose_slot)],
            BOOK_NAME: [MessageHandler(filters.TEXT, name)],
            BOOK_PHONE: [MessageHandler(filters.TEXT, phone)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(booking_conv)
    app.add_handler(MessageHandler(filters.TEXT, text_router))

    app.run_polling()


if __name__ == "__main__":
    main()
