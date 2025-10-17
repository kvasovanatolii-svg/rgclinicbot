# bot.py — МедНавигатор РГ Клиник (прототип)
# Зависимость: python-telegram-bot >= 20
# Запуск: 
#   1) pip install -r requirements.txt
#   2) Вставьте токен в BOT_TOKEN или установите переменную окружения TELEGRAM_BOT_TOKEN
#   3) python bot.py

import os
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

# === ВСТАВЬТЕ ТОКЕН ЗДЕСЬ (или используйте переменную окружения TELEGRAM_BOT_TOKEN) ===
BOT_TOKEN = "8132514036:AAHLzQzgBXfDe2rU7Iaorhj_KgSGGJSDRh0"


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
    "Я могу:\n"
    "• рассказать о стоимости и сроках анализов (/price)\n"
    "• помочь записаться к врачу (/record)\n"
    "• напомнить, как подготовиться к обследованию (/analysis)\n"
    "• отправить контакты и адреса клиник (/contacts)\n\n"
    "Используйте /menu чтобы вернуться в главное меню."
)

MENU_TEXT = (
    "📋 *Меню*\n"
    "• /analysis — Анализы, цены и подготовка\n"
    "• /record — Запись на приём\n"
    "• /price — Просмотр цен на услуги\n"
    "• /contacts — Контакты клиник\n"
    "• /help — Помощь"
)

# === Команды ===

async def start(update, context):
    await update.message.reply_text(WELCOME_TEXT)

async def help_command(update, context):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def menu(update, context):
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")

async def analysis(update, context):
    await update.message.reply_text(
        "🧪 Напишите название или *код анализа* — подскажу цену и условия подготовки.\n"
        "Пример: *Глюкоза* или *23-12-001*",
        parse_mode="Markdown",
    )

async def price(update, context):
    await update.message.reply_text(
        "💳 Для просмотра цены напишите *название услуги* или *код анализа*.\n"
        "Пример: *Общий анализ крови* или *11-10-001*",
        parse_mode="Markdown",
    )

async def record(update, context):
    await update.message.reply_text(
        "🗓 Для записи отправьте в одном сообщении:\n"
        "*ФИО пациента, телефон, ФИО врача/специализация, дата (ГГГГ-ММ-ДД), время (ЧЧ:ММ)*\n"
        "Пример: _Иванов И.И., +7..., Петров П.П., 2025-10-20, 14:30_",
        parse_mode="Markdown",
    )

async def contacts(update, context):
    await update.message.reply_text(
        "📍 РГ Клиник\n"
        "Адрес: ул. Примерная, д. 1\n"
        "Тел.: +7 (000) 000-00-00\n"
        "Часы работы: пн–пт 08:00–20:00, сб–вс 09:00–18:00\n"
        "Напишите, если нужны подробности по подразделениям."
    )

# Обработчик произвольного текста (эко-ответ на прототипе)
async def fallback_text(update, context):
    user_text = (update.message.text or '').strip()
    # Здесь позже подключим «МедНавигатор» к данным РГ Клиник (прайс/расписание)
    await update.message.reply_text(
        "Принял запрос: \"{0}\"\n"
        "Скоро подключу поиск по базе РГ Клиник и запись на приём автоматически 👍".format(user_text)
    )


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("❗ Укажите токен бота в BOT_TOKEN или TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("analysis", analysis))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("record", record))
    app.add_handler(CommandHandler("contacts", contacts))

    # Произвольный текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    # Запуск
    print("✅ Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
