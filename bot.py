# bot.py — МедНавигатор РГ Клиник (v8.1)
# Полный функционал + озвучка всех ответов + GPT-fallback для справочной информации

import os
import re
import json
import time
import logging
from io import BytesIO
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

# ---------- Optional TTS ----------
try:
    from gtts import gTTS
    TTS_AVAILABLE = True
except Exception:
    TTS_AVAILABLE = False

from openai import OpenAI

# ---------- ENV ----------
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID   = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON     = os.getenv("GOOGLE_SERVICE_ACCOUNT")
ADMIN_CHAT_ID    = os.getenv("ADMIN_CHAT_ID")

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
VOICE_TEXT_DUP   = os.getenv("VOICE_TEXT_DUPLICATE", "1")  # "1" голос+текст, "0" только голос

SCHEDULE_SHEET = os.getenv("GOOGLE_SCHEDULE_SHEET", "Schedule")
REQUESTS_SHEET = os.getenv("GOOGLE_REQUESTS_SHEET", "Requests")
PRICES_SHEET   = os.getenv("GOOGLE_PRICES_SHEET", "Prices")
PREP_SHEET     = os.getenv("GOOGLE_PREP_SHEET", "Prep")
DOCTORS_SHEET  = "Doctors"
INFO_SHEET     = "Info"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# ---------- UI ----------
WELCOME = "👋 Здравствуйте! Я — МедНавигатор РГ Клиник. Выберите раздел ниже:"
BTN_RECORD   = "📅 Запись на приём"
BTN_PRICES   = "🧾 Цены и анализы"
BTN_PREP     = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RECORD,   callback_data="RECORD")],
        [InlineKeyboardButton(BTN_PRICES,   callback_data="PRICES")],
        [InlineKeyboardButton(BTN_PREP,     callback_data="PREP")],
        [InlineKeyboardButton(BTN_CONTACTS, callback_data="CONTACTS")],
    ])

# ---------- Google Sheets ----------
HEADERS = {
    SCHEDULE_SHEET: ["slot_id","doctor_id","doctor_name","specialty","date","time","tz","status",
                     "patient_full_name","patient_phone","created_at","updated_at"],
    REQUESTS_SHEET: ["appointment_id","patient_full_name","patient_phone","doctor_full_name",
                     "date","time","datetime_iso","status"],
    PRICES_SHEET:   ["code","name","price","tat_days","notes"],
    PREP_SHEET:     ["test_name","memo"],
    INFO_SHEET:     ["key","value"],
}

def gs_client():
    if not SERVICE_JSON:
        raise SystemExit("❗ GOOGLE_SERVICE_ACCOUNT не задан")
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(SERVICE_JSON), scopes=scopes)
    return gspread.authorize(creds)

def open_ws(name: str):
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        sh.add_worksheet(title=name, rows=200, cols=30)
        ws = sh.worksheet(name)
        if name in HEADERS:
            ws.append_row(HEADERS[name])
        return ws

def read_all(ws):
    vals = ws.get_all_values()
    if not vals: return [], []
    return vals[0], vals[1:]

def header_map(header):
    return {re.sub(r'[^a-z0-9а-я]', '', h.strip().lower()): i for i, h in enumerate(header)}

def ensure_headers():
    gc = gs_client(); sh = gc.open_by_key(SPREADSHEET_ID)
    created = []
    for name, hdr in HEADERS.items():
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=200, cols=30)
            ws.append_row(hdr); created.append(name); continue
        if not ws.get_all_values():
            ws.append_row(hdr); created.append(name)
    return created

def fix_headers_force():
    gc = gs_client(); sh = gc.open_by_key(SPREADSHEET_ID)
    for name, hdr in HEADERS.items():
        try: ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=200, cols=30)
        ws.update("A1", [hdr])

# ---------- Safe replies ----------
DEFAULT_EMPTY_REPLY = "Извините, не нашёл информации по запросу. Попробуйте уточнить формулировку 🙏"

