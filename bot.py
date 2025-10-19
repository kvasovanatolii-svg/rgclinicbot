# bot.py — МедНавигатор РГ Клиник (inline-меню)
# Требования: python-telegram-bot==20.8

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)

# --- Тексты ---
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
    "Нажмите нужную кнопку ниже или команду /menu."
)

# --- Кнопки меню ---
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

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_kb())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_menu_kb())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

# --- Обработчик нажатий на кнопки ---
async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()  # короткий ответ клиенту (обязателен для UX)

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
            "*ФИО пациента, телефон, врач/специализация, дата (ГГГГ-ММ-ДД), время (ЧЧ:ММ)*\n"
            "_Пример: Иванов И.И., +7..., Терапевт, 2025-10-25, 14:30_"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == CB_PREP:
        text = (
            "ℹ️ *Подготовка к обследованиям*\n"
            "Напишите название анализа/исследования — отправлю памятку по подготовке."
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == CB_CONTACTS:
        text = (
            "📍 *Контакты РГ Клиник*\n"
            "Адрес: ул. Примерная, 1\n"
            "Тел.: +7 (000) 000-00-00\n"
            "Режим: пн–пт 08:00–20:00, сб–вс 09:00–18:00"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

# --- Фолбэк для произвольного текста (пока заглушка) ---
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    logging.info("Запрос: %s", txt)
    await update.message.reply_text(
        "Принял запрос 👍 Скоро подключу поиск по прайсу и запись в расписание.",
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

    # Любой текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    logging.info("Бот запускается (polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()


