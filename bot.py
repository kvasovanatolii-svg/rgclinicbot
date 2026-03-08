from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
if not GOOGLE_SHEETS_ID:
    raise RuntimeError("GOOGLE_SHEETS_ID is required")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("rgclinic.v15b")

# =========================
# UI
# =========================

BTN_BOOKING = "📅 Запись"
BTN_DOCTORS = "👨‍⚕️ Врачи"
BTN_PRICES = "🧾 Цены"
BTN_PREP = "ℹ️ Подготовка"
BTN_CONTACTS = "📍 Контакты"
BTN_BACK = "⬅️ Назад в меню"
BTN_CANCEL = "❌ Отмена"

MENU_ALIASES = {
    "запись": BTN_BOOKING,
    "врачи": BTN_DOCTORS,
    "цены": BTN_PRICES,
    "подготовка": BTN_PREP,
    "контакты": BTN_CONTACTS,
    "назад": BTN_BACK,
    "назад в меню": BTN_BACK,
    "отмена": BTN_CANCEL,
}

(
    BOOK_CHOOSE_DOCTOR,
    BOOK_CHOOSE_DATE,
    BOOK_CHOOSE_TIME,
    BOOK_ENTER_NAME,
    BOOK_ENTER_PHONE,
    WAIT_PRICE_QUERY,
    WAIT_PREP_QUERY,
) = range(7)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_BOOKING), KeyboardButton(BTN_DOCTORS)],
            [KeyboardButton(BTN_PRICES), KeyboardButton(BTN_PREP)],
            [KeyboardButton(BTN_CONTACTS)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие",
    )


