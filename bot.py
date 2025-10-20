# bot.py — МедНавигатор РГ Клиник (v2 Render Edition)
# ----------------------------------------------------
# ✅ Работает через polling
# ✅ Автоматически создаёт шапки в Google Sheets (/init_sheets)
# ✅ Поддержка записи к врачу, прайс-листов и памяток
# ✅ Кнопки: «Ещё слоты», «На другой день»
# ✅ Отмена записи: /cancel_booking <slot_id>
# ✅ Уведомление администратора через ADMIN_CHAT_ID

import os
import re
import json
import logging
from datetime import datetime
from dateutil.parser import parse as dt_parse
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# --- Настройки из окружения ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

SCHEDULE_SHEET = "Schedule"
REQUESTS_SHEET = "Requests"
PRICES_SHEET = "Prices"
PREP_SHEET = "Prep"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# --- Интерфейс ---
WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\nВыберите раздел ниже:"
HELP = "ℹ️ Команды:\n/start — начать\n/menu — меню\n/init_sheets — создать таблицы\n/cancel_booking <slot_id> — снять бронь"

BTN_RECORD = "📅 Запись на приём"
BTN_PRICES = "🧾 Цены и анализы"
BTN_PREP = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RECORD, callback_data="RECORD")],
        [InlineKeyboardButton(BTN_PRICES, callback_data="PRICES")],
        [InlineKeyboardButton(BTN_PREP, callback_data="PREP")],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data="CONTACTS")],
    ])

# --- Google Sheets ---
def gs_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(SERVICE_JSON), scopes=scopes)
    return gspread.authorize(creds)

def open_ws(sheet_name):
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        sh.add_worksheet(title=sheet_name, rows=100, cols=20)
        return sh.worksheet(sheet_name)

def ensure_headers():
    mapping = {
        SCHEDULE_SHEET: ["slot_id","doctor_id","doctor_name","specialty","date","time","tz","status","patient_full_name","patient_phone","created_at","updated_at"],
        REQUESTS_SHEET: ["appointment_id","patient_full_name","patient_phone","doctor_full_name","date","time","datetime_iso","status"],
        PRICES_SHEET: ["code","name","price","tat_days","notes"],
        PREP_SHEET: ["test_name","memo"]
    }
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    created = []
    for sheet, headers in mapping.items():
        try:
            ws = sh.worksheet(sheet)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet, rows=100, cols=20)
        vals = ws.get_all_values()
        if not vals:
            ws.append_row(headers)
            created.append(sheet)
    return created

# --- Вспомогательные функции ---
def read_all(ws):
    vals = ws.get_all_values()
    if not vals: return [], []
    return vals[0], vals[1:]

def header_map(header):
    return {re.sub(r'[^a-z0-9а-я]', '', h.strip().lower()): i for i, h in enumerate(header)}

# --- Schedule logic ---
def find_free_slots(query, page=0, page_size=3, date_filter=None):
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    hm = header_map(header)
    result = []
    now = datetime.now()
    for row in data:
        try:
            st = row[hm.get("status", -1)].upper()
            if st != "FREE": continue
            doc = row[hm.get("doctor_name", -1)]
            sp = row[hm.get("specialty", -1)]
            if query.lower() not in doc.lower() and query.lower() not in sp.lower():
                continue
            dt = dt_parse(f"{row[hm.get('date', -1)]} {row[hm.get('time', -1)]}")
            if date_filter and row[hm.get("date", -1)] != date_filter:
                continue
            if dt < now: continue
            result.append({
                "slot_id": row[hm.get("slot_id", -1)],
                "doctor_name": doc,
                "specialty": sp,
                "date": row[hm.get("date", -1)],
                "time": row[hm.get("time", -1)],
            })
        except Exception:
            continue
    start, end = page * page_size, (page + 1) * page_size
    return result[start:end]

def update_slot(slot_id, status, fio="", phone=""):
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    hm = header_map(header)
    for i, row in enumerate(data, start=2):
        if row[hm.get("slot_id", -1)] == slot_id:
            new = row.copy()
            new[hm["status"]] = status
            if "patient_full_name" in hm: new[hm["patient_full_name"]] = fio
            if "patient_phone" in hm: new[hm["patient_phone"]] = phone
            ws.update(f"A{i}:L{i}", [new])
            return True
    return False

