# bot.py — МедНавигатор РГ Клиник (inline-меню)
# Требования: python-telegram-bot==20.8, gspread, google-auth, python-dateutil

import os
import json
import logging
from datetime import datetime
from dateutil.parser import parse as dt_parse, ParserError

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SHEET_NAME     = os.getenv("GOOGLE_SHEET_NAME", "Requests")
SA_JSON        = os.getenv("GOOGLE_SERVICE_ACCOUNT")  # весь JSON сервисного аккаунта

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)

# ==== Google Sheets client ====
def get_gs_client():
    if not SA_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT не задан")
    info = json.loads(SA_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def append_booking_row(values: list):
    if not SPREADSHEET_ID:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID не задан")

    gc = get_gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = None
    try:
        ws = sh.worksheet(SHEET_NAME)
    except Exception:
        ws = sh.sheet1  # fallback на первый лист

    ws.append_row(values, value_input_option="USER_ENTERED")

# ==== Тексты ====
WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Я — МедНавигатор РГ Клиник, ваш цифровой помощник.\n"
    "Выберите раздел ниже:"
)

HELP_TEXT = (
    "ℹ️ Я помогу:\n"
    "• узнать цены и сроки анализов\n"
    "• записаться к врачу\n"
    "• подготовиться к обследованиям\n"
    "• получить контакты клиник\n\n"
    "Нажмите /menu или выберите кнопку."
)

# ==== Кнопки ====
BTN_PRICES   = "🧾 Цены и анализы"
BTN_RECORD   = "📅 Запись на приём"
BTN_CONTACTS = "📍 Контакты"
BTN_PREP     = "ℹ️ Подготовка"

CB_PRICES   = "MENU_PRICES"
CB_RECORD   = "MENU_RECORD"
CB_CONTACTS = "MENU_CONTACTS"
CB_PREP     = "MENU_PREP"

def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(BTN_PRICES, callback_data=CB_PRICES)],
        [InlineKeyboardButton(BTN_RECORD, callback_data=CB_RECORD)],
        [InlineKeyboardButton(BTN_PREP, callback_data=CB_PREP)],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data=CB_CONTACTS)],
    ]
    return InlineKeyboardMarkup(rows)

# ==== Команды ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_kb())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_menu_kb())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

# ==== Обработчик меню ====
async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == CB_PRICES:
        text = (
            "🧾 *Цены и анализы*\n"
            "Напишите название или *код анализа* — подскажу цену и сроки.\n"
            "Примеры: `Глюкоза`, `ОАК`, `11-10-001`"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == CB_RECORD:
        text = (
            "📅 *Запись на приём*\n"
            "Отправьте в одном сообщении:\n"
            "*ФИО, телефон, врач/специализация, дата (ГГГГ-ММ-ДД), время (ЧЧ:ММ)*\n"
            "_Пример: Иванов И.И., +7..., Терапевт, 2025-10-25, 14:30_"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == CB_PREP:
        text = "ℹ️ *Подготовка к обследованиям*\nНапишите название анализа — пришлю памятку."
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == CB_CONTACTS:
        text = (
            "📍 *Контакты РГ Клиник*\n"
            "Адрес: ул. Примерная, 1\n"
            "Тел.: +7 (000) 000-00-00\n"
            "Режим: пн–пт 08:00–20:00, сб–вс 09:00–18:00"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

# ==== Парсер записи ====
def parse_booking(text: str):
    # ожидаем: ФИО, телефон, врач, дата, время
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 5:
        return None, "Сообщение должно содержать 5 полей через запятую: ФИО, телефон, врач, дата (ГГГГ-ММ-ДД), время (ЧЧ:ММ)."

    fio, phone, doctor, date_s, time_s = parts[:5]
    # базовые проверки даты/времени
    try:
        dt = dt_parse(f"{date_s} {time_s}")
    except ParserError:
        return None, "Не распознал дату/время. Формат: 2025-10-25, 14:30."

    return {
        "fio": fio,
        "phone": phone,
        "doctor": doctor,
        "date": dt.date().isoformat(),
        "time": dt.strftime("%H:%M"),
    }, None

# ==== Фолбэк: запись в таблицу ====
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    user = update.effective_user
    logging.info("Запрос: %s", txt)

    # пробуем распарсить как запись
    data, err = parse_booking(txt)
    if err:
        await update.message.reply_text(
            "Принял запрос 👍\n"
            "Чтобы записать на приём, введите в одном сообщении:\n"
            "ФИО, телефон, врач, 2025-10-25, 14:30",
            reply_markup=main_menu_kb()
        )
        return

    # формируем строку и пишем в таблицу
    values = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(user.id) if user else "",
        user.full_name if user else "",
        data["fio"],
        data["phone"],
        data["doctor"],
        data["date"],
        data["time"],
        "Новая",  # статус заявки
    ]

    try:
        append_booking_row(values)
        await update.message.reply_text("📝 Заявка принята, администратор свяжется с вами. Спасибо!", reply_markup=main_menu_kb())
    except Exception as e:
        logging.exception("Ошибка записи в Google Sheets")
        await update.message.reply_text(
            "⚠️ Не удалось записать в таблицу. Передам администратору.\n"
            "Сообщение отправлено в лог.",
            reply_markup=main_menu_kb()
        )

def main():
    if not BOT_TOKEN:
        raise SystemExit("❗ Переменная TELEGRAM_BOT_TOKEN не задана")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))

    # Инлайн-кнопки
    app.add_handler(CallbackQueryHandler(on_menu_click))

    # Любой текст → попытка записи в таблицу
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    logging.info("Бот запускается (polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()