def _pick_target(update: Update):
    if getattr(update, "message", None):
        return update.message.reply_text, update.message
    if getattr(update, "callback_query", None) and update.callback_query.message:
        return update.callback_query.message.reply_text, update.callback_query.message
    return None, None

async def _safe_text(update: Update, text: str | None):
    send, _ = _pick_target(update)
    if not send: return
    await send((text or "").strip() or DEFAULT_EMPTY_REPLY)

async def _safe_text_kb(update: Update, text: str | None, kb=None):
    send, _ = _pick_target(update)
    if not send: return
    await send((text or "").strip() or DEFAULT_EMPTY_REPLY, reply_markup=kb)

# ---------- Voice (STT/TTS) ----------
oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
VOICE_MODE_USERS = set()

def is_voice_enabled(uid: int): return uid in VOICE_MODE_USERS

async def stt_transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not oa_client:
        await _safe_text(update, "Распознавание недоступно — нет OPENAI_API_KEY.")
        return ""
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        bio = BytesIO()
        await file.download_to_memory(out=bio)  # PTB 20.x
        bio.seek(0)
        resp = oa_client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", bio, "audio/ogg")
        )
        return getattr(resp, "text", "").strip()
    except Exception as e:
        await _safe_text(update, f"Ошибка распознавания речи: {e}")
        return ""

async def tts_send(update: Update, text: str):
    if not TTS_AVAILABLE:
        await _safe_text(update, text); return
    try:
        mp3 = BytesIO()
        gTTS(text=(text or " "), lang="ru").write_to_fp(mp3)
        mp3.seek(0)
        _, msg = _pick_target(update)
        await msg.chat.send_audio(audio=mp3, filename="reply.mp3", title="Ответ")
    except Exception:
        await _safe_text(update, text)

async def smart_reply(update: Update, text: str):
    send, _ = _pick_target(update)
    if not send: return
    txt = (text or "").strip() or DEFAULT_EMPTY_REPLY
    uid = update.effective_user.id if update.effective_user else 0
    if uid and is_voice_enabled(uid):
        if VOICE_TEXT_DUP == "1":
            await send(txt); await tts_send(update, txt)
        else:
            await tts_send(update, txt)
    else:
        await send(txt)

# ---------- Data helpers ----------
def _get_ws_records(sheet_name: str):
    return open_ws(sheet_name).get_all_records()

def prices_search_q(q: str, limit=10):
    rows = _get_ws_records(PRICES_SHEET); ql = q.strip().lower()
    is_code = bool(re.search(r"\d+-\d+-\d+|^srv-\d{3}$", ql))
    out = []
    for r in rows:
        name = str(r.get("name","")); code = str(r.get("code",""))
        if (is_code and code.lower()==ql) or (not is_code and ql in name.lower()):
            out.append(r)
        if len(out)>=limit: break
    return out

def prep_search_q(q: str, limit=5):
    rows = _get_ws_records(PREP_SHEET); ql = q.strip().lower(); out=[]
    for r in rows:
        name = str(r.get("test_name",""))
        if ql in name.lower(): out.append(r)
        if len(out)>=limit: break
    return out

def info_get(key: str, default=""):
    for r in _get_ws_records(INFO_SHEET):
        if str(r.get("key","")).strip().lower()==key.strip().lower():
            return str(r.get("value","")).strip()
    return default

def doctors_search(q: str, limit=5):
    rows = _get_ws_records(DOCTORS_SHEET); ql = q.strip().lower().replace(".","")
    out=[]
    for r in rows:
        fio=str(r.get("ФИО","")); spec=str(r.get("Специальность",""))
        if ql in fio.lower().replace(".","") or ql in spec.lower():
            out.append(r)
        if len(out)>=limit: break
    return out

def format_doctor_cards(items):
    msgs=[]
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