def add_request(fio, phone, doctor, date, time):
    ws = open_ws(REQUESTS_SHEET)
    header, _ = read_all(ws)
    if not header: ws.append_row(["appointment_id","patient_full_name","patient_phone","doctor_full_name","date","time","datetime_iso","status"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([now, fio, phone, doctor, date, time, f"{date}T{time}:00", "Новая"])

# --- Telegram logic ---
ASK_DOCTOR, ASK_SLOT, ASK_FIO, ASK_PHONE, ASK_DATE = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=main_menu())

async def init_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    created = ensure_headers()
    if created:
        await update.message.reply_text(f"Созданы листы: {', '.join(created)}")
    else:
        await update.message.reply_text("Все листы уже есть ✅")

async def cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите slot_id, пример:\n/cancel_booking DOC01-2025-10-25-09:00")
        return
    slot_id = context.args[0]
    if update_slot(slot_id, "FREE"):
        await update.message.reply_text(f"✅ Слот {slot_id} освобождён.")
    else:
        await update.message.reply_text("❌ Не удалось отменить (проверьте slot_id).")

# --- FSM: запись ---
async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message  # вместо update.message
    await msg.reply_text("Введите врача или специализацию:")
    return ASK_DOCTOR

async def record_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    context.user_data["query"] = query
    slots = find_free_slots(query, 0)
    if not slots:
        await update.message.reply_text("Свободных слотов не найдено 😔", reply_markup=main_menu())
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=s['slot_id'])] for s in slots] +
                              [[InlineKeyboardButton("Ещё слоты ⏭️", callback_data="MORE")],
                               [InlineKeyboardButton("На другой день 📅", callback_data="DATE")]])
    await update.message.reply_text("Выберите слот:", reply_markup=kb)
    return ASK_SLOT

async def record_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    slot_id = q.data
    if slot_id == "MORE":
        slots = find_free_slots(context.user_data["query"], 1)
        if not slots:
            await q.message.reply_text("Больше слотов нет 😅", reply_markup=main_menu())
            return ConversationHandler.END
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=s['slot_id'])] for s in slots])
        await q.message.reply_text("Дополнительные слоты:", reply_markup=kb)
        return ASK_SLOT
    if slot_id == "DATE":
        await q.message.reply_text("Введите дату (ГГГГ-ММ-ДД):")
        return ASK_DATE
    context.user_data["slot_id"] = slot_id
    await q.message.reply_text("Введите ФИО пациента:")
    return ASK_FIO

async def record_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date = update.message.text.strip()
    query = context.user_data["query"]
    slots = find_free_slots(query, 0, date_filter=date)
    if not slots:
        await update.message.reply_text("Свободных слотов на эту дату нет.", reply_markup=main_menu())
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=s['slot_id'])] for s in slots])
    await update.message.reply_text(f"Свободные слоты на {date}:", reply_markup=kb)
    return ASK_SLOT

async def record_fio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fio"] = update.message.text.strip()
    await update.message.reply_text("Введите телефон:")
    return ASK_PHONE

async def record_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    fio = context.user_data["fio"]
    slot_id = context.user_data["slot_id"]
    update_slot(slot_id, "BOOKED", fio, phone)
    add_request(fio, phone, "Врач", "—", "—")
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID),
                text=f"🆕 Новая запись:\n{fio}\n📞 {phone}\n🩺 Слот: {slot_id}")
        except Exception:
            pass
    await update.message.reply_text(f"✅ Запись подтверждена для {fio}.", reply_markup=main_menu())
    return ConversationHandler.END

# --- Запуск ---
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: record_start(u,c), pattern="RECORD"),
                      MessageHandler(filters.Regex("^📅 Запись на приём$"), record_start)],
        states={
            ASK_DOCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, record_doctor)],
            ASK_SLOT: [CallbackQueryHandler(record_slot)],
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, record_date)],
            ASK_FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, record_fio)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, record_phone)],
        },
        fallbacks=[],
        allow_reentry=True
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", lambda u,c: u.message.reply_text(HELP)))
    app.add_handler(CommandHandler("init_sheets", init_sheets))
    app.add_handler(CommandHandler("cancel_booking", cancel_booking))
    app.add_handler(conv)
       # --- Глобальный обработчик ошибок ---
    async def error_handler(update, context):
        logging.exception("Unhandled exception", exc_info=context.error)

    app.add_error_handler(error_handler)
return app

def main():
    if not BOT_TOKEN: raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    if not SPREADSHEET_ID: raise SystemExit("❗ GOOGLE_SPREADSHEET_ID не задан")
    if not SERVICE_JSON: raise SystemExit("❗ GOOGLE_SERVICE_ACCOUNT не задан")
    app = build_app()
    logging.info("Бот запускается (polling)…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()


