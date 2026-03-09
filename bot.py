import os
import re
import json
import logging
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

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("rgclinicbot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SPREADSHEET_ID = (
    os.getenv("GOOGLE_SHEETS_ID")
    or os.getenv("GOOGLE_SPREADSHEET_ID")
    or ""
).strip()
SERVICE_JSON = (
    os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    or os.getenv("GOOGLE_SERVICE_ACCOUNT")
    or ""
).strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

CITILAB_URL = "https://citilab.ru/ufa/catalog/"

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

BOOK_DOCTOR, BOOK_SLOT, BOOK_NAME, BOOK_PHONE, PREP_QUERY = range(5)

MENU_BOOK = "📅 Запись"
MENU_DOCTORS = "👨‍⚕️ Врачи"
MENU_PRICES = "🧾 Цены"
MENU_PREP = "ℹ️ Подготовка"
MENU_CONTACTS = "📍 Контакты"
MENU_BACK = "↩️ В меню"

menu = ReplyKeyboardMarkup(
    [
        [KeyboardButton(MENU_BOOK), KeyboardButton(MENU_DOCTORS)],
        [KeyboardButton(MENU_PRICES), KeyboardButton(MENU_PREP)],
        [KeyboardButton(MENU_CONTACTS)],
    ],
    resize_keyboard=True,
)

back_menu = ReplyKeyboardMarkup(
    [[KeyboardButton(MENU_BACK)]],
    resize_keyboard=True,
)

# ---------- helpers ----------

def norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slug(value) -> str:
    text = norm(value).lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", text)


def valid_phone(value: str) -> bool:
    digits = re.sub(r"\D+", "", value or "")
    return 10 <= len(digits) <= 15


def get_info_value(key: str) -> str:
    rows = sheet_info.get_all_records()
    for row in rows:
        k = norm(row.get("Ключ") or row.get("key"))
        v = norm(row.get("Значение") or row.get("value"))
        if k == key:
            return v
    return ""


def safe_records(ws):
    try:
        return ws.get_all_records()
    except Exception as e:
        logger.exception("Ошибка чтения листа: %s", e)
        return []


def get_schedule_rows():
    values = sheet_schedule.get_all_values()
    if not values:
        return [], {}

    headers = [norm(x) for x in values[0]]
    header_map = {h: i for i, h in enumerate(headers)}

    required = {
        "slot_id": ["ID слота", "slot_id", "Id слота"],
        "doctor": ["Врач", "doctor_name", "doctor"],
        "specialty": ["Специальность", "specialty"],
        "date": ["Дата", "date"],
        "time": ["Время", "time"],
        "status": ["Статус", "status"],
        "patient": ["Пациент", "patient", "patient_full_name"],
        "phone": ["Телефон", "phone", "patient_phone"],
    }

    idx = {}
    for key, variants in required.items():
        found = None
        for v in variants:
            if v in header_map:
                found = header_map[v]
                break
        if found is None:
            raise RuntimeError(f"Не найдена колонка '{key}' в листе Расписание")
        idx[key] = found

    parsed = []
    for row_num, row in enumerate(values[1:], start=2):
        row_extended = row + [""] * (len(headers) - len(row))
        item = {
            "row_num": row_num,
            "slot_id": norm(row_extended[idx["slot_id"]]),
            "doctor": norm(row_extended[idx["doctor"]]),
            "specialty": norm(row_extended[idx["specialty"]]),
            "date": norm(row_extended[idx["date"]]),
            "time": norm(row_extended[idx["time"]]),
            "status": norm(row_extended[idx["status"]]).upper(),
            "patient": norm(row_extended[idx["patient"]]),
            "phone": norm(row_extended[idx["phone"]]),
        }
        if item["slot_id"] and item["doctor"]:
            parsed.append(item)

    return parsed, idx


def free_slots():
    rows, _ = get_schedule_rows()
    return [r for r in rows if r["status"] == "FREE"]


def doctors_with_free_slots():
    result = []
    seen = set()

    for slot in free_slots():
        key = (slot["doctor"], slot["specialty"])
        if key not in seen:
            seen.add(key)
            result.append({
                "doctor": slot["doctor"],
                "specialty": slot["specialty"],
            })

    return result


