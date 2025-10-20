# bot.py — МедНавигатор РГ Клиник (Stable v3)
# -------------------------------------------
# ✔ /init_sheets — создаёт шапки листов
# ✔ /fix_headers — принудительно обновляет шапки
# ✔ /debug_slots [запрос] — показывает, что видит бот
# ✔ Запись к врачу: поиск слотов, «Ещё слоты», «На другой день», BOOKED
# ✔ /cancel_booking <slot_id> — вернуть слот в FREE
# ✔ Уведомления админу (ADMIN_CHAT_ID)
# Требования: python-telegram-bot==20.8, gspread, google-auth, python-dateutil

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
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# --------- ENV ----------
BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID  = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON    = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_CHAT_ID   = os.getenv("ADMIN_CHAT_ID")

SCHEDULE_SHEET  = "Schedule"
REQUESTS_SHEET  = "Requests"
PRICES_SHEET    = "Prices"
PREP_SHEET      = "Prep"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# --------- UI ----------
WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\nВыберите раздел ниже:"
HELP    = "ℹ️ Команды: /start, /menu, /init_sheets, /fix_headers, /debug_slots, /cancel_booking <slot_id>"

BTN_RECORD   = "📅 Запись на приём"
BTN_PRICES   = "🧾 Цены и анализы"
BTN_PREP     = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RECORD,   callback_data="RECORD")],
        [InlineKeyboardButton(BTN_PRICES,   callback_data="PRICES")],
        [InlineKeyboardButton(BTN_PREP,     callback_data="PREP")],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data="CONTACTS")],
    ])

# --------- Google Sheets helpers ----------
HEADERS = {
    SCHEDULE_SHEET: ["slot_id","doctor_id","doctor_name","specialty","date","time","tz","status","patient_full_name","patient_phone","created_at","updated_at"],
    REQUESTS_SHEET: ["appointment_id","patient_full_name","patient_phone","doctor_full_name","date","time","datetime_iso","status"],
    PRICES_SHEET:   ["code","name","price","tat_days","notes"],
    PREP_SHEET:     ["test_name","memo"],
}

def gs_client():
    if not SERVICE_JSON:
        raise SystemExit("❗ GOOGLE_SERVICE_ACCOUNT не задан")
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(SERVICE_JSON), scopes=scopes)
    return gspread.authorize(creds)

def open_ws(sheet_name: str):
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        sh.add_worksheet(title=sheet_name, rows=100, cols=30)
        ws = sh.worksheet(sheet_name)
        ws.append_row(HEADERS[sheet_name])
        return ws

def ensure_headers() -> list[str]:
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    created = []
    for name, hdr in HEADERS.items():
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=100, cols=30)
            ws.append_row(hdr)
            created.append(name)
            continue
        vals = ws.get_all_values()
        if not vals:
            ws.append_row(hdr)
            created.append(name)
    return created

def fix_headers_force():
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    for name, hdr in HEADERS.items():
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=100, cols=30)
        ws.update("A1", [hdr])

def read_all(ws):
    vals = ws.get_all_values()
    if not vals: return [], []
    return vals[0], vals[1:]

def header_map(header):
    # Нормализатор названий (латиница/русские буквы в нижнем регистре без пробелов)
    return {re.sub(r'[^a-z0-9а-я]', '', h.strip().lower()): i for i, h in enumerate(header)}

# --------- Schedule ops ----------
def find_free_slots(query: str, page: int = 0, page_size: int = 3, date_filter: str | None = None):
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    if not header: return []
    hm = header_map(header)

    def col(name: str) -> int:
        return hm.get(re.sub(r'[^a-z0-9а-я]', '', name))

    idx_status = col("status")
    idx_doc    = col("doctor_name")
    idx_spec   = col("specialty")
    idx_date   = col("date")
    idx_time   = col("time")
    idx_slot   = col("slot_id")

    q = (query or "").strip().lower()
    now = datetime.now()
    pool = []

    for r in data:
        try:
            if idx_status is None or r[idx_status].strip().upper() != "FREE":
                continue
            doc = r[idx_doc] if idx_doc is not None else ""
            sp  = r[idx_spec] if idx_spec is not None else ""
            if q and (q not in doc.lower()) and (q not in sp.lower()):
                continue
            d = r[idx_date] if idx_date is not None else ""
            t = r[idx_time] if idx_time is not None else ""
            if not d or not t:
                continue
            if date_filter and d != date_filter:
                continue
            dt = dt_parse(f"{d} {t}")
            if dt < now:
                continue
            pool.append({
                "slot_id": r[idx_slot] if idx_slot is not None else "",
                "doctor_name": doc,
                "specialty": sp,
                "date": d,
                "time": t,
            })
        except Exception:
            continue

    start = page * page_size
    end = start + page_size
    return pool[start:end]

