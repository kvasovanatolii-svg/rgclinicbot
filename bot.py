# bot.py — МедНавигатор РГ Клиник (Full v6)
# --------------------------------------------------------------
# ✔ Запись на приём (FREE → BOOKED), пагинация, фильтр по дате
# ✔ Автошапки листов: /init_sheets и /fix_headers
# ✔ Диагностика: /debug_slots [запрос]
# ✔ Инфо-справка 24/7 из листа Info
# ✔ Поиск по Price/Prep (кнопки и свободный текст)
# ✔ Карточки врача из листа Doctors: /doctor и естественные фразы
# ✔ Понимает «график приёма врачей/расписание врачей», фамилии с инициалами
# ✔ Антиконфликт polling: снятие вебхука, подавление спама ошибок
# Требования: python-telegram-bot==20.8, gspread, google-auth, python-dateutil

import os
import re
import json
import time
import logging
from datetime import datetime
from dateutil.parser import parse as dt_parse

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
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
INFO_SHEET      = "Info"
DOCTORS_SHEET   = "Doctors"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# --------- UI ----------
WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\nВыберите раздел ниже:"
HELP    = ("ℹ️ Команды:\n"
           "/menu — меню\n"
           "/init_sheets — создать листы и шапки\n"
           "/fix_headers — принудительно обновить шапки\n"
           "/debug_slots [запрос] — показать видимые слоты\n"
           "/doctor <фамилия|спец> — карточка врача\n"
           "/hours /manager /promos /services /contacts\n"
           "/cancel_booking <slot_id> — снять бронь")

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
    INFO_SHEET:     ["key","value"],
    # DOCTORS_SHEET создаётся импортом: ФИО, Специальность, Стаж, Сертификаты, График приёма, Кабинет, Краткое био
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
        sh.add_worksheet(title=sheet_name, rows=200, cols=30)
        ws = sh.worksheet(sheet_name)
        if sheet_name in HEADERS:
            ws.append_row(HEADERS[sheet_name])
        return ws

def ensure_headers() -> list:
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    created = []
    for name, hdr in HEADERS.items():
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=200, cols=30)
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
            ws = sh.add_worksheet(title=name, rows=200, cols=30)
        ws.update("A1", [hdr])

def read_all(ws):
    vals = ws.get_all_values()
    if not vals: return [], []
    return vals[0], vals[1:]

def header_map(header):
    return {re.sub(r'[^a-z0-9а-я]', '', h.strip().lower()): i for i, h in enumerate(header)}

# --------- Schedule ops ----------
def find_free_slots(query: str, page: int = 0, page_size: int = 3, date_filter: str | None = None):
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    if not header: return []
    hm = header_map(header)
    col = lambda n: hm.get(re.sub(r'[^a-z0-9а-я]','',n))

    idx_status = col("status"); idx_doc = col("doctor_name"); idx_spec = col("specialty")
    idx_date = col("date"); idx_time = col("time"); idx_slot = col("slot_id")

    q = (query or "").strip().lower()
    now = datetime.now()
    pool = []
    for r in data:
        try:
            if idx_status is None or r[idx_status].strip().upper() != "FREE": continue
            doc = r[idx_doc] if idx_doc is not None and idx_doc < len(r) else ""
            sp  = r[idx_spec] if idx_spec is not None and idx_spec < len(r) else ""
            if q and (q not in str(doc).lower()) and (q not in str(sp).lower()): continue
            d = r[idx_date] if idx_date is not None and idx_date < len(r) else ""
            t = r[idx_time] if idx_time is not None and idx_time < len(r) else ""
            if not d or not t: continue
            if date_filter and d != date_filter: continue
            if dt_parse(f"{d} {t}") < now: continue
            pool.append({
                "slot_id": r[idx_slot] if idx_slot is not None and idx_slot < len(r) else "",
                "doctor_name": doc, "specialty": sp, "date": d, "time": t
            })
        except Exception:
            continue
    start = page * page_size
    return pool[start:start+page_size]

