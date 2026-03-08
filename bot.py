import os
import json
import logging
import gspread

from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except Exception:
    ADMIN_ID = 0

SHEET_INFO = "Инфо"
SHEET_DOCTORS = "Врачи"
SHEET_SCHEDULE = "Расписание"
SHEET_PRICES = "Цены"
SHEET_PREP = "Подготовка"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def gs():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_JSON),
        scopes=scopes,
    )
    return gspread.authorize(creds)


def sheet(name: str):
    try:
        return gs().open_by_key(SPREADSHEET_ID).worksheet(name)
    except Exception as e:
        logging.exception("Ошибка открытия листа %s: %s", name, e)
        return None


def records(name: str):
    try:
        ws = sheet(name)
        if not ws:
            return []
        return ws.get_all_records()
    except Exception as e:
        logging.exception("Ошибка чтения листа %s: %s", name, e)
        return []


def normalize_text(value) -> str:
    return str(value or "").strip()


def info(key: str) -> str:
    rows = records(SHEET_INFO)

    for r in rows:
        k = normalize_text(r.get("Ключ") or r.get("key")).lower()
        if k == key.lower():
            return normalize_text(r.get("Значение") or r.get("value"))

    return ""


def doctors_list():
    rows = records(SHEET_DOCTORS)
    result = []

    for r in rows:
        name = normalize_text(r.get("ФИО") or r.get("doctor_name"))
        spec = normalize_text(r.get("Специальность") or r.get("specialty"))
        if name:
            if spec:
                result.append(f"{name} — {spec}")
            else:
                result.append(name)

    return result


def prices_list(limit: int = 10):
    rows = records(SHEET_PRICES)
    result = []

    for r in rows[:limit]:
        code = normalize_text(r.get("Код") or r.get("code"))
        name = normalize_text(r.get("Название") or r.get("name"))
        price = normalize_text(r.get("Цена") or r.get("price"))
        tat = normalize_text(r.get("Срок готовности") or r.get("tat_days"))
        note = normalize_text(r.get("Примечание") or r.get("notes"))

        if not name:
            continue

        line = f"{name}"
        if code:
            line += f" ({code})"
        if price:
            line += f" — {price}"
        if tat:
            line += f", срок: {tat}"
        if note:
            line += f"\n{note}"

        result.append(line)

    return result


def prep_search(query: str, limit: int = 5):
    rows = records(SHEET_PREP)
    q = query.lower().strip()
    found = []

    for r in rows:
        name = normalize_text(r.get("Анализ") or r.get("test_name"))
        memo = normalize_text(r.get("Подготовка") or r.get("memo"))

        if not name:
            continue

        if q in name.lower():
            found.append((name, memo))

        if len(found) >= limit:
            break

    return found


def free_doctors():
    rows = records(SHEET_SCHEDULE)
    docs = []

    for r in rows:
        status = normalize_text(r.get("status") or r.get("Статус")).upper()
        doctor = normalize_text(r.get("doctor_name") or r.get("Врач"))

        if status == "FREE" and doctor and doctor not in docs:
            docs.append(doctor)

    return docs


def dates_for_doctor(doctor: str):
    rows = records(SHEET_SCHEDULE)
    dates = []

    for r in rows:
        status = normalize_text(r.get("status") or r.get("Статус")).upper()
        doctor_name = normalize_text(r.get("doctor_name") or r.get("Врач"))
        date = normalize_text(r.get("date") or r.get("Дата"))

        if status == "FREE" and doctor_name == doctor and date and date not in dates:
            dates.append(date)

    return dates


def times_for_doctor_date(doctor: str, date: str):
    rows = records(SHEET_SCHEDULE)
    result = []

    for r in rows:
        status = normalize_text(r.get("status") or r.get("Статус")).upper()
        doctor_name = normalize_text(r.get("doctor_name") or r.get("Врач"))
        row_date = normalize_text(r.get("date") or r.get("Дата"))
        time = normalize_text(r.get("time") or r.get("Время"))
        slot_id = normalize_text(r.get("slot_id") or r.get("ID слота"))

        if status == "FREE" and doctor_name == doctor and row_date == date and slot_id:
            result.append({
                "slot_id": slot_id,
                "time": time,
            })

    return result