# ---------- Schedule (поиск/бронь) ----------
def _future_ok(d, t, now):
    try: return dt_parse(f"{d} {t}") >= now
    except Exception: return False

def find_free_slots(query: str, page=0, page_size=3, date_filter=None):
    ws = open_ws(SCHEDULE_SHEET); header, data = read_all(ws)
    if not header: return []
    hm = header_map(header)
    col = lambda n: hm.get(re.sub(r'[^a-z0-9а-я]','',n))
    idx_status=col("status"); idx_doc=col("doctor_name"); idx_spec=col("specialty")
    idx_date=col("date"); idx_time=col("time"); idx_slot=col("slot_id")

    q = (query or "").strip().lower(); now=datetime.now()
    pool=[]
    for r in data:
        try:
            if idx_status is None or r[idx_status].strip().upper()!="FREE": continue
            doc = r[idx_doc] if idx_doc is not None and idx_doc<len(r) else ""
            sp  = r[idx_spec] if idx_spec is not None and idx_spec<len(r) else ""
            if q and (q not in str(doc).lower()) and (q not in str(sp).lower()): continue
            d = r[idx_date] if idx_date is not None and idx_date<len(r) else ""
            t = r[idx_time] if idx_time is not None and idx_time<len(r) else ""
            if not d or not t: continue
            if date_filter and d!=date_filter: continue
            if not _future_ok(d,t,now): continue
            pool.append({"slot_id": r[idx_slot] if idx_slot is not None and idx_slot<len(r) else "",
                         "doctor_name": doc, "specialty": sp, "date": d, "time": t})
        except Exception:
            continue
    start=page*page_size
    return pool[start:start+page_size]

def update_slot(slot_id: str, status: str, fio="", phone="") -> bool:
    ws = open_ws(SCHEDULE_SHEET); header, data = read_all(ws)
    if not header: return False
    hm=header_map(header); norm=lambda s: re.sub(r'[^a-z0-9а-я]','',s)
    idx_slot=hm.get(norm("slot_id")); idx_status=hm.get(norm("status"))
    idx_fio=hm.get(norm("patient_full_name")); idx_phone=hm.get(norm("patient_phone"))
    idx_upd=hm.get(norm("updated_at"))
    for i, r in enumerate(data, start=2):
        if idx_slot is not None and idx_slot<len(r) and r[idx_slot]==slot_id:
            row=r[:]; 
            while len(row)<len(header): row.append("")
            if idx_status is not None: row[idx_status]=status
            if idx_fio    is not None: row[idx_fio]=fio
            if idx_phone  is not None: row[idx_phone]=phone
            if idx_upd    is not None: row[idx_upd]=datetime.now().isoformat(timespec="seconds")
            end_col=chr(64+len(header))
            ws.update(f"A{i}:{end_col}{i}", [row])
            return True
    return False

def get_slot_info(slot_id: str):
    ws=open_ws(SCHEDULE_SHEET); header, data=read_all(ws); hm=header_map(header)
    norm=lambda s: re.sub(r'[^a-z0-9а-я]','',s); idx_slot=hm.get(norm("slot_id"))
    gv=lambda row,name: (row[hm.get(norm(name))] if hm.get(norm(name)) is not None and hm.get(norm(name))<len(row) else "")
    for r in data:
        if idx_slot is not None and idx_slot<len(r) and r[idx_slot]==slot_id:
            return {"doctor_full_name": gv(r,"doctor_name"), "date": gv(r,"date"), "time": gv(r,"time")}
    return {"doctor_full_name":"","date":"","time":""}

def append_request(fio, phone, doctor, date, time_):
    ws=open_ws(REQUESTS_SHEET); header,_=read_all(ws)
    if not header: ws.append_row(HEADERS[REQUESTS_SHEET])
    now_id=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([now_id,fio,phone,doctor,date,time_,f"{date}T{time_}:00","Новая"])

