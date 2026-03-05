from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from config import BOT_TOKEN, ADMIN_ID
from sheets import records
from booking import free_slots, book_slot
from ai import ai_answer


BTN_RECORD = "📅 Запись"
BTN_CONTACTS = "📍 Контакты"

def menu():

    return InlineKeyboardMarkup([

        [InlineKeyboardButton(BTN_RECORD, callback_data="record")],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data="contacts")]

    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "МедНавигатор РГ Клиник",
        reply_markup=menu()
    )


async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    if q.data == "record":

        slots = free_slots()

        if not slots:

            await q.message.reply_text("Свободных слотов нет")
            return

        kb = []

        for s in slots[:10]:

            txt = f"{s['doctor_name']} {s['date']} {s['time']}"

            kb.append(
                [InlineKeyboardButton(txt, callback_data=f"slot_{s['slot_id']}")]
            )

        await q.message.reply_text(
            "Выберите время",
            reply_markup=InlineKeyboardMarkup(kb)
        )


async def slot_click(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    slot_id = q.data.replace("slot_","")

    context.user_data["slot"] = slot_id

    await q.message.reply_text("Введите ФИО")


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if "slot" in context.user_data and "name" not in context.user_data:

        context.user_data["name"] = update.message.text

        await update.message.reply_text("Введите телефон")

        return


    if "slot" in context.user_data and "phone" not in context.user_data:

        context.user_data["phone"] = update.message.text

        ok = book_slot(
            context.user_data["slot"],
            context.user_data["name"],
            context.user_data["phone"]
        )

        if ok:

            await update.message.reply_text("Вы записаны")

            if ADMIN_ID:

                await context.bot.send_message(
                    ADMIN_ID,
                    f"""
Новая запись

Пациент: {context.user_data['name']}
Телефон: {context.user_data['phone']}
"""
                )

        context.user_data.clear()

        return


    ai = ai_answer(update.message.text)

    if ai:

        await update.message.reply_text(ai)


def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(menu_click, pattern="record"))

    app.add_handler(CallbackQueryHandler(slot_click, pattern="slot_"))

    app.add_handler(MessageHandler(filters.TEXT, text))

    print("Bot started")

    app.run_polling()


if __name__ == "__main__":

    main()