def update_slot(slot_id: str, status: str, fio: str = "", phone: str = "") -> bool:
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    if not header: return False
    hm = header_map(header)

    idx_slot   = hm.get("slot_id")
    idx_status = hm.get("status")
    idx_fio    = hm.get("patient_full_name")
    idx_phone  = hm.get("patient_phone")
    idx_upd    = hm.get("updated_at")

    for i, r in enumerate(data, start=2):
        if idx_slot is not None and r[idx_slot] == slot_id:
            row = r[:]
            if idx_status is not None: row[idx_status] = status
            if idx_fio is not None:    row[idx_fio]    = fio
            if idx_phone is not None:  row[idx_phone]  = phone
            if idx_upd is not None:    row[idx_upd]    = datetime.now().isoformat(timespec="seconds")
            # растянуть до длины шапки
            while len(row) < len(header):
                row.append("")
            ws.update(f"A{i}:{chr(64+len(header))}{i}", [row])
            return True
    return False

def get_slot_info(slot_id: str) -> dict:
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    hm = header_map(header)
    idx_slot = hm.get("slot_id")
    def gv(row, name):
        j = hm.get(name); 
        return row[j] if j is not None and j < len(row) else ""
    for r in data:
        if idx_slot is not None and r[idx_slot] == slot_id:
            return {
                "doctor_full_name": gv(r, "doctor_name"),
                "date": gv(r, "date"),
                "time": gv(r, "time"),
            }
    return {"doctor_full_name": "", "date": "", "time": ""}

def append_request(fio: str, phone: str, doctor: str, date: str, time: str):
    ws = open_ws(REQUESTS_SHEET)
    header, _ = read_all(ws)
    if not header:
        ws.append_row(HEADERS[REQUESTS_SHEET])
    now_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([now_id, fio, phone, doctor, date, time, f"{date}T{time}:00", "Новая"])

# --------- Handlers ----------
ASK_DOCTOR, ASK_SLOT, ASK_FIO, ASK_PHONE, ASK_DATE = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text(WELCOME, reply_markup=main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text("Главное меню:", reply_markup=main_menu())

async def init_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    created = ensure_headers()
    await update.message.reply_text("Все листы уже есть ✅" if not created else f"Созданы листы/шапки: {', '.join(created)}")

async def fix_headers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fix_headers_force()
    await update.message.reply_text("✅ Заголовки колонок обновлены на листах: " + ", ".join(HEADERS.keys()))

async def cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите slot_id. Пример:\n/cancel_booking DOC01-2025-10-28-09:00")
        return
    slot_id = context.args[0]
    ok = update_slot(slot_id, "FREE", "", "")
    await update.message.reply_text("✅ Слот освобождён" if ok else "❌ Не удалось отменить (проверьте slot_id/статус).")

async def debug_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip() if context.args else ""
    try:
        slots = find_free_slots(query, page=0, page_size=10, date_filter=None)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка чтения таблицы: {e}")
        return
    if not slots:
        await update.message.reply_text("🔍 Свободных слотов не найдено.")
        return
    text = "\n".join([f"• {s['doctor_name']} • {s['specialty']} • {s['date']} {s['time']} • `{s['slot_id']}`" for s in slots])
    await update.message.reply_text("Найденные FREE-слоты:\n" + text, parse_mode="Markdown")

# ---- FSM: запись ----
async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text("Введите врача или специализацию (например, Гинеколог):")
    context.user_data.clear()
    return ASK_DOCTOR

async def record_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    context.user_data["query"] = q
    context.user_data["page"] = 0
    context.user_data["date_filter"] = None
    slots = find_free_slots(q, page=0, page_size=3, date_filter=None)
    if not slots:
        await update.message.reply_text("Свободных слотов не найдено 😔", reply_markup=main_menu())
        return ConversationHandler.END
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=f"SLOT::{s['slot_id']}")] for s in slots] +
        [[InlineKeyboardButton("Ещё слоты ⏭️", callback_data="MORE"),
          InlineKeyboardButton("На другой день 📅", callback_data="ASKDATE")]]
    )
    await update.message.reply_text("Выберите слот:", reply_markup=kb)
    return ASK_SLOT