# ---------- AI fallback ----------
def _collect_context_for_ai():
    ctx = {
        "hours": info_get("clinic_hours", ""),
        "address": info_get("clinic_address", ""),
        "phone": info_get("clinic_phone", ""),
        "services": info_get("clinic_services", ""),
        "manager": info_get("clinic_manager", ""),
        "promos": info_get("clinic_promos", ""),
        "doctors": [r.get("ФИО","")+" — "+r.get("Специальность","") for r in _get_ws_records(DOCTORS_SHEET)][:20]
    }
    return ctx

def _ai_prompt_system(ctx):
    return (
        "Ты — 'МедНавигатор РГ Клиник', справочный помощник клиники. "
        "Отвечай только по организации работы клиники и её услугам. "
        "Не давай диагнозов, не назначай лечение, не интерпретируй анализы. "
        "Если вопрос медицинский — мягко предложи записаться к врачу. "
        "Коротко, понятно, дружелюбно. Если ответ неочевиден — скажи, что уточним у администратора.\n\n"
        f"Контекст (справочно):\n"
        f"- Режим работы: {ctx.get('hours')}\n"
        f"- Адрес: {ctx.get('address')}\n"
        f"- Телефон: {ctx.get('phone')}\n"
        f"- Руководитель: {ctx.get('manager')}\n"
        f"- Услуги: {ctx.get('services')}\n"
        f"- Акции: {ctx.get('promos')}\n"
        f"- Врачи: {', '.join(ctx.get('doctors', []))}\n"
        "Если информации нет — так и скажи и предложи связать с администратором."
    )

def ai_answer(question: str) -> str:
    if not OPENAI_API_KEY:
        return ""
    client = oa_client or OpenAI(api_key=OPENAI_API_KEY)
    ctx = _collect_context_for_ai()
    system = _ai_prompt_system(ctx)
    try:
        # компактная и дешёвая модель, можно заменить на gpt-4o
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question.strip()},
            ],
            temperature=0.3,
            max_tokens=400,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text
    except Exception as e:
        logging.exception("AI answer failed: %s", e)
        return ""

# ---------- Handlers ----------
ASK_DOCTOR, ASK_SLOT, ASK_FIO, ASK_PHONE, ASK_DATE = range(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await smart_reply(update, WELCOME)
    await _safe_text_kb(update, "Главное меню:", main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_text_kb(update, "Главное меню:", main_menu())

async def init_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    created = ensure_headers()
    await smart_reply(update, "Все листы уже есть ✅" if not created else f"Созданы листы/шапки: {', '.join(created)}")

async def fix_headers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fix_headers_force()
    await smart_reply(update, "✅ Заголовки колонок обновлены: " + ", ".join(HEADERS.keys()))

async def cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await smart_reply(update, "Укажите slot_id. Пример:\n/cancel_booking DOC01-2025-10-28-09:00"); return
    ok = update_slot(context.args[0], "FREE", "", "")
    await smart_reply(update, "✅ Слот освобождён" if ok else "❌ Не удалось отменить (slot_id/статус).")

async def debug_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=" ".join(context.args).strip() if context.args else ""
    try: slots=find_free_slots(query, page=0, page_size=10, date_filter=None)
    except Exception as e:
        await smart_reply(update, f"⚠️ Ошибка чтения таблицы: {e}"); return
    if not slots: await smart_reply(update, "🔍 Свободных слотов не найдено."); return
    text="Найденные FREE-слоты:\n" + "\n".join([f"• {s['doctor_name']} • {s['specialty']} • {s['date']} {s['time']} • `{s['slot_id']}`" for s in slots])
    await smart_reply(update, text)

# --- FSM: запись
async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await smart_reply(update, "Введите врача или специализацию (например, Гинеколог):")
    return ASK_DOCTOR

async def record_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=(update.message.text or "").strip()
    context.user_data["query"]=q; context.user_data["page"]=0; context.user_data["date_filter"]=None
    slots=find_free_slots(q, page=0, page_size=3, date_filter=None)
    if not slots:
        await smart_reply(update, "Свободных слотов не найдено 😔")
        await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return ConversationHandler.END
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=f"SLOT::{s['slot_id']}")] for s in slots] +
        [[InlineKeyboardButton("Ещё слоты ⏭️","MORE"), InlineKeyboardButton("На другой день 📅","ASKDATE")]]
    )
    await _safe_text_kb(update, "Выберите слот:", kb); return ASK_SLOT

