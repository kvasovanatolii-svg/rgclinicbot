# Create an updated bot with slot pagination, "another day" filter, and cancel command
from pathlib import Path

code = r'''
# bot_v2_actions.py — МедНавигатор РГ Клиник (MVP v2 + кнопки «Ещё слоты / На другой день» + отмена записи)
# Требования: python-telegram-bot==20.8, gspread, google-auth, python-dateutil, pandas (для шаблонов не обязателен)
# Функции:
# • Schedule: FREE → HOLD → BOOKED; отмена BOOKED → FREE (/cancel_booking <slot_id>)
# • Показ слотов: постранично (по 3 за страницу) + выбор конкретной даты («На другой день»)
# • Прайс и Памятки
# • /init_sheets — шапки на листах

import os, re, json, logging
from datetime import datetime
from dateutil.parser import parse as dt_parse, ParserError

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

BOT_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID     = os.getenv("GOOGLE_SPREADSHEET_ID")
SCHEDULE_SHEET     = os.getenv("GOOGLE_SCHEDULE_SHEET", "Schedule")
REQUESTS_SHEET     = os.getenv("GOOGLE_REQUESTS_SHEET", "Requests")
PRICES_SHEET       = os.getenv("GOOGLE_PRICES_SHEET", "Prices")
PREP_SHEET         = os.getenv("GOOGLE_PREP_SHEET", "Prep")
SA_JSON            = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_CHAT_ID      = os.getenv("ADMIN_CHAT_ID")
HOLD_MINUTES       = int(os.getenv("HOLD_MINUTES", "5"))
TZ                 = os.getenv("TIMEZONE", "Europe/Moscow")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

WELCOME_TEXT = "👋 Здравствуйте!\n\nЯ — МедНавигатор РГ Клиник. Выберите раздел ниже:"
HELP_TEXT = "ℹ️ Я помогу: запись к врачу, цены, подготовка, контакты.\nНажмите /menu или выберите кнопку."

BTN_RECORD, BTN_PRICES, BTN_PREP, BTN_CONTACTS = "📅 Запись на приём","🧾 Цены и анализы","ℹ️ Подготовка","📍 Контакты"
CB_RECORD, CB_PRICES, CB_PREP, CB_CONTACTS = "MENU_RECORD","MENU_PRICES","MENU_PREP","MENU_CONTACTS"

SCHEDULE_HEADERS = ["slot_id","doctor_id","doctor_name","specialty","date","time","tz","status","patient_full_name","patient_phone","created_at","updated_at"]
REQUESTS_HEADERS = ["appointment_id","patient_full_name","patient_phone","doctor_full_name","date","time","datetime_iso","status"]
PRICES_HEADERS   = ["code","name","price","tat_days","notes"]
PREP_HEADERS     = ["test_name","memo"]

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RECORD, callback_data=CB_RECORD)],
        [InlineKeyboardButton(BTN_PRICES, callback_data=CB_PRICES)],
        [InlineKeyboardButton(BTN_PREP,   callback_data=CB_PREP)],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data=CB_CONTACTS)],
    ])

def _gs_client():
    if not SA_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT не задан")
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(SA_JSON), scopes=scopes)
    return gspread.authorize(creds)

def _open_ws(sheet_name: str):
    gc = _gs_client(); sh = gc.open_by_key(SPREADSHEET_ID)
    try:    return sh.worksheet(sheet_name)
    except: return None

def _norm(s: str) -> str:
    if s is None: return ""
    x = str(s).strip().lower().replace("ё","е")
    x = re.sub(r"[^a-z0-9а-я_]+","_",x); x = re.sub(r"_+","_",x).strip("_")
    return x

def _read_all(ws):
    values = ws.get_all_values()
    if not values: return [], []
    return values[0], values[1:]

def ensure_sheet_headers(sheet_name: str, headers: list):
    ws = _open_ws(sheet_name)
    if not ws: return False
    header, _ = _read_all(ws)
    if not header:
        ws.append_row(headers)
        logging.info("Создал шапку на листе %s", sheet_name)
        return True
    return False

# ======= Schedule helpers =======
def _header_map(header):
    return { _norm(c): i for i, c in enumerate(header) }

def _update_row(ws, rownum, header, updates: dict):
    row = ws.row_values(rownum)
    while len(row) < len(header): row.append("")
    for i, v in updates.items(): row[i] = v
    ws.update(f"A{rownum}:{chr(64+len(header))}{rownum}", [row])

def _find_row_by(ws, header, col_name, value):
    hm = _header_map(header); col = hm.get(_norm(col_name))
    if col is None: return None, None
    data = ws.get_all_values()[1:]
    for i, r in enumerate(data, start=2):
        if len(r) > col and r[col] == value:
            return i, r
    return None, None

def find_free_slots(query: str, page:int=0, page_size:int=3, date_filter:str=None):
    ws = _open_ws(SCHEDULE_SHEET)
    if not ws: return []
    header, data = _read_all(ws); hm = _header_map(header)
    idx = {k: hm.get(_norm(k)) for k in SCHEDULE_HEADERS}
    key = (query or "").strip().lower()
    now = datetime.now()
    pool = []
    for r in data:
        try:
            st = (r[idx["status"]] if idx["status"] is not None else "").strip().upper()
            if st != "FREE": continue
            dn = r[idx["doctor_name"]] if idx["doctor_name"] is not None else ""
            sp = r[idx["specialty"]] if idx["specialty"] is not None else ""
            if key and key not in str(dn).lower() and key not in str(sp).lower():
                continue
            dt = dt_parse(f"{r[idx['date']]} {r[idx['time']]}")
            if dt < now: continue
            if date_filter and r[idx["date"]] != date_filter:
                continue
            pool.append({
                "slot_id": r[idx["slot_id"]],
                "doctor_name": dn, "specialty": sp,
                "date": r[idx["date"]], "time": r[idx["time"]]
            })
        except Exception:
            continue
    start = page*page_size; end = start+page_size
    return pool[start:end]

def hold_slot(slot_id: str) -> bool:
    ws = _open_ws(SCHEDULE_SHEET)
    if not ws: return False
    header, data = _read_all(ws); hm = _header_map(header)
    idx = {k: hm.get(_norm(k)) for k in SCHEDULE_HEADERS}
    for i, r in enumerate(data, start=2):
        if r[idx["slot_id"]] == slot_id:
            st = r[idx["status"]].strip().upper()
            if st != "FREE": return False
            now_iso = datetime.now().isoformat(timespec="seconds")
            updates = { idx["status"]: "HOLD", idx["updated_at"]: now_iso }
            if idx["created_at"] is not None and not r[idx["created_at"]]:
                updates[idx["created_at"]] = now_iso
            if idx["patient_full_name"] is not None: updates[idx["patient_full_name"]] = ""
            if idx["patient_phone"] is not None: updates[idx["patient_phone"]] = ""
            _update_row(ws, i, header, updates); return True
    return False

def book_slot(slot_id: str, fio: str, phone: str) -> bool:
    ws = _open_ws(SCHEDULE_SHEET)
    if not ws: return False
    header, data = _read_all(ws); hm = _header_map(header)
    idx = {k: hm.get(_norm(k)) for k in SCHEDULE_HEADERS}
    for i, r in enumerate(data, start=2):
        if r[idx["slot_id"]] == slot_id:
            st = r[idx["status"]].strip().upper()
            if st != "HOLD": return False
            now_iso = datetime.now().isoformat(timespec="seconds")
            updates = {
                idx["status"]: "BOOKED",
                idx["patient_full_name"]: fio,
                idx["patient_phone"]: phone,
                idx["updated_at"]: now_iso,
            }
            _update_row(ws, i, header, updates); return True
    return False

def cancel_booking(slot_id: str) -> bool:
    """BOOKED → FREE, очистка ФИО/тел, обновление updated_at"""
    ws = _open_ws(SCHEDULE_SHEET)
    if not ws: return False
    header, data = _read_all(ws); hm = _header_map(header)
    idx = {k: hm.get(_norm(k)) for k in SCHEDULE_HEADERS}
    for i, r in enumerate(data, start=2):
        if r[idx["slot_id"]] == slot_id:
            st = r[idx["status"]].strip().upper()
            if st != "BOOKED": return False
            now_iso = datetime.now().isoformat(timespec="seconds")
            updates = { idx["status"]: "FREE", idx["updated_at"]: now_iso }
            if idx["patient_full_name"] is not None: updates[idx["patient_full_name"]] = ""
            if idx["patient_phone"] is not None: updates[idx["patient_phone"]] = ""
            _update_row(ws, i, header, updates); return True
    return False

# ======= Requests =======
def requests_append(payload: dict):
    ws = _open_ws(REQUESTS_SHEET)
    if not ws: return
    header, _ = _read_all(ws)
    if not header: ws.append_row(REQUESTS_HEADERS)
    appointment_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        appointment_id,
        payload.get("patient_full_name",""),
        payload.get("patient_phone",""),
        payload.get("doctor_full_name",""),
        payload.get("date",""),
        payload.get("time",""),
        f"{payload.get('date','')}T{payload.get('time','')}:00",
        "Новая",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return appointment_id

# ======= Prices/Prep =======
def prices_search(q: str, limit=5):
    ws = _open_ws(PRICES_SHEET)
    if not ws: return []
    header, data = _read_all(ws); hm = _header_map(header)
    c_code = hm.get("code") or hm.get("код"); c_name = hm.get("name") or hm.get("наименование")
    c_price = hm.get("price") or hm.get("цена"); c_tat = hm.get("tat_days") or hm.get("срок"); c_notes = hm.get("notes") or hm.get("примечания")
    ql = q.lower(); hits = []
    is_code = bool(re.search(r"\d+-\d+-\d+", ql))
    for r in data:
        name = r[c_name] if c_name is not None and c_name < len(r) else ""
        code = r[c_code] if c_code is not None and c_code < len(r) else ""
        if is_code and str(code).strip() == q.strip():
            hits.append((code,name, r[c_price] if c_price is not None and c_price < len(r) else "",
                         r[c_tat] if c_tat is not None and c_tat < len(r) else "",
                         r[c_notes] if c_notes is not None and c_notes < len(r) else ""))
        elif not is_code and ql in str(name).lower():
            hits.append((code,name, r[c_price] if c_price is not None and c_price < len(r) else "",
                         r[c_tat] if c_tat is not None and c_tat < len(r) else "",
                         r[c_notes] if c_notes is not None and c_notes < len(r) else ""))
        if len(hits) >= limit: break
    return hits

def prep_search(q: str, limit=3):
    ws = _open_ws(PREP_SHEET)
    if not ws: return []
    header, data = _read_all(ws); hm = _header_map(header)
    c_name = hm.get("test_name") or hm.get("анализ"); c_memo = hm.get("memo") or hm.get("памятка") or hm.get("подготовка")
    ql = q.lower(); hits = []
    for r in data:
        name = r[c_name] if c_name is not None and c_name < len(r) else ""
        memo = r[c_memo] if c_memo is not None and c_memo < len(r) else ""
        if ql in str(name).lower(): hits.append((name, memo))
        if len(hits) >= limit: break
    return hits

# ======= STATES =======
ASK_DOCTOR, ASK_SLOT, ASK_FIO, ASK_PHONE, ASK_DATE = range(5)

def _slots_keyboard(slots, query, page, date_filter):
    buttons = []
    for s in slots:
        cap = f"{s['doctor_name']} • {s['date']} {s['time']}"
        buttons.append([InlineKeyboardButton(cap, callback_data=f"SLOT__{s['slot_id']}")])
    # навигация
    nav = [
        InlineKeyboardButton("Ещё слоты ⏭️", callback_data=f"NEXTPAGE__{page+1}__{date_filter or ''}"),
        InlineKeyboardButton("На другой день 📅", callback_data="OTHERDAY__ask"),
    ]
    buttons.append(nav)
    # сохранить контекст запроса
    buttons.append([InlineKeyboardButton("◀️ В меню", callback_data="BACKMENU")])
    return InlineKeyboardMarkup(buttons)

# ======= Handlers =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_kb())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_menu_kb())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(f"chat_id: {cid}\nuser_id: {uid}")

async def init_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    created = []
    if ensure_sheet_headers(SCHEDULE_SHEET, SCHEDULE_HEADERS): created.append(SCHEDULE_SHEET)
    if ensure_sheet_headers(REQUESTS_SHEET, REQUESTS_HEADERS): created.append(REQUESTS_SHEET)
    if ensure_sheet_headers(PRICES_SHEET,   PRICES_HEADERS):   created.append(PRICES_SHEET)
    if ensure_sheet_headers(PREP_SHEET,     PREP_HEADERS):     created.append(PREP_SHEET)
    msg = "Готово. "
    if created: msg += "Созданы шапки: " + ", ".join(created)
    await update.message.reply_text(msg or "Ок")

# ---- Запись: вход ----
async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите врача или специализацию (например, Терапевт):")
    context.user_data.clear()
    return ASK_DOCTOR

# ---- Список слотов для врача/спеца ----
async def record_ask_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data["query"] = txt
    context.user_data["page"] = 0
    context.user_data["date_filter"] = None
    slots = find_free_slots(txt, page=0, page_size=3, date_filter=None)
    if not slots:
        await update.message.reply_text("Свободных слотов не нашёл. Попробуйте другого врача/дату.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    await update.message.reply_text("Доступные варианты:", reply_markup=_slots_keyboard(slots, txt, 0, None))
    return ASK_SLOT

# ---- Обработка кнопок слотов/навигации ----
async def record_slot_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    if data.startswith("SLOT__"):
        slot_id = data.split("__",1)[1]
        if not hold_slot(slot_id):
            await q.message.reply_text("Увы, слот только что заняли. Попробуйте другой.", reply_markup=main_menu_kb())
            return ConversationHandler.END
        context.user_data["slot_id"] = slot_id
        await q.message.reply_text("Введите ФИО пациента:")
        return ASK_FIO

    if data.startswith("NEXTPAGE__"):
        _, page_s, date_s = data.split("__",2)
        page = int(page_s); date_filter = date_s or None
        query = context.user_data.get("query","")
        slots = find_free_slots(query, page=page, page_size=3, date_filter=date_filter)
        if not slots:
            await q.message.reply_text("Больше свободных слотов не найдено.", reply_markup=main_menu_kb())
            return ASK_SLOT
        context.user_data["page"] = page
        context.user_data["date_filter"] = date_filter
        await q.message.reply_text("Ещё варианты:", reply_markup=_slots_keyboard(slots, query, page, date_filter))
        return ASK_SLOT

    if data == "OTHERDAY__ask":
        await q.message.reply_text("Укажите дату в формате ГГГГ-ММ-ДД (например, 2025-10-28):")
        return ASK_DATE

    if data == "BACKMENU":
        await q.message.reply_text("Главное меню:", reply_markup=main_menu_kb())
        return ConversationHandler.END

    await q.message.reply_text("Выберите слот из списка.")
    return ASK_SLOT

# ---- Ввод даты для фильтра ----
async def record_set_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_txt = (update.message.text or "").strip()
    try:
        d = dt_parse(date_txt).date().isoformat()
    except Exception:
        await update.message.reply_text("Не распознал дату. Пример: 2025-10-28")
        return ASK_DATE
    context.user_data["date_filter"] = d
    context.user_data["page"] = 0
    query = context.user_data.get("query","")
    slots = find_free_slots(query, page=0, page_size=3, date_filter=d)
    if not slots:
        await update.message.reply_text("На эту дату свободных слотов нет. Попробуйте другую.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    await update.message.reply_text(f"Свободные слоты на {d}:", reply_markup=_slots_keyboard(slots, query, 0, d))
    return ASK_SLOT

# ---- Сбор данных пациента и бронь ----
async def record_get_fio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fio"] = (update.message.text or "").strip()
    await update.message.reply_text("Укажите контактный телефон:")
    return ASK_PHONE

async def record_get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = (update.message.text or "").strip()
    context.user_data["phone"] = phone
    slot_id = context.user_data.get("slot_id"); fio = context.user_data.get("fio")
    if not book_slot(slot_id, fio, phone):
        await update.message.reply_text("Не удалось подтвердить слот (возможно, его заняли). Попробуйте заново.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    # Данные о слоте для журнала
    ws = _open_ws(SCHEDULE_SHEET); header, data = _read_all(ws); hm = _header_map(header)
    idx_id = hm.get("slot_id"); idx_doc = hm.get("doctor_name"); idx_date = hm.get("date"); idx_time = hm.get("time")
    row = next((r for r in data if r[idx_id] == slot_id), None)
    payload = {
        "patient_full_name": fio,
        "patient_phone": phone,
        "doctor_full_name": row[idx_doc] if row else "",
        "date": row[idx_date] if row else "",
        "time": row[idx_time] if row else "",
    }
    appointment_id = requests_append(payload)

    # Уведомление админу
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=(
                    "🆕 *Новая запись*\n"
                    f"*Пациент:* {payload['patient_full_name']}\n"
                    f"*Телефон:* {payload['patient_phone']}\n"
                    f"*Врач:* {payload['doctor_full_name']}\n"
                    f"*Дата:* {payload['date']}  *Время:* {payload['time']}\n"
                    f"*slot_id:* `{slot_id}`\n"
                    f"*ID:* `{appointment_id}`"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            logging.exception("Не удалось отправить уведомление админу")

    await update.message.reply_text(
        f"📝 Запись подтверждена:\n{payload['doctor_full_name']}\n{payload['date']} {payload['time']}\n"
        f"Пациент: {fio}\nТелефон: {phone}", reply_markup=main_menu_kb()
    )
    return ConversationHandler.END

async def record_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Запись отменена.", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ---- Команда отмены брони (для администратора) ----
async def cancel_booking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Использование: /cancel_booking <slot_id>"""
    if not context.args:
        await update.message.reply_text("Укажите slot_id. Пример:\n/cancel_booking DOC01-2025-10-28-09:00")
        return
    slot_id = context.args[0]
    if cancel_booking(slot_id):
        await update.message.reply_text(f"Отмена выполнена. Слот {slot_id} снова FREE.")
    else:
        await update.message.reply_text("Не удалось отменить. Проверьте slot_id или статус слота.")

# ---- Прайс/Памятки (упрощённый роутер) ----
async def prices_or_prep_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if re.search(r"\d+-\d+-\d+", q):
        hits = prices_search(q, limit=5)
        if not hits:
            await update.message.reply_text("По коду ничего не нашёл.")
            return
        lines = []
        for code, name, price, tat, notes in hits:
            line = f"• *{name}*"
            if code: line += f" (`{code}`)"
            if price: line += f" — {price}"
            if tat: line += f", срок: {tat}"
            if notes: line += f"\n  _{notes}_"
            lines.append(line)
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    hits = prep_search(q, limit=3)
    if hits:
        await update.message.reply_text("\n\n".join([f"• *{n}*\n{m}" for n,m in hits]), parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    hits = prices_search(q, limit=5)
    if hits:
        lines = []
        for code, name, price, tat, notes in hits:
            line = f"• *{name}*"
            if code: line += f" (`{code}`)"
            if price: line += f" — {price}"
            if tat: line += f", срок: {tat}"
            if notes: line += f"\n  _{notes}_"
            lines.append(line)
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    await update.message.reply_text("Ничего не нашёл. Уточните запрос.", reply_markup=main_menu_kb())

# ======= Init / Build =======
async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook удалён и pending очищены")
    except Exception:
        logging.exception("Не удалось удалить webhook")

def build_app():
    if not BOT_TOKEN: raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    if not SPREADSHEET_ID: raise SystemExit("❗ GOOGLE_SPREADSHEET_ID не задан")
    app = (ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build())

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu",  menu))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(CommandHandler("whoami",  whoami))
    app.add_handler(CommandHandler("init_sheets",  init_sheets))
    app.add_handler(CommandHandler("cancel_booking",  cancel_booking_cmd))

    # FSM записи
    conv = ConversationHandler(
        entry_points=[CommandHandler("record", record_start), MessageHandler(filters.Regex("^" + re.escape("📅 Запись на приём") + "$"), record_start)],
        states={
            ASK_DOCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, record_ask_slots)],
            ASK_SLOT:   [CallbackQueryHandler(record_slot_nav, pattern=r"^(SLOT__|NEXTPAGE__|OTHERDAY__ask|BACKMENU)")],
            ASK_DATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, record_set_date)],
            ASK_FIO:    [MessageHandler(filters.TEXT & ~filters.COMMAND, record_get_fio)],
            ASK_PHONE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, record_get_phone)],
        },
        fallbacks=[CommandHandler("cancel", record_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Прайс/Памятки
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, prices_or_prep_router), group=2)

    logging.info("Бот запускается (polling)…")
    return app

def main():
    app = build_app()
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
'''
out_file = Path("/mnt/data/bot_v2_actions.py")
out_file.write_text(code, encoding="utf-8")
str(out_file)