async def record_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "MORE":
        page = context.user_data.get("page", 0) + 1
        context.user_data["page"] = page
        query = context.user_data.get("query", "")
        d = context.user_data.get("date_filter")
        slots = find_free_slots(query, page=page, page_size=3, date_filter=d)
        if not slots:
            await q.message.reply_text("Больше слотов не найдено.", reply_markup=main_menu())
            return ConversationHandler.END
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=f"SLOT::{s['slot_id']}")] for s in slots] +
            [[InlineKeyboardButton("Ещё слоты ⏭️", callback_data="MORE"),
              InlineKeyboardButton("На другой день 📅", callback_data="ASKDATE")]]
        )
        await q.message.reply_text("Ещё варианты:", reply_markup=kb)
        return ASK_SLOT

    if data == "ASKDATE":
        await q.message.reply_text("Введите дату (ГГГГ-ММ-ДД):")
        return ASK_DATE

    if data.startswith("SLOT::"):
        slot_id = data.split("::", 1)[1]
        context.user_data["slot_id"] = slot_id
        await q.message.reply_text("Введите ФИО пациента:")
        return ASK_FIO

    await q.message.reply_text("Пожалуйста, выберите слот из списка.")
    return ASK_SLOT

async def record_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_txt = (update.message.text or "").strip()
    try:
        d = dt_parse(date_txt).date().isoformat()
    except Exception:
        await update.message.reply_text("Не распознал дату. Пример: 2025-10-28")
        return ASK_DATE
    context.user_data["date_filter"] = d
    context.user_data["page"] = 0
    query = context.user_data.get("query", "")
    slots = find_free_slots(query, page=0, page_size=3, date_filter=d)
    if not slots:
        await update.message.reply_text("На эту дату свободных слотов нет.", reply_markup=main_menu())
        return ConversationHandler.END
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=f"SLOT::{s['slot_id']}")] for s in slots] +
        [[InlineKeyboardButton("Ещё слоты ⏭️", callback_data="MORE"),
          InlineKeyboardButton("На другой день 📅", callback_data="ASKDATE")]]
    )
    await update.message.reply_text(f"Свободные слоты на {d}:", reply_markup=kb)
    return ASK_SLOT

async def record_fio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fio"] = (update.message.text or "").strip()
    await update.message.reply_text("Введите телефон:")
    return ASK_PHONE

async def record_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = (update.message.text or "").strip()
    fio   = context.user_data.get("fio", "")
    slot_id = context.user_data.get("slot_id", "")

    # BOOKED в расписании
    ok = update_slot(slot_id, "BOOKED", fio, phone)
    if not ok:
        await update.message.reply_text("Не удалось подтвердить слот (возможно, его заняли). Попробуйте заново.", reply_markup=main_menu())
        return ConversationHandler.END

    # данные слота для журнала
    info = get_slot_info(slot_id)
    append_request(fio, phone, info.get("doctor_full_name",""), info.get("date",""), info.get("time",""))

    # уведомление админу
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=(f"🆕 Новая запись\n"
                      f"Пациент: {fio}\n"
                      f"Телефон: {phone}\n"
                      f"Врач: {info.get('doctor_full_name','')}\n"
                      f"Дата: {info.get('date','')} {info.get('time','')}\n"
                      f"slot_id: {slot_id}")
            )
        except Exception:
            logging.exception("Не удалось отправить уведомление админу")

    await update.message.reply_text(
        f"✅ Запись подтверждена:\n{info.get('doctor_full_name','')}\n{info.get('date','')} {info.get('time','')}\nПациент: {fio}\nТелефон: {phone}",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

# --------- App wiring ----------
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(lambda u, c: record_start(u, c), pattern="RECORD"),
            MessageHandler(filters.Regex("^📅 Запись на приём$"), record_start),
        ],
        states={
            ASK_DOCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, record_doctor)],
            ASK_SLOT:   [CallbackQueryHandler(record_slot)],
            ASK_DATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, record_date)],
            ASK_FIO:    [MessageHandler(filters.TEXT & ~filters.COMMAND, record_fio)],
            ASK_PHONE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, record_phone)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu",  menu))
    app.add_handler(CommandHandler("help",  lambda u, c: u.message.reply_text(HELP)))
    app.add_handler(CommandHandler("init_sheets",  init_sheets))
    app.add_handler(CommandHandler("fix_headers",  fix_headers))
    app.add_handler(CommandHandler("debug_slots",  debug_slots))
    app.add_handler(CommandHandler("cancel_booking", cancel_booking))

    # FSM
    app.add_handler(conv)

    # Глобальный обработчик ошибок
    async def error_handler(update, context):
        logging.exception("Unhandled exception", exc_info=context.error)
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=f"⚠️ Ошибка: {context.error}"
                )
            except Exception:
                pass

    app.add_error_handler(error_handler)
    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    if not SPREADSHEET_ID:
        raise SystemExit("❗ GOOGLE_SPREADSHEET_ID не задан")
    if not SERVICE_JSON:
        raise SystemExit("❗ GOOGLE_SERVICE_ACCOUNT не задан")
    app = build_app()
    logging.info("Бот запускается (polling)…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

