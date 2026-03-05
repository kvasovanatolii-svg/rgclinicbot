import os

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")

ADMIN_ID = os.getenv("ADMIN_ID")

SHEET_DOCTORS = "Врачи"
SHEET_PRICES = "Цены"
SHEET_PREP = "Подготовка"
SHEET_SCHEDULE = "Расписание"
SHEET_REQUESTS = "Записи"
SHEET_INFO = "Инфо"