def update_slot(slot_id: str, status: str, fio: str = "", phone: str = "") -> bool:
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    if not header: return False
    hm = header_map(header)
    norm = lambda s: re.sub(r'[^a-z0-9а-я]', '', s)

    idx_slot = hm.get(norm("slot_id")); idx_status = hm.get(norm("status"))
    idx_fio = hm.get(norm("patient_full_name")); idx_phone = hm.get(norm("patient_phone"))
    idx_upd = hm.get(norm("updated_at"))

    for i, r in enumerate(data, start=2):
        if idx_slot is not None and idx_slot < len(r) and r[idx_slot] == slot_id:
            row = r[:]
            while len(row) < len(header): row.append("")
            if idx_status is not None: row[idx_status] = status
            if idx_fio    is not None: row[idx_fio]    = fio
            if idx_phone  is not None: row[idx_phone]  = phone
            if idx_upd    is not None: row[idx_upd]    = datetime.now().isoformat(timespec="seconds")
            end_col = chr(64 + len(header))
            ws.update(f"A{i}:{end_col}{i}", [row])
            return True
    return False

def get_slot_info(slot_id: str) -> dict:
    ws = open_ws(SCHEDULE_SHEET)
    header, data = read_all(ws)
    hm = header_map(header); norm = lambda s: re.sub(r'[^a-z0-9а-я]', '', s)
    idx_slot = hm.get(norm("slot_id"))
    gv = lambda row, name: (row[hm.get(norm(name))] if hm.get(norm(name)) is not None and hm.get(norm(name)) < len(row) else "")
    for r in data:
        if idx_slot is not None and idx_slot < len(r) and r[idx_slot] == slot_id:
            return {"doctor_full_name": gv(r,"doctor_name"), "date": gv(r,"date"), "time": gv(r,"time")}
    return {"doctor_full_name": "", "date": "", "time": ""}

def append_request(fio: str, phone: str, doctor: str, date: str, time_: str):
    ws = open_ws(REQUESTS_SHEET)
    header, _ = read_all(ws)
    if not header: ws.append_row(HEADERS[REQUESTS_SHEET])
    now_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([now_id, fio, phone, doctor, date, time_, f"{date}T{time_}:00", "Новая"])

# --------- Prices/Prep/Info/Doctors helpers ----------
def _get_ws_records(sheet_name: str):
    return open_ws(sheet_name).get_all_records()

def prices_search_q(q: str, limit: int = 10):
    rows = _get_ws_records(PRICES_SHEET)
    ql = q.strip().lower()
    is_code = bool(re.search(r"\d+-\d+-\d+|^srv-\d{3}$", ql))
    out = []
    for r in rows:
        name = str(r.get("name","")); code = str(r.get("code",""))
        if (is_code and code.lower() == ql) or (not is_code and ql in name.lower()):
            out.append(r)
        if len(out) >= limit: break
    return out

def prep_search_q(q: str, limit: int = 5):
    rows = _get_ws_records(PREP_SHEET)
    ql = q.strip().lower(); out = []
    for r in rows:
        name = str(r.get("test_name",""))
        if ql in name.lower(): out.append(r)
        if len(out) >= limit: break
    return out

def info_get(key: str, default: str = "") -> str:
    rows = _get_ws_records(INFO_SHEET)
    for r in rows:
        if str(r.get("key","")).strip().lower() == key.strip().lower():
            return str(r.get("value","")).strip()
    return default

def doctors_search(q: str, limit: int = 5):
    rows = _get_ws_records(DOCTORS_SHEET)
    ql = q.strip().lower().replace(".", "")  # убираем точки из инициалов
    out = []
    for r in rows:
        fio  = str(r.get("ФИО","")); spec = str(r.get("Специальность",""))
        if ql in fio.lower().replace(".", "") or ql in spec.lower():
            out.append(r)
        if len(out) >= limit: break
    return out