def booking_in_progress(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(
        context.user_data.get("doctor_list")
        or context.user_data.get("doctor_slots")
        or context.user_data.get("selected_slot")
        or context.user_data.get("patient_name")
    )


def search_own_prep(query: str):
    rows = safe_records(sheet_prep)
    q = slug(query)
    best = None
    best_score = -1

    for row in rows:
        analysis = norm(row.get("Анализ") or row.get("Услуга") or row.get("analysis"))
        prep_text = norm(row.get("Подготовка") or row.get("prep"))
        if not analysis:
            continue

        hay = slug(analysis)
        score = 0
        if q and q in hay:
            score = 100
        else:
            q_words = [slug(x) for x in query.split() if slug(x)]
            score = sum(1 for w in q_words if w in hay)

        if score > best_score:
            best_score = score
            best = {"analysis": analysis, "prep": prep_text}

    if best_score > 0:
        return best
    return None


def first_prices(limit=15):
    rows = safe_records(sheet_prices)
    result = []
    for row in rows:
        code = norm(row.get("Код") or row.get("code"))
        name = norm(row.get("Название") or row.get("name"))
        price = norm(row.get("Цена") or row.get("price"))
        if name:
            result.append({"code": code, "name": name, "price": price})
    return result[:limit]


def doctors_text():
    rows = safe_records(sheet_doctors)
    if not rows:
        return "Список врачей сейчас недоступен. Уточните, пожалуйста, у администратора."

    parts = ["👨‍⚕️ Врачи РГ Клиник\n"]
    for row in rows:
        fio = norm(row.get("ФИО") or row.get("fio"))
        spec = norm(row.get("Специальность") or row.get("specialty"))
        exp = norm(row.get("Стаж") or row.get("experience"))
        schedule = norm(row.get("График приёма") or row.get("График") or row.get("schedule"))
        cabinet = norm(row.get("Кабинет") or row.get("cabinet"))

        if not fio:
            continue

        block = f"{fio}"
        if spec:
            block += f"\nСпециальность: {spec}"
        if exp:
            block += f"\nСтаж: {exp}"
        if schedule:
            block += f"\nГрафик: {schedule}"
        if cabinet:
            block += f"\nКабинет: {cabinet}"
        parts.append(block + "\n")

    return "\n".join(parts)

# ---------- handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 МедНавигатор РГ Клиник\n\n"
        "Выберите действие в меню.\n"
        "Бот предоставляет справочную информацию и не заменяет консультацию специалиста."
    )
    await update.message.reply_text(text, reply_markup=menu)


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = get_info_value("clinic_address")
    phone = get_info_value("clinic_phone")
    hours = get_info_value("clinic_hours")
    manager = get_info_value("clinic_manager")

    parts = ["📍 Контакты"]
    if address:
        parts.append(f"Адрес: {address}")
    if phone:
        parts.append(f"Телефон: {phone}")
    if hours:
        parts.append(f"Режим работы: {hours}")
    if manager:
        parts.append(f"Главный врач: {manager}")

    await update.message.reply_text("\n".join(parts), reply_markup=menu)


async def doctors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(doctors_text(), reply_markup=menu)


async def prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = first_prices(15)
    if not items:
        text = (
            "🧾 Цены на услуги РГ Клиник сейчас недоступны.\n\n"
            f"Лабораторные анализы по аутсорсингу: {CITILAB_URL}"
        )
        await update.message.reply_text(text, reply_markup=menu)
        return

    parts = ["🧾 Цены на услуги РГ Клиник\n"]
    for item in items:
        line = "• "
        if item["code"]:
            line += f'{item["code"]} '
        line += item["name"]
        if item["price"]:
            line += f' — {item["price"]}'
        parts.append(line)

    parts.append("")
    parts.append("Лабораторные анализы, выполняемые по аутсорсингу:")
    parts.append(f"Стоимость и подготовка доступны на сайте СИТИЛАБ: {CITILAB_URL}")

    await update.message.reply_text("\n".join(parts), reply_markup=menu)


async def prep_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Напишите название исследования или услуги РГ Клиник.\n"
        "Если это лабораторный анализ по аутсорсингу, я направлю вас на сайт СИТИЛАБ.",
        reply_markup=back_menu,
    )
    return PREP_QUERY


async def prep_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = norm(update.message.text)

    if text == MENU_BACK:
        await update.message.reply_text("Возвращаю в главное меню.", reply_markup=menu)
        return ConversationHandler.END

    item = search_own_prep(text)
    if item and item["prep"]:
        await update.message.reply_text(
            f"{item['analysis']}\n\n{item['prep']}\n\nИнформация справочная.",
            reply_markup=menu,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Для лабораторных анализов, выполняемых по аутсорсингу, "
        "стоимость и режим подготовки смотрите на сайте СИТИЛАБ:\n"
        f"{CITILAB_URL}",
        reply_markup=menu,
    )
    return ConversationHandler.END