def book_slot(slot_id: str, patient_name: str, patient_phone: str) -> bool:
    try:
        ws = sheet(SHEET_SCHEDULE)
        if not ws:
            return False

        rows = ws.get_all_records()

        for i, r in enumerate(rows, start=2):
            row_slot_id = normalize_text(r.get("slot_id") or r.get("ID слота"))
            status = normalize_text(r.get("status") or r.get("Статус")).upper()

            if row_slot_id == slot_id:
                if status != "FREE":
                    return False

                header = ws.row_values(1)
                header_map = {str(h).strip(): idx + 1 for idx, h in enumerate(header)}

                status_col = header_map.get("status") or header_map.get("Статус")
                fio_col = header_map.get("patient_full_name") or header_map.get("Пациент")
                phone_col = header_map.get("patient_phone") or header_map.get("Телефон")

                if status_col:
                    ws.update_cell(i, status_col, "BOOKED")
                if fio_col:
                    ws.update_cell(i, fio_col, patient_name)
                if phone_col:
                    ws.update_cell(i, phone_col, patient_phone)

                return True

        return False

    except Exception as e:
        logging.exception("Ошибка записи слота: %s", e)
        return False


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Запись", callback_data="record")],
        [InlineKeyboardButton("👨‍⚕️ Врачи", callback_data="doctors")],
        [InlineKeyboardButton("🧾 Цены", callback_data="prices")],
        [InlineKeyboardButton("ℹ️ Подготовка", callback_data="prep")],
        [InlineKeyboardButton("📍 Контакты", callback_data="contacts")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 МедНавигатор РГ Клиник\n\nВыберите действие:",
        reply_markup=main_menu()
    )


async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "contacts":
        addr = info("clinic_address")
        phone = info("clinic_phone")
        hours = info("clinic_hours")

        text = (
            "📍 РГ Клиник\n\n"
            f"Адрес: {addr or 'Информация уточняется'}\n"
            f"Телефон: {phone or 'Информация уточняется'}\n"
            f"Режим работы: {hours or 'Информация уточняется'}"
        )
        await q.message.reply_text(text)
        return

    if data == "doctors":
        docs = doctors_list()
        if not docs:
            await q.message.reply_text("Список врачей пока не заполнен.")
            return

        text = "👨‍⚕️ В клинике работают:\n\n" + "\n".join(f"• {d}" for d in docs)
        await q.message.reply_text(text)
        return

    if data == "prices":
        items = prices_list()
        if not items:
            await q.message.reply_text("Прайс пока не заполнен.")
            return

        text = "🧾 Услуги и цены:\n\n" + "\n\n".join(f"• {x}" for x in items)
        await q.message.reply_text(text)
        return

    if data == "prep":
        await q.message.reply_text("Напишите название анализа или исследования, и я пришлю памятку по подготовке.")
        return

    if data == "record":
        docs = free_doctors()
        if not docs:
            await q.message.reply_text("Свободных слотов пока нет.")
            return

        kb = [[InlineKeyboardButton(d, callback_data=f"doc::{d}")] for d in docs]
        await q.message.reply_text("Выберите врача:", reply_markup=InlineKeyboardMarkup(kb))
        return


async def doctor_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    doctor = q.data.split("doc::", 1)[1]
    context.user_data["doctor"] = doctor

    dates = dates_for_doctor(doctor)
    if not dates:
        await q.message.reply_text("Для выбранного врача нет свободных дат.")
        return

    kb = [[InlineKeyboardButton(d, callback_data=f"date::{d}")] for d in dates]
    await q.message.reply_text("Выберите дату:", reply_markup=InlineKeyboardMarkup(kb))