def format_doctor_cards(items):
    msgs = []
    for r in items:
        msgs.append(
            "👨‍⚕️ *{fio}*\n"
            "Специальность: {spec}\n"
            "Стаж: {exp}\n"
            "Кабинет: {cab}\n"
            "График: {sched}\n"
            "Сертификаты: {cert}\n"
            "Кратко: {bio}".format(
                fio=r.get("ФИО","").strip(),
                spec=r.get("Специальность","").strip(),
                exp=r.get("Стаж","").strip(),
                cab=r.get("Кабинет","").strip(),
                sched=r.get("График приёма","").strip(),
                cert=r.get("Сертификаты","").strip(),
                bio=r.get("Краткое био","").strip(),
            )
        )
    return "\n\n".join(msgs)

# --------- Handlers ----------
ASK_DOCTOR, ASK_SLOT, ASK_FIO, ASK_PHONE, ASK_DATE = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(WELCOME, reply_markup=main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Главное меню:", reply_markup=main_menu())

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

# FSM: запись
async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Введите врача или специализацию (например, Гинеколог):")
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
        context.user_data["slot_id"] = data.split("::", 1)[1]
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
    phone  = (update.message.text or "").strip()
    fio    = context.user_data.get("fio", "")
    slot_id = context.user_data.get("slot_id", "")

    ok = update_slot(slot_id, "BOOKED", fio, phone)
    if not ok:
        await update.message.reply_text("Не удалось подтвердить слот (возможно, его заняли). Попробуйте заново.", reply_markup=main_menu())
        return ConversationHandler.END

    info = get_slot_info(slot_id)
    append_request(fio, phone, info.get("doctor_full_name",""), info.get("date",""), info.get("time",""))

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

# Меню-клики
async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "PRICES":
        await q.message.reply_text("🧾 Напишите название услуги/анализа или код (например, SRV-003, 11-10-001):"); return
    if data == "PREP":
        await q.message.reply_text("ℹ️ Напишите название анализа/исследования — пришлю памятку по подготовке."); return
    if data == "CONTACTS":
        hours = info_get("clinic_hours", "пн–пт 08:00–20:00, сб–вс 09:00–18:00")
        addr  = info_get("clinic_address", "Адрес уточняется")
        phone = info_get("clinic_phone", "+7 (000) 000-00-00")
        await q.message.reply_text(f"📍 РГ Клиник\nАдрес: {addr}\nТел.: {phone}\nРежим работы: {hours}", reply_markup=main_menu()); return

# FAQ команды
async def hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🕘 График работы: {info_get('clinic_hours', 'пн–пт 08:00–20:00; сб–вс 09:00–18:00')}")

async def manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"👤 Руководитель: {info_get('clinic_manager', 'Информация уточняется')}")

async def promos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🎉 Акции:\n{info_get('clinic_promos', 'Сейчас активных акций нет.')}")

async def services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🩺 Услуги клиники:\n{info_get('clinic_services', 'Перечень услуг смотрите в листе Prices.')}")

async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    h = info_get("clinic_hours", "пн–пт 08:00–20:00, сб–вс 09:00–18:00")
    a = info_get("clinic_address", "Адрес уточняется")
    p = info_get("clinic_phone", "+7 (000) 000-00-00")
    await update.message.reply_text(f"📍 РГ Клиник\nАдрес: {a}\nТел.: {p}\nРежим работы: {h}")

# Доктора
async def doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Введите: /doctor <фамилия|специализация>"); return
    items = doctors_search(query, limit=5)
    if not items:
        await update.message.reply_text("Ничего не нашлось. Попробуйте точнее (например, «Смирнова»)."); return
    await update.message.reply_text(format_doctor_cards(items), parse_mode="Markdown")