async def booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doctor_list = doctors_with_free_slots()
    except Exception as e:
        logger.exception("Ошибка чтения расписания: %s", e)
        await update.message.reply_text(
            "Не удалось открыть расписание. Уточните запись у администратора.",
            reply_markup=menu,
        )
        return ConversationHandler.END

    if not doctor_list:
        await update.message.reply_text(
            "Свободных слотов сейчас нет. Пожалуйста, уточните запись у администратора.",
            reply_markup=menu,
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["doctor_list"] = doctor_list

    keyboard = []
    for i, item in enumerate(doctor_list[:30]):
        label = item["doctor"]
        if item["specialty"]:
            label += f" — {item['specialty']}"
        keyboard.append(
            [InlineKeyboardButton(label[:64], callback_data=f"book_doctor|{i}")]
        )

    await update.message.reply_text(
        "Выберите врача и специальность:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BOOK_DOCTOR


async def choose_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.split("|")[1])
        selected = context.user_data["doctor_list"][idx]
        doctor = selected["doctor"]
        specialty = selected["specialty"]
    except Exception:
        await query.edit_message_text("Не удалось определить врача. Начните запись заново.")
        return ConversationHandler.END

    slots = [
        s for s in free_slots()
        if s["doctor"] == doctor and s["specialty"] == specialty
    ]

    if not slots:
        await query.edit_message_text("У выбранного врача нет свободных слотов.")
        return ConversationHandler.END

    context.user_data["selected_doctor"] = doctor
    context.user_data["selected_specialty"] = specialty
    context.user_data["doctor_slots"] = slots

    keyboard = [
        [InlineKeyboardButton(f'{s["date"]} {s["time"]}', callback_data=f"book_slot|{i}")]
        for i, s in enumerate(slots[:30])
    ]

    text = f"Врач: {doctor}"
    if specialty:
        text += f"\nСпециальность: {specialty}"
    text += "\nВыберите время:"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BOOK_SLOT


async def choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.split("|")[1])
        slot = context.user_data["doctor_slots"][idx]
    except Exception:
        await query.edit_message_text("Не удалось определить слот. Начните запись заново.")
        return ConversationHandler.END

    context.user_data["selected_slot"] = slot

    text = f"Вы выбрали:\nВрач: {slot['doctor']}"
    if slot["specialty"]:
        text += f"\nСпециальность: {slot['specialty']}"
    text += f"\nДата: {slot['date']}\nВремя: {slot['time']}\n\nВведите ФИО пациента."

    await query.edit_message_text(text)
    return BOOK_NAME


async def booking_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = norm(update.message.text)

    if text == MENU_BACK:
        await update.message.reply_text("Возвращаю в главное меню.", reply_markup=menu)
        context.user_data.clear()
        return ConversationHandler.END

    if len(text) < 5:
        await update.message.reply_text("Пожалуйста, введите ФИО полностью.", reply_markup=back_menu)
        return BOOK_NAME

    context.user_data["patient_name"] = text
    await update.message.reply_text("Введите телефон пациента:", reply_markup=back_menu)
    return BOOK_PHONE


async def booking_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = norm(update.message.text)

    if text == MENU_BACK:
        await update.message.reply_text("Возвращаю в главное меню.", reply_markup=menu)
        context.user_data.clear()
        return ConversationHandler.END

    if not valid_phone(text):
        await update.message.reply_text("Введите телефон в корректном формате.", reply_markup=back_menu)
        return BOOK_PHONE

    patient_name = context.user_data.get("patient_name")
    selected_slot = context.user_data.get("selected_slot")

    if not selected_slot:
        await update.message.reply_text("Слот не найден. Начните запись заново.", reply_markup=menu)
        context.user_data.clear()
        return ConversationHandler.END

    try:
        rows, _ = get_schedule_rows()
        fresh_slot = next((r for r in rows if r["slot_id"] == selected_slot["slot_id"]), None)

        if not fresh_slot:
            await update.message.reply_text("Слот не найден в расписании. Попробуйте снова.", reply_markup=menu)
            context.user_data.clear()
            return ConversationHandler.END

        if fresh_slot["status"] != "FREE":
            await update.message.reply_text("Это время уже занято. Пожалуйста, выберите другой слот.", reply_markup=menu)
            context.user_data.clear()
            return ConversationHandler.END

        sheet_schedule.update(
            f"A{fresh_slot['row_num']}:H{fresh_slot['row_num']}",
            [[
                fresh_slot["slot_id"],
                fresh_slot["doctor"],
                fresh_slot["specialty"],
                fresh_slot["date"],
                fresh_slot["time"],
                "BOOKED",
                patient_name,
                text,
            ]],
        )

        request_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet_requests.append_row(
            [
                request_id,
                patient_name,
                text,
                fresh_slot["doctor"],
                fresh_slot["date"],
                fresh_slot["time"],
                "Новая",
            ],
            value_input_option="USER_ENTERED",
        )

        if ADMIN_CHAT_ID:
            try:
                admin_text = (
                    "📥 Новая запись\n"
                    f"Пациент: {patient_name}\n"
                    f"Телефон: {text}\n"
                    f"Врач: {fresh_slot['doctor']}\n"
                )

                if fresh_slot["specialty"]:
                    admin_text += f"Специальность: {fresh_slot['specialty']}\n"

                admin_text += (
                    f"Дата: {fresh_slot['date']}\n"
                    f"Время: {fresh_slot['time']}"
                )

                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=admin_text,
                )
            except Exception as e:
                logger.warning("Не удалось отправить уведомление админу: %s", e)

        text_confirm = (
            "✅ Запись оформлена.\n"
            f"Врач: {fresh_slot['doctor']}\n"
        )

        if fresh_slot["specialty"]:
            text_confirm += f"Специальность: {fresh_slot['specialty']}\n"

        text_confirm += (
            f"Дата: {fresh_slot['date']}\n"
            f"Время: {fresh_slot['time']}\n\n"
            "Справочно: при необходимости администратор свяжется с вами."
        )

        await update.message.reply_text(
            text_confirm,
            reply_markup=menu,
        )

        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        logger.exception("Ошибка при записи: %s", e)
        await update.message.reply_text(
            "Не удалось завершить запись. Пожалуйста, попробуйте ещё раз или свяжитесь с администратором.",
            reply_markup=menu,
        )
        context.user_data.clear()
        return ConversationHandler.END