def sub_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_CANCEL)], [KeyboardButton(BTN_BACK)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# =========================
# HELPERS
# =========================

def s(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def norm(x: Any) -> str:
    txt = s(x).lower().replace("ё", "е")
    txt = re.sub(r"[^\wа-я]+", "", txt)
    return txt


def menu_text(x: str) -> str:
    clean = re.sub(r"[^\wа-яА-ЯёЁ ]+", " ", s(x))
    clean = re.sub(r"\s+", " ", clean).strip().lower().replace("ё", "е")
    return MENU_ALIASES.get(clean, s(x).strip())


def only_digits(x: str) -> str:
    return re.sub(r"\D+", "", s(x))


def valid_phone(phone: str) -> bool:
    return len(only_digits(phone)) >= 10


def parse_price(value: str) -> Optional[float]:
    txt = s(value)
    if not txt:
        return None
    txt = txt.replace("₽", "").replace("р.", "").replace("р", "")
    txt = txt.replace(" ", "").replace("\xa0", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", txt)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def price_text(value: str) -> str:
    p = parse_price(value)
    if p is None:
        return s(value) or "уточняется"
    if p.is_integer():
        return f"{int(p):,}".replace(",", " ") + " ₽"
    return f"{p:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def parse_date(value: str) -> Optional[datetime]:
    txt = s(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(txt, fmt)
        except Exception:
            continue
    return None


def parse_time(value: str) -> str:
    txt = s(value)
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", txt)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return txt


def now_local() -> datetime:
    return datetime.now()


def truncate(text: str, limit: int = 3800) -> str:
    text = s(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# =========================
# DATA MODELS
# =========================

@dataclass
class Doctor:
    rownum: int
    fio: str
    specialty: str
    experience: str
    certificates: str
    schedule: str
    cabinet: str
    bio: str


@dataclass
class PriceItem:
    rownum: int
    code: str
    name: str
    price: str
    ready_time: str
    note: str


@dataclass
class PrepItem:
    rownum: int
    analysis: str
    prep: str


@dataclass
class SlotItem:
    rownum: int
    slot_id: str
    doctor_name: str
    specialty: str
    date: str
    time: str
    status: str
    patient_name: str
    patient_phone: str


# =========================
# GOOGLE SHEETS
# =========================

class SheetsRepo:
    def __init__(self, spreadsheet_id: str):
        self.spreadsheet_id = spreadsheet_id
        self.gc = self._auth()
        self.sh = self.gc.open_by_key(spreadsheet_id)
        self.cache: Dict[str, Tuple[datetime, Any]] = {}

    def _auth(self) -> gspread.Client:
        inline_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        json_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

        if inline_json:
            creds = Credentials.from_service_account_info(json.loads(inline_json), scopes=SCOPES)
            return gspread.authorize(creds)
        if json_file:
            creds = Credentials.from_service_account_file(json_file, scopes=SCOPES)
            return gspread.authorize(creds)
        if os.path.exists("service_account.json"):
            creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
            return gspread.authorize(creds)
        raise RuntimeError("Google service account credentials not found")

    def ws(self, title: str):
        return self.sh.worksheet(title)

    def get_cached(self, key: str):
        item = self.cache.get(key)
        if not item:
            return None
        ts, value = item
        if (now_local() - ts).total_seconds() > CACHE_TTL_SECONDS:
            return None
        return value

    def set_cached(self, key: str, value: Any):
        self.cache[key] = (now_local(), value)

    def invalidate(self, *keys: str):
        if not keys:
            self.cache.clear()
            return
        for k in keys:
            self.cache.pop(k, None)

    def all_values(self, title: str) -> List[List[str]]:
        key = f"raw:{title}"
        cached = self.get_cached(key)
        if cached is not None:
            return cached
        data = self.ws(title).get_all_values()
        self.set_cached(key, data)
        return data

    def header_map(self, title: str) -> Dict[str, int]:
        values = self.all_values(title)
        if not values:
            return {}
        return {norm(col): i for i, col in enumerate(values[0]) if s(col)}

    def find_col(self, title: str, aliases: List[str]) -> Optional[int]:
        hmap = self.header_map(title)
        for a in aliases:
            idx = hmap.get(norm(a))
            if idx is not None:
                return idx
        return None

    def rows(self, title: str, aliases: Dict[str, List[str]], min_non_empty: int = 1) -> List[Dict[str, Any]]:
        values = self.all_values(title)
        if not values:
            return []
        header = values[0]
        idx_map = {field: self.find_col(title, al) for field, al in aliases.items()}
        result = []
        for rownum, row in enumerate(values[1:], start=2):
            if not any(s(v) for v in row):
                continue
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            item: Dict[str, Any] = {"_rownum": rownum}
            non_empty = 0
            for field, idx in idx_map.items():
                val = row[idx] if idx is not None and idx < len(row) else ""
                val = s(val)
                if val:
                    non_empty += 1
                item[field] = val
            if non_empty >= min_non_empty:
                result.append(item)
        return result

    # -------- reading --------

    def info(self) -> Dict[str, str]:
        key = "info"
        cached = self.get_cached(key)
        if cached is not None:
            return cached

        values = self.all_values("Инфо")
        result: Dict[str, str] = {}
        if not values:
            return result

        k_idx = self.find_col("Инфо", ["Ключ", "key"]) or 0
        v_idx = self.find_col("Инфо", ["Значение", "value"]) or 1

        for row in values[1:]:
            if len(row) <= max(k_idx, v_idx):
                row = row + [""] * (max(k_idx, v_idx) + 1 - len(row))
            key_name = norm(row[k_idx])
            val = s(row[v_idx])
            if key_name:
                result[key_name] = val

        self.set_cached(key, result)
        return result

    def doctors(self) -> List[Doctor]:
        key = "doctors"
        cached = self.get_cached(key)
        if cached is not None:
            return cached

        rows = self.rows("Врачи", {
            "fio": ["ФИО", "Врач", "doctor_name", "doctor", "name"],
            "specialty": ["Специальность", "specialty"],
            "experience": ["Стаж", "experience"],
            "certificates": ["Сертификаты", "certificate", "certificates"],
            "schedule": ["График приёма", "График приема", "График", "schedule"],
            "cabinet": ["Кабинет", "cabinet", "room"],
            "bio": ["Краткое био", "Био", "bio", "описание"],
        }, min_non_empty=1)

        data = [
            Doctor(
                rownum=r["_rownum"],
                fio=r["fio"],
                specialty=r["specialty"],
                experience=r["experience"],
                certificates=r["certificates"],
                schedule=r["schedule"],
                cabinet=r["cabinet"],
                bio=r["bio"],
            )
            for r in rows
            if r["fio"] or r["specialty"]
        ]
        self.set_cached(key, data)
        return data

    def prices(self) -> List[PriceItem]:
        key = "prices"
        cached = self.get_cached(key)
        if cached is not None:
            return cached

        rows = self.rows("Цены", {
            "code": ["Код", "code", "id"],
            "name": ["Название", "Анализ", "Услуга", "name", "service"],
            "price": ["Цена", "Стоимость", "price"],
            "ready_time": ["Срок готовности", "Готовность", "ready_time"],
            "note": ["Примечание", "Комментарий", "note"],
        }, min_non_empty=1)

        data = [
            PriceItem(
                rownum=r["_rownum"],
                code=r["code"],
                name=r["name"],
                price=r["price"],
                ready_time=r["ready_time"],
                note=r["note"],
            )
            for r in rows
            if r["code"] or r["name"]
        ]
        self.set_cached(key, data)
        return data

    def preps(self) -> List[PrepItem]:
        key = "preps"
        cached = self.get_cached(key)
        if cached is not None:
            return cached

        rows = self.rows("Подготовка", {
            "analysis": ["Анализ", "Название", "name", "analysis"],
            "prep": ["Подготовка", "prep", "preparation", "описание"],
        }, min_non_empty=1)

        data = [
            PrepItem(rownum=r["_rownum"], analysis=r["analysis"], prep=r["prep"])
            for r in rows
            if r["analysis"] or r["prep"]
        ]
        self.set_cached(key, data)
        return data

    def slots(self) -> List[SlotItem]:
        key = "slots"
        cached = self.get_cached(key)
        if cached is not None:
            return cached

        rows = self.rows("Расписание", {
            "slot_id": ["ID слота", "slot_id", "id слота", "id"],
            "doctor_name": ["Врач", "doctor_name", "doctor", "ФИО"],
            "specialty": ["Специальность", "specialty"],
            "date": ["Дата", "date"],
            "time": ["Время", "time"],
            "status": ["Статус", "status"],
            "patient_name": ["Пациент", "patient_name", "patient_full_name", "ФИО пациента"],
            "patient_phone": ["Телефон", "phone", "patient_phone"],
        }, min_non_empty=2)

        data = [
            SlotItem(
                rownum=r["_rownum"],
                slot_id=r["slot_id"] or f"row-{r['_rownum']}",
                doctor_name=r["doctor_name"],
                specialty=r["specialty"],
                date=r["date"],
                time=parse_time(r["time"]),
                status=s(r["status"]).upper(),
                patient_name=r["patient_name"],
                patient_phone=r["patient_phone"],
            )
            for r in rows
        ]
        self.set_cached(key, data)
        return data

    # -------- writing --------

    def require_col(self, title: str, aliases: List[str]) -> int:
        idx = self.find_col(title, aliases)
        if idx is None:
            raise RuntimeError(f"Column not found in '{title}': {aliases}")
        return idx + 1  # 1-based for update_cell

    def book_slot(self, slot: SlotItem, patient_name: str, patient_phone: str):
        ws = self.ws("Расписание")
        status_col = self.require_col("Расписание", ["Статус", "status"])
        patient_col = self.require_col("Расписание", ["Пациент", "patient_name", "patient_full_name", "ФИО пациента"])
        phone_col = self.require_col("Расписание", ["Телефон", "phone", "patient_phone"])

        ws.update_cell(slot.rownum, status_col, "BOOKED")
        ws.update_cell(slot.rownum, patient_col, patient_name)
        ws.update_cell(slot.rownum, phone_col, patient_phone)
        self.invalidate("slots", "raw:Расписание")

    def append_booking(self, patient_name: str, patient_phone: str, doctor_name: str, date: str, time: str):
        ws = self.ws("Записи")
        header = self.all_values("Записи")[0]
        row = []
        stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
        for col in header:
            coln = norm(col)
            if coln in (norm("ID записи"), norm("id"), norm("id записи")):
                row.append(stamp)
            elif coln in (norm("Пациент"), norm("patient")):
                row.append(patient_name)
            elif coln in (norm("Телефон"), norm("phone")):
                row.append(patient_phone)
            elif coln in (norm("Врач"), norm("doctor")):
                row.append(doctor_name)
            elif coln in (norm("Дата"), norm("date")):
                row.append(date)
            elif coln in (norm("Время"), norm("time")):
                row.append(time)
            elif coln in (norm("Статус"), norm("status")):
                row.append("Новая")
            else:
                row.append("")
        ws.append_row(row, value_input_option="USER_ENTERED")
        self.invalidate("raw:Записи")

repo = SheetsRepo(GOOGLE_SHEETS_ID)

# =========================
# SEARCH / BUSINESS LOGIC
# =========================

def info_value(*aliases: str) -> str:
    info = repo.info()
    for a in aliases:
        val = info.get(norm(a))
        if val:
            return val
    return ""

def list_specialties(doctors: List[Doctor]) -> str:
    specs = sorted({d.specialty for d in doctors if d.specialty})
    return ", ".join(specs)

def find_prices(query: str, limit: int = 10) -> List[PriceItem]:
    q = norm(query)
    q_words = [norm(w) for w in re.split(r"\s+", s(query)) if norm(w)]
    items = []
    for p in repo.prices():
        hay = " ".join([p.code, p.name, p.note]).lower().replace("ё", "е")
        hnorm = norm(hay)
        score = 0
        if q and q in hnorm:
            score += 100
        if q and q == norm(p.code):
            score += 200
        if q and q == norm(p.name):
            score += 180
        for w in q_words:
            if w and w in hnorm:
                score += 10
        if score > 0:
            items.append((score, p))
    items.sort(key=lambda x: (-x[0], x[1].name))
    return [p for _, p in items[:limit]]

def find_preps(query: str, limit: int = 5) -> List[PrepItem]:
    q = norm(query)
    q_words = [norm(w) for w in re.split(r"\s+", s(query)) if norm(w)]
    items = []
    for p in repo.preps():
        hay = " ".join([p.analysis, p.prep]).lower().replace("ё", "е")
        hnorm = norm(hay)
        score = 0
        if q and q in hnorm:
            score += 100
        if q and q == norm(p.analysis):
            score += 180
        for w in q_words:
            if w and w in hnorm:
                score += 10
        if score > 0:
            items.append((score, p))
    items.sort(key=lambda x: (-x[0], x[1].analysis))
    return [p for _, p in items[:limit]]

def available_slots_by_doctor(doctor_name: str) -> List[SlotItem]:
    today = now_local().date()
    result = []
    for slot in repo.slots():
        if norm(slot.doctor_name) != norm(doctor_name):
            continue
        if slot.status != "FREE":
            continue
        dt = parse_date(slot.date)
        if dt and dt.date() < today:
            continue
        result.append(slot)
    result.sort(key=lambda x: ((parse_date(x.date) or now_local()).date(), parse_time(x.time)))
    return result

def group_slots_by_date(slots: List[SlotItem]) -> Dict[str, List[SlotItem]]:
    grouped: Dict[str, List[SlotItem]] = {}
    for sl in slots:
        grouped.setdefault(sl.date, []).append(sl)
    return grouped

def choose_slot_by_id(slot_id: str) -> Optional[SlotItem]:
    for slot in repo.slots():
        if s(slot.slot_id) == s(slot_id):
            return slot
    return None

def format_doctor(d: Doctor) -> str:
    parts = [f"👨‍⚕️ <b>{d.fio or 'Врач'}</b>"]
    if d.specialty:
        parts.append(f"Специальность: {d.specialty}")
    if d.experience:
        parts.append(f"Стаж: {d.experience}")
    if d.schedule:
        parts.append(f"График: {d.schedule}")
    if d.cabinet:
        parts.append(f"Кабинет: {d.cabinet}")
    if d.bio:
        parts.append(f"Био: {d.bio}")
    return "\n".join(parts)

def format_price_item(item: PriceItem) -> str:
    lines = [f"🧾 <b>{item.name or 'Услуга'}</b>"]
    if item.code:
        lines.append(f"Код: {item.code}")
    if item.price:
        lines.append(f"Цена: {price_text(item.price)}")
    if item.ready_time:
        lines.append(f"Срок готовности: {item.ready_time}")
    if item.note:
        lines.append(f"Примечание: {item.note}")
    return "\n".join(lines)

def format_prep_item(item: PrepItem) -> str:
    lines = [f"ℹ️ <b>{item.analysis}</b>"]
    if item.prep:
        lines.append(item.prep)
    return "\n".join(lines)

def contacts_text() -> str:
    address = info_value("clinic_address", "address")
    phone = info_value("clinic_phone", "phone")
    hours = info_value("clinic_hours", "hours")
    manager = info_value("clinic_manager", "manager")
    services = info_value("clinic_services", "services")
    promos = info_value("clinic_promos", "promos")

    lines = ["📍 <b>Контакты РГ Клиник</b>"]
    if address:
        lines.append(f"Адрес: {address}")
    if phone:
        lines.append(f"Телефон: {phone}")
    if hours:
        lines.append(f"Режим работы: {hours}")
    if manager:
        lines.append(f"Руководитель / главный врач: {manager}")
    if promos:
        lines.append(f"\nАкции/предложения:\n{promos}")
    if services:
        lines.append(f"\nНаправления клиники:\n{services}")
    if len(lines) == 1:
        lines.append("Справочная информация временно недоступна. Пожалуйста, уточните у администратора.")
    return "\n".join(lines)

def smart_answer(text: str) -> Optional[str]:
    t = s(text).lower().replace("ё", "е")

    if any(x in t for x in ["руководител", "главный врач", "директор"]):
        manager = info_value("clinic_manager", "manager")
        if manager:
            return f"👨‍⚕️ <b>Руководитель / главный врач клиники:</b>\n{manager}"
        return "Информация о руководителе временно уточняется. Для точной информации обратитесь к администратору."

    if "адрес" in t or "где вы" in t or "где находится" in t:
        address = info_value("clinic_address", "address")
        return f"📍 <b>Адрес клиники:</b>\n{address}" if address else "Адрес временно уточняется."

    if "телефон" in t or "номер" in t or "позвон" in t:
        phone = info_value("clinic_phone", "phone")
        return f"📞 <b>Телефон клиники:</b>\n{phone}" if phone else "Телефон временно уточняется."

    if "режим" in t or "часы работы" in t or "когда работает" in t or "график работы" in t:
        hours = info_value("clinic_hours", "hours")
        return f"🕒 <b>Режим работы:</b>\n{hours}" if hours else "Режим работы временно уточняется."

    if "какие специалисты" in t or "какие врачи" in t or "специалисты" in t:
        docs = repo.doctors()
        specs = list_specialties(docs)
        if specs:
            return f"👨‍⚕️ <b>Специалисты клиники:</b>\n{specs}"
        return "Список специалистов временно недоступен."

    # быстрый поиск по ценам/подготовке из свободного текста
    price_hits = find_prices(text, limit=3)
    if price_hits:
        blocks = [format_price_item(x) for x in price_hits]
        return "🔎 <b>Похожие услуги / анализы:</b>\n\n" + "\n\n".join(blocks)

    prep_hits = find_preps(text, limit=2)
    if prep_hits:
        blocks = [format_prep_item(x) for x in prep_hits]
        return "🔎 <b>Похожие результаты по подготовке:</b>\n\n" + "\n\n".join(blocks)

    return None


# =========================
# HANDLERS
# =========================

async def reply_html(update: Update, text: str, reply_markup=None):
    await update.effective_message.reply_text(
        truncate(text),
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = (
        "👋 <b>МедНавигатор РГ Клиник</b>\n\n"
        "Официальный бот клиники.\n"
        "Помогаю быстро найти анализы и услуги, узнать цены и сроки готовности, "
        "подготовку к исследованиям и оформить запись к врачу.\n\n"
        "Информация справочная и не заменяет консультацию специалиста."
    )
    await reply_html(update, text, main_menu())

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_html(update, "Выберите действие:", main_menu())

async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_html(update, contacts_text(), main_menu())

async def doctors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = repo.doctors()
    if not docs:
        await reply_html(update, "Список врачей временно недоступен. Пожалуйста, уточните информацию у администратора.", main_menu())
        return
    blocks = [format_doctor(d) for d in docs]
    await reply_html(update, "👨‍⚕️ <b>Врачи клиники</b>\n\n" + "\n\n".join(blocks), main_menu())

async def prices_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "prices"
    items = repo.prices()[:12]
    if not items:
        await reply_html(update, "Прайс временно недоступен. Пожалуйста, уточните стоимость у администратора.", main_menu())
        return ConversationHandler.END
    text = "🧾 <b>Цены</b>\n\n"
    text += "\n\n".join(format_price_item(x) for x in items)
    text += "\n\nНапишите название анализа, услуги или код — я найду точную позицию."
    await reply_html(update, text, sub_menu())
    return WAIT_PRICE_QUERY

async def prep_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "prep"
    text = (
        "ℹ️ <b>Подготовка к исследованию</b>\n\n"
        "Напишите название анализа или услуги.\n"
        "Например: <i>глюкоза</i>, <i>общий анализ крови</i>, <i>узи брюшной полости</i>."
    )
    await reply_html(update, text, sub_menu())
    return WAIT_PREP_QUERY

async def process_price_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = menu_text(update.effective_message.text or "")
    if txt in (BTN_BACK, BTN_CANCEL):
        return await cancel_to_menu(update, context)

    hits = find_prices(txt, limit=10)
    if not hits:
        await reply_html(
            update,
            "По вашему запросу ничего не найдено.\n"
            "Попробуйте указать другое название, часть названия или код услуги.",
            sub_menu(),
        )
        return WAIT_PRICE_QUERY

    text = "🔎 <b>Найдено:</b>\n\n" + "\n\n".join(format_price_item(x) for x in hits)
    await reply_html(update, text, sub_menu())
    return WAIT_PRICE_QUERY

async def process_prep_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = menu_text(update.effective_message.text or "")
    if txt in (BTN_BACK, BTN_CANCEL):
        return await cancel_to_menu(update, context)

    hits = find_preps(txt, limit=5)
    if not hits:
        await reply_html(
            update,
            "Подготовка по такому запросу не найдена.\n"
            "Попробуйте другое название анализа или уточните информацию у администратора.",
            sub_menu(),
        )
        return WAIT_PREP_QUERY

    text = "ℹ️ <b>Подготовка</b>\n\n" + "\n\n".join(format_prep_item(x) for x in hits)
    await reply_html(update, text, sub_menu())
    return WAIT_PREP_QUERY

# ----- BOOKING -----

async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = repo.doctors()
    if not docs:
        await reply_html(update, "Список врачей временно недоступен. Пожалуйста, уточните запись у администратора.", main_menu())
        return ConversationHandler.END

    buttons = []
    for d in docs:
        label = f"{d.fio} — {d.specialty}" if d.specialty else d.fio
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"book_doctor|{d.fio}")])

    await update.effective_message.reply_text(
        "📅 Выберите врача:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    await update.effective_message.reply_text("Для отмены используйте кнопку ниже.", reply_markup=sub_menu())
    return BOOK_CHOOSE_DOCTOR

async def booking_choose_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, doctor_name = query.data.split("|", 1)
    context.user_data["booking_doctor_name"] = doctor_name

    slots = available_slots_by_doctor(doctor_name)
    if not slots:
        await query.message.reply_text(
            "Свободных слотов у выбранного врача пока нет. Выберите другого врача или уточните запись у администратора.",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    grouped = group_slots_by_date(slots)
    buttons = []
    for date_str, date_slots in list(grouped.items())[:20]:
        count = len(date_slots)
        buttons.append([InlineKeyboardButton(f"{date_str} ({count})", callback_data=f"book_date|{doctor_name}|{date_str}")])

    await query.message.reply_text(
        f"Выбран врач: {doctor_name}\nВыберите дату:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return BOOK_CHOOSE_DATE

async def booking_choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, doctor_name, date_str = query.data.split("|", 2)
    context.user_data["booking_date"] = date_str

    slots = [x for x in available_slots_by_doctor(doctor_name) if s(x.date) == s(date_str)]
    if not slots:
        await query.message.reply_text("На выбранную дату свободных слотов уже нет. Пожалуйста, выберите другую дату.", reply_markup=main_menu())
        return ConversationHandler.END

    buttons = []
    for sl in slots[:30]:
        label = sl.time or "без времени"
        buttons.append([InlineKeyboardButton(label, callback_data=f"book_slot|{sl.slot_id}")])

    await query.message.reply_text(
        f"Дата: {date_str}\nВыберите время:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return BOOK_CHOOSE_TIME

async def booking_choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, slot_id = query.data.split("|", 1)

    slot = choose_slot_by_id(slot_id)
    if not slot or slot.status != "FREE":
        await query.message.reply_text("Этот слот уже недоступен. Пожалуйста, начните запись заново.", reply_markup=main_menu())
        return ConversationHandler.END

    context.user_data["booking_slot_id"] = slot.slot_id
    context.user_data["booking_slot_rownum"] = slot.rownum
    context.user_data["booking_time"] = slot.time

    await query.message.reply_text(
        f"Вы выбрали:\nВрач: {slot.doctor_name}\nДата: {slot.date}\nВремя: {slot.time}\n\nВведите ФИО пациента:",
        reply_markup=sub_menu(),
    )
    return BOOK_ENTER_NAME

async def booking_enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = menu_text(update.effective_message.text or "")
    if txt in (BTN_CANCEL, BTN_BACK):
        return await cancel_to_menu(update, context)

    if len(txt) < 5:
        await reply_html(update, "Пожалуйста, введите ФИО полностью.", sub_menu())
        return BOOK_ENTER_NAME

    context.user_data["patient_name"] = s(update.effective_message.text)
    await reply_html(update, "Введите номер телефона пациента:", sub_menu())
    return BOOK_ENTER_PHONE

async def booking_enter_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = menu_text(update.effective_message.text or "")
    if txt in (BTN_CANCEL, BTN_BACK):
        return await cancel_to_menu(update, context)

    raw_phone = s(update.effective_message.text)
    if not valid_phone(raw_phone):
        await reply_html(update, "Пожалуйста, введите корректный номер телефона.", sub_menu())
        return BOOK_ENTER_PHONE

    slot_id = context.user_data.get("booking_slot_id")
    patient_name = context.user_data.get("patient_name", "")
    patient_phone = raw_phone

    slot = choose_slot_by_id(slot_id)
    if not slot or slot.status != "FREE":
        await reply_html(update, "К сожалению, выбранный слот уже занят. Попробуйте оформить запись заново.", main_menu())
        return ConversationHandler.END

    try:
        repo.book_slot(slot, patient_name, patient_phone)
        repo.append_booking(patient_name, patient_phone, slot.doctor_name, slot.date, slot.time)
    except Exception as e:
        logger.exception("Booking failed: %s", e)
        await reply_html(
            update,
            "Не удалось завершить запись из-за ошибки таблицы. Пожалуйста, попробуйте ещё раз или обратитесь к администратору.",
            main_menu(),
        )
        return ConversationHandler.END

    confirm = (
        "✅ <b>Запись оформлена</b>\n\n"
        f"Пациент: {patient_name}\n"
        f"Телефон: {patient_phone}\n"
        f"Врач: {slot.doctor_name}\n"
        f"Дата: {slot.date}\n"
        f"Время: {slot.time}\n\n"
        "Информация справочная. При необходимости администратор свяжется с вами."
    )
    await reply_html(update, confirm, main_menu())

    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=(
                    "📥 Новая запись\n\n"
                    f"Пациент: {patient_name}\n"
                    f"Телефон: {patient_phone}\n"
                    f"Врач: {slot.doctor_name}\n"
                    f"Дата: {slot.date}\n"
                    f"Время: {slot.time}"
                ),
            )
        except Exception:
            logger.exception("Failed to notify admin")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply_html(update, "Действие отменено. Выберите действие:", main_menu())
    return ConversationHandler.END

# ----- GENERAL TEXT -----

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = menu_text(update.effective_message.text or "")

    if txt == BTN_BOOKING:
        return await booking_start(update, context)
    if txt == BTN_DOCTORS:
        return await doctors(update, context)
    if txt == BTN_PRICES:
        return await prices_menu(update, context)
    if txt == BTN_PREP:
        return await prep_menu(update, context)
    if txt == BTN_CONTACTS:
        return await contacts(update, context)
    if txt in (BTN_BACK, BTN_CANCEL):
        return await cancel_to_menu(update, context)

    answer = smart_answer(txt)
    if answer:
        await reply_html(update, answer, main_menu())
        return

    await reply_html(
        update,
        "Я могу помочь со справочной информацией по РГ Клиник:\n"
        "• запись к врачу\n"
        "• врачи и специальности\n"
        "• цены\n"
        "• подготовка к исследованиям\n"
        "• контакты\n\n"
        "Выберите кнопку меню или напишите название анализа / услуги.",
        main_menu(),
    )

# ----- ERROR HANDLER -----

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Произошла временная ошибка. Пожалуйста, повторите запрос чуть позже.",
                reply_markup=main_menu(),
            )
    except Exception:
        logger.exception("Failed to send error message to user")

# =========================
# APP
# =========================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BOOKING)}$"), booking_start),
        ],
        states={
            BOOK_CHOOSE_DOCTOR: [
                CallbackQueryHandler(booking_choose_doctor, pattern=r"^book_doctor\|"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
            ],
            BOOK_CHOOSE_DATE: [
                CallbackQueryHandler(booking_choose_date, pattern=r"^book_date\|"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
            ],
            BOOK_CHOOSE_TIME: [
                CallbackQueryHandler(booking_choose_time, pattern=r"^book_slot\|"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
            ],
            BOOK_ENTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, booking_enter_name),
            ],
            BOOK_ENTER_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, booking_enter_phone),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_to_menu),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BACK)}$"), cancel_to_menu),
        ],
        allow_reentry=True,
    )

    prices_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PRICES)}$"), prices_menu),
        ],
        states={
            WAIT_PRICE_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_price_query)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_to_menu),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BACK)}$"), cancel_to_menu),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )

    prep_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PREP)}$"), prep_menu),
        ],
        states={
            WAIT_PREP_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_prep_query)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_to_menu),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BACK)}$"), cancel_to_menu),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(booking_conv)
    app.add_handler(prices_conv)
    app.add_handler(prep_conv)
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DOCTORS)}$"), doctors))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CONTACTS)}$"), contacts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(on_error)

    return app

def main():
    logger.info("Starting RG Clinic bot v15b")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