async def date_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    date = q.data.split("date::", 1)[1]
    context.user_data["date"] = date
    doctor = context.user_data.get("doctor", "")

    slots = times_for_doctor_date(doctor, date)
    if not slots:
        await q.message.reply_text("На эту дату свободных слотов нет.")
        return

    kb = [
        [InlineKeyboardButton(s["time"] or s["slot_id"], callback_data=f"slot::{s['slot_id']}")]
        for s in slots
    ]
    await q.message.reply_text("Выберите время:", reply_markup=InlineKeyboardMarkup(kb))


async def slot_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    slot_id = q.data.split("slot::", 1)[1]
    context.user_data["slot_id"] = slot_id
    await q.message.reply_text("Введите ФИО пациента:")


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    msg_l = msg.lower()

    if "slot_id" in context.user_data and "patient_name" not in context.user_data:
        context.user_data["patient_name"] = msg
        await update.message.reply_text("Введите телефон:")
        return

    if "slot_id" in context.user_data and "patient_phone" not in context.user_data:
        context.user_data["patient_phone"] = msg

        ok = book_slot(
            context.user_data["slot_id"],
            context.user_data["patient_name"],
            context.user_data["patient_phone"],
        )

        if ok:
            await update.message.reply_text("✅ Вы записаны.")
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "Новая запись\n\n"
                            f"Пациент: {context.user_data['patient_name']}\n"
                            f"Телефон: {context.user_data['patient_phone']}\n"
                            f"Врач: {context.user_data.get('doctor', '')}\n"
                            f"Дата: {context.user_data.get('date', '')}\n"
                            f"slot_id: {context.user_data.get('slot_id', '')}"
                        )
                    )
                except Exception as e:
                    logging.exception("Ошибка уведомления админа: %s", e)
        else:
            await update.message.reply_text("Не удалось записать: слот уже занят или недоступен.")

        context.user_data.clear()
        return

    if "руководител" in msg_l or "главный врач" in msg_l:
        manager = info("clinic_manager")
        await update.message.reply_text(
            f"Главный врач клиники:\n{manager or 'Информация уточняется'}"
        )
        return

    if "адрес" in msg_l:
        await update.message.reply_text(info("clinic_address") or "Информация уточняется")
        return

    if "телефон" in msg_l or "номер" in msg_l:
        await update.message.reply_text(info("clinic_phone") or "Информация уточняется")
        return

    if "режим" in msg_l or "график" in msg_l or "работает" in msg_l:
        await update.message.reply_text(info("clinic_hours") or "Информация уточняется")
        return

    if "врач" in msg_l or "специалист" in msg_l:
        docs = doctors_list()
        if docs:
            text = "👨‍⚕️ В клинике работают:\n\n" + "\n".join(f"• {d}" for d in docs)
        else:
            text = "Список врачей пока не заполнен."
        await update.message.reply_text(text)
        return

    if "цена" in msg_l or "стоимость" in msg_l or "сколько стоит" in msg_l:
        items = prices_list()
        if items:
            text = "🧾 Услуги и цены:\n\n" + "\n\n".join(f"• {x}" for x in items)
        else:
            text = "Прайс пока не заполнен."
        await update.message.reply_text(text)
        return

    prep_items = prep_search(msg)
    if prep_items:
        text = "\n\n".join(f"ℹ️ {name}\n{memo}" for name, memo in prep_items)
        await update.message.reply_text(text)
        return

    await update.message.reply_text(
        "Я помогаю по вопросам клиники: запись, врачи, цены, подготовка, контакты.",
        reply_markup=main_menu()
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_click, pattern="^(record|doctors|prices|prep|contacts)$"))
    app.add_handler(CallbackQueryHandler(doctor_click, pattern=r"^doc::"))
    app.add_handler(CallbackQueryHandler(date_click, pattern=r"^date::"))
    app.add_handler(CallbackQueryHandler(slot_click, pattern=r"^slot::"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logging.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