async def record_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); data=q.data
    if data=="MORE":
        page=context.user_data.get("page",0)+1; context.user_data["page"]=page
        slots=find_free_slots(context.user_data.get("query",""), page=page, page_size=3,
                              date_filter=context.user_data.get("date_filter"))
        if not slots:
            await _safe_text_kb(update, "Больше слотов не найдено.", main_menu()); return ConversationHandler.END
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=f"SLOT::{s['slot_id']}")] for s in slots] +
            [[InlineKeyboardButton("Ещё слоты ⏭️","MORE"), InlineKeyboardButton("На другой день 📅","ASKDATE")]]
        )
        await _safe_text_kb(update, "Ещё варианты:", kb); return ASK_SLOT

    if data=="ASKDATE":
        await _safe_text(update, "Введите дату (ГГГГ-ММ-ДД):"); return ASK_DATE

    if data.startswith("SLOT::"):
        context.user_data["slot_id"]=data.split("::",1)[1]
        await _safe_text(update, "Введите ФИО пациента:"); return ASK_FIO

    await _safe_text(update, "Пожалуйста, выберите слот из списка."); return ASK_SLOT

async def record_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt=(update.message.text or "").strip()
    try: d=dt_parse(txt).date().isoformat()
    except Exception:
        await smart_reply(update, "Не распознал дату. Пример: 2025-10-28"); return ASK_DATE
    context.user_data["date_filter"]=d; context.user_data["page"]=0
    slots=find_free_slots(context.user_data.get("query",""), page=0, page_size=3, date_filter=d)
    if not slots:
        await smart_reply(update, "На эту дату свободных слотов нет.")
        await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return ConversationHandler.END
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{s['doctor_name']} • {s['date']} {s['time']}", callback_data=f"SLOT::{s['slot_id']}")] for s in slots] +
        [[InlineKeyboardButton("Ещё слоты ⏭️","MORE"), InlineKeyboardButton("На другой день 📅","ASKDATE")]]
    )
    await _safe_text_kb(update, f"Свободные слоты на {d}:", kb); return ASK_SLOT

async def record_fio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fio"]=(update.message.text or "").strip()
    await smart_reply(update, "Введите телефон:"); return ASK_PHONE

async def record_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone=(update.message.text or "").strip()
    fio=context.user_data.get("fio",""); slot_id=context.user_data.get("slot_id","")
    if not update_slot(slot_id, "BOOKED", fio, phone):
        await smart_reply(update, "Не удалось подтвердить слот (возможно, его заняли). Попробуйте заново.")
        await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return ConversationHandler.END
    info=get_slot_info(slot_id); append_request(fio, phone, info.get("doctor_full_name",""), info.get("date",""), info.get("time",""))
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID),
                text=(f"🆕 Новая запись\nПациент: {fio}\nТелефон: {phone}\nВрач: {info.get('doctor_full_name','')}\n"
                      f"Дата: {info.get('date','')} {info.get('time','')}\nslot_id: {slot_id}"))
        except Exception: logging.exception("notify admin failed")
    await smart_reply(update,
        f"✅ Запись подтверждена:\n{info.get('doctor_full_name','')}\n{info.get('date','')} {info.get('time','')}\nПациент: {fio}\nТелефон: {phone}")
    await _safe_text_kb(update, "Главное меню:", main_menu()); return ConversationHandler.END

