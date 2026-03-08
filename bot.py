import os
import json
import logging
from typing import Any

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

# Названия листов — должны совпадать 1 в 1
SHEET_INFO = "Инфо"
SHEET_DOCTORS = "Врачи"
SHEET_PRICES = "Цены"
SHEET_PREP = "Подготовка"
SHEET_SCHEDULE = "Расписание"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# -------------------- helpers --------------------

def norm(v: Any) -> str:
    return str(v or "").strip()


def safe_lower(v: Any) -> str:
    return norm(v).lower()


def first_of(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row and norm(row.get(k)):
            return norm(row.get(k))
    return ""


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
        logging.exception("Не удалось открыть лист %s: %s", name, e)
        return None


def records(name: str) -> list[dict]:
    try:
        ws = sheet(name)
        if not ws:
            return []
        rows = ws.get_all_records()
        logging.info("Лист %s: прочитано %s строк", name, len(rows))
        return rows
    except Exception as e:
        logging.exception("Ошибка чтения листа %s: %s", name, e)
        return []


# -------------------- data access --------------------

def info_value(key: str) -> str:
    rows = records(SHEET_INFO)
    key_l = key.lower()

    for r in rows:
        k = first_of(r, "Ключ", "ключ", "key").lower()
        if k == key_l:
            return first_of(r, "Значение", "значение", "value")

    return ""


def doctors_list() -> list[str]:
    rows = records(SHEET_DOCTORS)
    result = []

    for r in rows:
        name = first_of(r, "ФИО", "фио", "doctor_name", "name")
        spec = first_of(r, "Специальность", "специальность", "specialty")

        if name:
            result.append(f"{name} — {spec}" if spec else name)

    return result


def prices_list(limit: int = 10) -> list[str]:
    rows = records(SHEET_PRICES)
    result = []

    for r in rows[:limit]:
        code = first_of(r, "Код", "код", "code")
        name = first_of(r, "Название", "название", "name")
        price = first_of(r, "Цена", "цена", "price")
        tat = first_of(r, "Срок готовности", "tat_days")
        note = first_of(r, "Примечание", "notes")

        if not name:
            continue

        line = name
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


def prep_search(query: str, limit: int = 5) -> list[tuple[str, str]]:
    rows = records(SHEET_PREP)
    q = safe_lower(query)
    found = []

    for r in rows:
        name = first_of(r, "Анализ", "анализ", "test_name", "name")
        memo = first_of(r, "Подготовка", "подготовка", "memo")

        if name and q in safe_lower(name):
            found.append((name, memo))
            if len(found) >= limit:
                break

    return found


def free_doctors() -> list[str]:
    rows = records(SHEET_SCHEDULE)
    result = []

    for r in rows:
        status = first_of(r, "status", "Статус", "статус").upper()
        doctor = first_of(r, "doctor_name", "Врач", "врач")

        if status == "FREE" and doctor and doctor not in result:
            result.append(doctor)

    return result


def dates_for_doctor(doctor: str) -> list[str]:
    rows = records(SHEET_SCHEDULE)
    result = []

    for r in rows:
        status = first_of(r, "status", "Статус").upper()
        doctor_name = first_of(r, "doctor_name", "Врач")
        date = first_of(r, "date", "Дата")

        if status == "FREE" and doctor_name == doctor and date and date not in result:
            result.append(date)

    return result


def times_for_doctor_date(doctor: str, date: str) -> list[dict]:
    rows = records(SHEET_SCHEDULE)
    result = []

    for r in rows:
        status = first_of(r, "status", "Статус").upper()
        doctor_name = first_of(r, "doctor_name", "Врач")
        row_date = first_of(r, "date", "Дата")
        time = first_of(r, "time", "Время")
        slot_id = first_of(r, "slot_id", "ID слота", "id слота")

        if status == "FREE" and doctor_name == doctor and row_date == date and slot_id:
            result.append({"slot_id": slot_id, "time": time or slot_id})

    return result


def book_slot(slot_id: str, patient_name: str, patient_phone: str) -> bool:
    try:
        ws = sheet(SHEET_SCHEDULE)
        if not ws:
            return False

        rows = ws.get_all_records()
        header = ws.row_values(1)
        hmap = {norm(h): i + 1 for i, h in enumerate(header)}

        status_col = hmap.get("status") or hmap.get("Статус")
        fio_col = hmap.get("patient_full_name") or hmap.get("Пациент")
        phone_col = hmap.get("patient_phone") or hmap.get("Телефон")

        for i, r in enumerate(rows, start=2):
            row_slot_id = first_of(r, "slot_id", "ID слота")
            row_status = first_of(r, "status", "Статус").upper()

            if row_slot_id == slot_id:
                if row_status != "FREE":
                    return False

                if status_col:
                    ws.update_cell(i, status_col, "BOOKED")
                if fio_col:
                    ws.update_cell(i, fio_col, patient_name)
                if phone_col:
                    ws.update_cell(i, phone_col, patient_phone)
                return True

        return False

    except Exception as e:
        logging.exception("Ошибка записи в слот: %s", e)
        return False


# -------------------- menu --------------------

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Запись", callback_data="record")],
        [InlineKeyboardButton("👨‍⚕️ Врачи", callback_data="doctors")],
        [InlineKeyboardButton("🧾 Цены", callback_data="prices")],
        [InlineKeyboardButton("ℹ️ Подготовка", callback_data="prep")],
        [InlineKeyboardButton("📍 Контакты", callback_data="contacts")],
    ])