async def answer_info_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = slug(update.message.text)

    if any(x in text for x in ["руководител", "главныйврач", "директор"]):
        manager = get_info_value("clinic_manager")
        reply = f"Главный врач клиники: {manager}" if manager else "Информация о руководителе уточняется."
        await update.message.reply_text(reply, reply_markup=menu)
        return True

    if any(x in text for x in ["адрес", "гденаходит", "локац"]):
        value = get_info_value("clinic_address")
        reply = f"Адрес: {value}" if value else "Адрес уточняется."
        await update.message.reply_text(reply, reply_markup=menu)
        return True

    if any(x in text for x in ["телефон", "номер", "контакт", "позвонить"]):
        value = get_info_value("clinic_phone")
        reply = f"Телефон: {value}" if value else "Телефон уточняется."
        await update.message.reply_text(reply, reply_markup=menu)
        return True

    if any(x in text for x in ["режимработ", "часыработ", "графикработ"]):
        value = get_info_value("clinic_hours")
        reply = f"Режим работы: {value}" if value else "Режим работы уточняется."
        await update.message.reply_text(reply, reply_markup=menu)
        return True

    return False


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = norm(update.message.text)

    if booking_in_progress(context) and text in [MENU_DOCTORS, MENU_PRICES, MENU_CONTACTS, MENU_BOOK]:
        await update.message.reply_text(
            "Сейчас у вас идет запись.\n"
            "Завершите её или нажмите ↩️ В меню.",
            reply_markup=back_menu,
        )
        return

    if text == MENU_DOCTORS:
        await doctors(update, context)
        return

    if text == MENU_PRICES:
        await prices(update, context)
        return

    if text == MENU_CONTACTS:
        await contacts(update, context)
        return

    answered = await answer_info_question(update, context)
    if answered:
        return

    await update.message.reply_text(
        "Выберите действие в меню 👇",
        reply_markup=menu,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=menu)
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Произошла техническая ошибка. Попробуйте ещё раз.",
                reply_markup=menu,
            )
    except Exception:
        logger.exception("Не удалось отправить сообщение об ошибке")


def main():
    app = Application.builder().token(TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{re.escape(MENU_BOOK)}$"), booking)],
        states={
            BOOK_DOCTOR: [CallbackQueryHandler(choose_doctor, pattern=r"^book_doctor\|\d+$")],
            BOOK_SLOT: [CallbackQueryHandler(choose_slot, pattern=r"^book_slot\|\d+$")],
            BOOK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, booking_name)],
            BOOK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, booking_phone)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{re.escape(MENU_BACK)}$"), cancel),
            CommandHandler("cancel", cancel),
        ],
        per_message=False,
    )

    prep_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{re.escape(MENU_PREP)}$"), prep_entry)],
        states={
            PREP_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, prep_query)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{re.escape(MENU_BACK)}$"), cancel),
            CommandHandler("cancel", cancel),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(booking_conv)
    app.add_handler(prep_conv)
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(MENU_DOCTORS)}$"), doctors))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(MENU_PRICES)}$"), prices))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(MENU_CONTACTS)}$"), contacts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.add_error_handler(on_error)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