# --- Меню-клики (все ответы через smart_reply -> озвучиваются)
async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); data=q.data
    if data=="PRICES":
        await smart_reply(update, "🧾 Напишите название услуги/анализа или код (например, SRV-003, 11-10-001)")
        return
    if data=="PREP":
        await smart_reply(update, "ℹ️ Напишите название анализа/исследования — пришлю памятку по подготовке.")
        return
    if data=="CONTACTS":
        hours=info_get("clinic_hours","пн–пт 08:00–20:00, сб–вс 09:00–18:00")
        addr=info_get("clinic_address","Адрес уточняется")
        phone=info_get("clinic_phone","+7 (000) 000-00-00")
        await smart_reply(update, f"📍 РГ Клиник\nАдрес: {addr}\nТел.: {phone}\nРежим работы: {hours}")
        await _safe_text_kb(update, "Главное меню:", main_menu())
        return

# --- FAQ router (правила + GPT fallback)
async def faq_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text=(update.message.text or "").strip()
    if not text: return
    tl=text.lower()

    # Расписание врачей
    if any(k in tl for k in ["график врач","расписание врач","прием врач","приёма врач"]):
        docs=_get_ws_records(DOCTORS_SHEET)
        if not docs: await smart_reply(update,"Расписание врачей пока не добавлено."); return
        lines=[]
        for d in docs:
            lines.append(f"👨‍⚕️ *{d.get('ФИО','')}* — {d.get('Специальность','')}\n📅 {d.get('График приёма','')}\n🏥 {d.get('Кабинет','')}")
        await smart_reply(update, "\n\n".join(lines))
        await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return

    # «доктор/врач ...» либо одиночная фамилия
    m=re.search(r"(?:доктор|врач)\s+([A-Za-zА-Яа-яЁё\.\-]+)", text)
    q_doctor=m.group(1) if m else None
    if not q_doctor and re.fullmatch(r"[А-Яа-яЁё\.\-]{4,}", text): q_doctor=text
    if q_doctor:
        q_doctor=q_doctor.replace(".","").strip()
        items=doctors_search(q_doctor,5) or doctors_search(text,5)
        if items:
            await smart_reply(update, format_doctor_cards(items))
            await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return

    # Быстрые справки из Info
    if any(k in tl for k in ["график работы","режим работы","часы работы","когда открыты"]):
        await smart_reply(update, f"🕘 График работы: {info_get('clinic_hours','пн–пт 08:00–20:00; сб–вс 09:00–18:00')}"); return
    if any(k in tl for k in ["руководител","директор","главврач","управляющ"]):
        await smart_reply(update, f"👤 Руководитель: {info_get('clinic_manager','Информация уточняется')}"); return
    if any(k in tl for k in ["акци","скидк","предложени"]):
        await smart_reply(update, f"🎉 Акции:\n{info_get('clinic_promos','Сейчас активных акций нет.')}"); return
    if any(k in tl for k in ["контакт","адрес","телефон"]):
        h=info_get("clinic_hours","пн–пт 08:00–20:00, сб–вс 09:00–18:00")
        a=info_get("clinic_address","Адрес уточняется")
        p=info_get("clinic_phone","+7 (000) 000-00-00")
        await smart_reply(update, f"📍 РГ Клиник\nАдрес: {a}\nТел.: {p}\nРежим работы: {h}"); return
    if any(k in tl for k in ["услуг","направлени","что лечите","что делаете"]):
        await smart_reply(update, f"🩺 Услуги клиники:\n{info_get('clinic_services','Перечень услуг — в листе Prices.')}"); return

    # Памятки / Прайс
    prep_hits=prep_search_q(text,3)
    if prep_hits:
        await smart_reply(update, "\n\n".join([f"• *{h.get('test_name','')}*\n{h.get('memo','')}" for h in prep_hits]))
        await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return
    price_hits=prices_search_q(text,5)
    if price_hits:
        lines=[]
        for h in price_hits:
            line=f"• *{h.get('name','')}*"
            if h.get("code"):     line += f" (`{h.get('code')}`)"
            if h.get("price"):    line += f" — {h.get('price')}"
            if h.get("tat_days"): line += f", срок: {h.get('tat_days')}"
            if h.get("notes"):    line += f"\n  _{h.get('notes')}_"
            lines.append(line)
        await smart_reply(update, "\n".join(lines))
        await _safe_text_kb(update, "Выберите раздел ниже 👇", main_menu()); return

    # ---- GPT fallback (справочно) ----
    ai = ai_answer(text)
    if ai:
        await smart_reply(update, ai)
        await _safe_text_kb(update, "Нужна запись или другая справка? Выберите ниже 👇", main_menu())
        return

    # если совсем ничего
    await _safe_text_kb(update, "Я вас понял. Выберите раздел ниже 👇", main_menu())