# -------------------- handlers --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 МедНавигатор РГ Клиник\n\nВыберите действие:",
        reply_markup=main_menu(),
    )


async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    logging.info("Нажата кнопка: %s", data)

    try:
        if data == "contacts":
            addr = info_value("clinic_address")
            phone = info_value("clinic_phone")
            hours = info_value("clinic_hours")

            await q.message.reply_text(
                "📍 РГ Клиник\n\n"
                f"Адрес: {addr or 'Информация уточняется'}\n"
                f"Телефон: {phone or 'Информация уточняется'}\n"
                f"Режим работы: {hours or 'Информация уточняется'}"
            )
            return

        if data == "doctors":
            docs = doctors_list()
            if not docs:
                await q.message.reply_text("Список врачей пока не заполнен.")
                return

            await q.message.reply_text(
                "👨‍⚕️ В клинике работают:\n\n" + "\n".join(f"• {d}" for d in docs)
            )
            return

        if data == "prices":
            items = prices_list()
            if not items:
                await q.message.reply_text("Прайс пока не заполнен.")
                return

            await q.message.reply_text(
                "🧾 Услуги и цены:\n\n" + "\n\n".join(f"• {x}" for x in items)
            )
            return

        if data == "prep":
            await q.message.reply_text(
                "Напишите название анализа или исследования, и я пришлю памятку по подготовке."
            )
            return

        if data == "record":
            docs = free_doctors()
            if not docs:
                await q.message.reply_text("Свободных слотов пока нет.")
                return

            kb = [[InlineKeyboardButton(d, callback_data=f"doc::{d}")] for d in docs]
            await q.message.reply_text(
                "Выберите врача:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        await q.message.reply_text("Неизвестная команда меню.")
    except Exception as e:
        logging.exception("Ошибка в menu_click: %s", e)
        await q.message.reply_text("Не удалось обработать кнопку. Проверьте таблицу и попробуйте снова.")


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
        [InlineKeyboardButton(s["time"], callback_data=f"slot::{s['slot_id']}")]
        for s in slots
    ]
    await q.message.reply_text("Выберите время:", reply_markup=InlineKeyboardMarkup(kb))


async def slot_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data["slot_id"] = q.data.split("slot::", 1)[1]
    await q.message.reply_text("Введите ФИО пациента:")


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = norm(update.message.text)
    msg_l = msg.lower()

    # Запись
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
                        ),
                    )
                except Exception as e:
                    logging.exception("Ошибка уведомления администратору: %s", e)
        else:
            await update.message.reply_text("Не удалось записать: слот уже занят или недоступен.")

        context.user_data.clear()
        return

    # Справка
    if "руководител" in msg_l or "главный врач" in msg_l:
        manager = info_value("clinic_manager")
        await update.message.reply_text(
            f"Главный врач клиники:\n{manager or 'Информация уточняется'}"
        )
        return

    if "адрес" in msg_l:
        await update.message.reply_text(info_value("clinic_address") or "Информация уточняется")
        return

    if "телефон" in msg_l or "номер" in msg_l:
        await update.message.reply_text(info_value("clinic_phone") or "Информация уточняется")
        return

    if "режим" in msg_l or "график" in msg_l or "работает" in msg_l:
        await update.message.reply_text(info_value("clinic_hours") or "Информация уточняется")
        return

    if "врач" in msg_l or "специалист" in msg_l:
        docs = doctors_list()
        if docs:
            await update.message.reply_text(
                "👨‍⚕️ В клинике работают:\n\n" + "\n".join(f"• {d}" for d in docs)
            )
        else:
            await update.message.reply_text("Список врачей пока не заполнен.")
        return

    if "цена" in msg_l or "стоимость" in msg_l or "сколько стоит" in msg_l:
        items = prices_list()
        if items:
            await update.message.reply_text(
                "🧾 Услуги и цены:\n\n" + "\n\n".join(f"• {x}" for x in items)
            )
        else:
            await update.message.reply_text("Прайс пока не заполнен.")
        return

    prep_items = prep_search(msg)
    if prep_items:
        await update.message.reply_text(
            "\n\n".join(f"ℹ️ {name}\n{memo}" for name, memo in prep_items)
        )
        return

    await update.message.reply_text(
        "Я помогаю по вопросам клиники: запись, врачи, цены, подготовка, контакты.",
        reply_markup=main_menu(),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Необработанная ошибка: %s", context.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_click, pattern=r"^(record|doctors|prices|prep|contacts)$"))
    app.add_handler(CallbackQueryHandler(doctor_click, pattern=r"^doc::"))
    app.add_handler(CallbackQueryHandler(date_click, pattern=r"^date::"))
    app.add_handler(CallbackQueryHandler(slot_click, pattern=r"^slot::"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    logging.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