# Универсальный FAQ-роутер
async def faq_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text: return
    tl = text.lower()

    # Общие запросы про график/расписание врачей
    if any(k in tl for k in ["график врач", "расписание врач", "прием врач", "приёма врач"]):
        docs = _get_ws_records(DOCTORS_SHEET)
        if not docs:
            await update.message.reply_text("Расписание врачей пока не добавлено.")
            return
        lines = []
        for d in docs:
            fio = d.get("ФИО",""); spec = d.get("Специальность","")
            sched = d.get("График приёма",""); cab = d.get("Кабинет","")
            lines.append(f"👨‍⚕️ *{fio}* — {spec}\n📅 {sched}\n🏥 {cab}")
        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())
        return

    # Естественный запрос про врача: «доктор/врач …» или одиночная фамилия (с инициалами)
    import re as _re
    m = _re.search(r"(?:доктор|врач)\s+([A-Za-zА-Яа-яЁё\.\-]+)", text)
    q_doctor = m.group(1) if m else None
    if not q_doctor and _re.fullmatch(r"[А-Яа-яЁё\.\-]{4,}", text):
        q_doctor = text
    if q_doctor:
        q_doctor = q_doctor.replace(".", "").strip()
        items = doctors_search(q_doctor, limit=5) or doctors_search(text, limit=5)
        if items:
            return await update.message.reply_text(format_doctor_cards(items), parse_mode="Markdown", reply_markup=main_menu())

    # Быстрые справки
    if any(k in tl for k in ["график работы","режим работы","часы работы","когда открыты"]): return await hours(update, context)
    if any(k in tl for k in ["руководител","директор","главврач","управляющ"]): return await manager(update, context)
    if any(k in tl for k in ["акци","скидк","предложени"]): return await promos(update, context)
    if any(k in tl for k in ["контакт","адрес","телефон"]): return await contacts(update, context)
    if any(k in tl for k in ["услуг","направлени","что лечите","что делаете"]): return await services(update, context)

    # Памятки → Прайс
    prep_hits = prep_search_q(text, limit=3)
    if prep_hits:
        lines = [f"• *{h.get('test_name','')}*\n{h.get('memo','')}" for h in prep_hits]
        return await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())
    price_hits = prices_search_q(text, limit=5)
    if price_hits:
        lines = []
        for h in price_hits:
            line = f"• *{h.get('name','')}*"
            code=h.get("code",""); price=h.get("price",""); tat=h.get("tat_days",""); notes=h.get("notes","")
            if code: line += f" (`{code}`)"
            if price: line += f" — {price}"
            if tat: line += f", срок: {tat}"
            if notes: line += f"\n  _{notes}_"
            lines.append(line)
        return await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())

    await update.message.reply_text("Я вас понял. Выберите раздел ниже 👇", reply_markup=main_menu())

# --------- App wiring / startup ----------
_last_conflict = 0
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook снят, очередь очищена")
    except Exception:
        logging.exception("Не удалось снять webhook при старте")

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

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

    app.add_handler(CommandHandler("doctor", doctor))
    app.add_handler(CommandHandler("hours", hours))
    app.add_handler(CommandHandler("manager", manager))
    app.add_handler(CommandHandler("promos", promos))
    app.add_handler(CommandHandler("services", services))
    app.add_handler(CommandHandler("contacts", contacts))

    # Кнопки меню
    app.add_handler(CallbackQueryHandler(menu_click, pattern="^(PRICES|PREP|CONTACTS)$"))

    # FSM
    app.add_handler(conv)

    # Универсальный FAQ-роутер
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, faq_router), group=2)

    # Глобальный обработчик ошибок
    async def error_handler(update, context):
        global _last_conflict
        err = context.error
        if isinstance(err, Conflict):
            now = time.time()
            if now - _last_conflict < 60: return
            _last_conflict = now
        logging.exception("Unhandled exception", exc_info=err)
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=f"⚠️ Ошибка: {err}")
            except Exception:
                pass

    app.add_error_handler(error_handler)
    return app

def main():
    if not BOT_TOKEN:       raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    if not SPREADSHEET_ID:  raise SystemExit("❗ GOOGLE_SPREADSHEET_ID не задан")
    if not SERVICE_JSON:    raise SystemExit("❗ GOOGLE_SERVICE_ACCOUNT не задан")
    app = build_app()
    logging.info("Бот запускается (polling)…")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