# --- Голосовые сообщения
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await stt_transcribe_voice(update, context)
    if not text: return
    await smart_reply(update, f"🗣 Распознал: {text}")
    context.user_data["_override_text"]=text
    await faq_router(update, context)
    context.user_data.pop("_override_text", None)

# --- Голосовой режим: переключатели
async def voice_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    VOICE_MODE_USERS.add(update.effective_user.id)
    await smart_reply(update, "🔊 Голосовой помощник включён.")
async def voice_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    VOICE_MODE_USERS.discard(update.effective_user.id)
    await smart_reply(update, "🔕 Голосовой помощник выключен.")
async def voice_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    on="включён" if is_voice_enabled(update.effective_user.id) else "выключен"
    mode="голос+текст" if VOICE_TEXT_DUP=="1" else "только голос"
    await smart_reply(update, f"ℹ️ Режим: {on} ({mode})")

# --- Error handler
_last_conflict=0
async def error_handler(update, context):
    global _last_conflict
    err=context.error
    if isinstance(err, Conflict):
        now=time.time()
        if now-_last_conflict<60: return
        _last_conflict=now
    logging.exception("Unhandled error", exc_info=err)
    if ADMIN_CHAT_ID:
        try: await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=f"⚠️ Ошибка: {err}")
        except Exception: pass

# --- App wiring
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(lambda u,c: record_start(u,c), pattern="RECORD"),
            MessageHandler(filters.Regex("^📅 Запись на приём$"), record_start),
        ],
        states={
            ASK_DOCTOR:[MessageHandler(filters.TEXT & ~filters.COMMAND, record_doctor)],
            ASK_SLOT:[CallbackQueryHandler(record_slot)],
            ASK_DATE:[MessageHandler(filters.TEXT & ~filters.COMMAND, record_date)],
            ASK_FIO:[MessageHandler(filters.TEXT & ~filters.COMMAND, record_fio)],
            ASK_PHONE:[MessageHandler(filters.TEXT & ~filters.COMMAND, record_phone)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("init_sheets", init_sheets))
    app.add_handler(CommandHandler("fix_headers", fix_headers))
    app.add_handler(CommandHandler("debug_slots", debug_slots))
    app.add_handler(CommandHandler("cancel_booking", cancel_booking))

    # голосовой режим
    app.add_handler(CommandHandler("voice_on", voice_on))
    app.add_handler(CommandHandler("voice_off", voice_off))
    app.add_handler(CommandHandler("voice_status", voice_status))

    # кнопки
    app.add_handler(CallbackQueryHandler(menu_click, pattern="^(PRICES|PREP|CONTACTS)$"))

    # FSM и обработчики
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, faq_router), group=2)

    app.add_error_handler(error_handler)
    return app

def main():
    if not BOT_TOKEN:      raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан")
    if not SPREADSHEET_ID: raise SystemExit("❗ GOOGLE_SPREADSHEET_ID не задан")
    if not SERVICE_JSON:   raise SystemExit("❗ GOOGLE_SERVICE_ACCOUNT не задан")

    app=build_app()
    logging.info("✅ Бот запускается (polling)…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
