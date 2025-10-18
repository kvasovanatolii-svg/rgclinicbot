# bot.py — МедНавигатор РГ Клиник (универсальный, polling)
# Требования: python-telegram-bot >= 20,<21

import os
import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

# === Токен берём из переменной окружения TELEGRAM_BOT_TOKEN ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Логирование (видно в логах хостинга)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)

WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Я — МедНавигатор РГ Клиник, ваш цифровой помощник.\n\n"
    "Чем могу помочь сегодня?\n"
    "1️⃣ Узнать цену или срок анализа (/analysis)\n"
    "2️⃣ Записаться на приём (/record)\n"
    "3️⃣ Подготовка к анализам (/analysis)\n"
    "4️⃣ Контакты и график работы (/contacts)\n\n"
    "Просто выберите пункт или напишите ваш вопрос ✍️"
)

HELP_TEXT = (
    "ℹ️ *Помощь*\n\n"
    "• /analysis — Анализы, цены и подготовка\n"
    "• /record — Запись на приём\n"
    "• /price — Цены на услуги\n"
    "• /contacts — Контакты клиник\n"
    "• /menu — Главное меню"
)

MENU_TEXT = (
    "📋 *Меню*\n"
    "• /analysis — Анализы, цены и подготовка\n"
    "• /record — Запись на приём\n"
    "• /price — Цены на услуги\n"
    "• /contacts — Контакты\n"
    "• /help — Помощь"
)

async def start(update, context):
    logging.info("Получен /start от chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(WELCOME_TEXT)

async def help_command(update, context):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def menu(update, context):
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")

async def analysis(update, context):
    await update.message.reply_text(
        "🧪 Напишите название или *код анализа* — подскажу цену и подготовку.\n"
        "Пример: *Глюкоза* или *23-12-001*",
        parse_mode="Markdown",
    )

async def price(update, context):
    await update.message.reply_text(
        "💳 Назовите *услугу* или *код анализа* — покажу цену.",
        parse_mode="Markdown",
    )

async def record(update, context):
    await update.message.reply_text(
        "🗓 Для записи отправьте:\n"
        "*ФИО пациента, телефон, врач/спец., дата (ГГГГ-ММ-ДД), время (ЧЧ:ММ)*\n"
        "Пример: _Иванов И.И., +7..., Петров П.П., 2025-10-20, 14:30_",
        parse_mode="Markdown",
    )

async def contacts(update, context):
    await update.message.reply_text(
        "📍 РГ Клиник\nАдрес: ул. Примерная, д.1\nТел.: +7 (000) 000-00-00\n"
        "Режим: пн–пт 08:00–20:00, сб–вс 09:00–18:00"
    )

async def fallback_text(update, context):
    txt = (update.message.text or "").strip()
    logging.info("Текст от %s: %s", update.effective_user.id, txt)
    await update.message.reply_text(
        "Принял запрос 👍 Скоро подключу поиск по базе РГ Клиник и запись автоматически."
    )

def main():
    if not BOT_TOKEN:
        raise SystemExit("❗ Переменная окружения TELEGRAM_BOT_TOKEN не задана")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("analysis", analysis))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("record", record))
    app.add_handler(CommandHandler("contacts", contacts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    logging.info("Бот запускается (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)  # работает на Render BW / Railway / платный PA

if __name__ == "__main__":
    from telegram import Update  # импорт здесь, чтобы не мешал статическому анализу
    main()
