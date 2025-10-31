# -*- coding: utf-8 -*-
"""
bot.py — МедНавигатор РГ Клиник
Мини-версия: текст + кнопки + голос (через Yandex SpeechKit)
Приспособлена под Render и python-telegram-bot 20.x
"""

import os
import json
import logging
import tempfile

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------
# ЛОГИ
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rgclinicbot")


# ------------------------------------------------------------
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ------------------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

URL_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
URL_TTS = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

if not BOT_TOKEN:
    raise SystemExit("❗ TELEGRAM_BOT_TOKEN не задан в переменных окружения")


# ------------------------------------------------------------
# UI
# ------------------------------------------------------------
WELCOME = (
    "👋 Здравствуйте! Я — МедНавигатор РГ Клиник.\n"
    "Справочная информация по анализам, подготовке и записи.\n"
    "Отправьте текст или голос — я разберу 😊"
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Запись на приём", callback_data="RECORD")],
            [InlineKeyboardButton("🧾 Цены и анализы", callback_data="PRICES")],
            [InlineKeyboardButton("ℹ️ Подготовка", callback_data="PREP")],
            [InlineKeyboardButton("📍 Контакты", callback_data="CONTACTS")],
        ]
    )


# ------------------------------------------------------------
# SpeechKit: STT
# ------------------------------------------------------------
async def stt_yandex_ogg(ogg_bytes: bytes) -> str:
    """Отправляем голос в Yandex STT и возвращаем распознанный текст (или пустую строку)."""
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        log.warning("SpeechKit: не заданы YANDEX_API_KEY или YANDEX_FOLDER_ID")
        return ""

    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    params = {
        "folderId": YANDEX_FOLDER_ID,
        "lang": "ru-RU",
        "format": "oggopus",
    }

    try:
        r = requests.post(
            URL_STT,
            headers=headers,
            params=params,
            data=ogg_bytes,
            timeout=60,
        )
        log.info("STT status=%s, text=%r", r.status_code, r.text)
        r.raise_for_status()

        # ответ приходит построчно вида:
        # result=...
        # session_id=...
        for line in r.text.splitlines():
            if line.startswith("result="):
                return line.split("=", 1)[1].strip()
        return ""
    except Exception as e:
        log.exception("STT error: %s", e)
        return ""


# ------------------------------------------------------------
# SpeechKit: TTS
# ------------------------------------------------------------
async def tts_yandex_ogg(text: str) -> bytes:
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        return b""

    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    data = {
        "text": text,
        "lang": "ru-RU",
        "voice": "ermil",
        "emotion": "neutral",
        "speed": "1.0",
        "format": "oggopus",
        "folderId": YANDEX_FOLDER_ID,
    }

    try:
        with requests.post(
            URL_TTS,
            headers=headers,
            data=data,
            stream=True,
            timeout=60,
        ) as r:
            log.info("TTS status=%s", r.status_code)
            r.raise_for_status()
            return b"".join(r.iter_content(4096))
    except Exception as e:
        log.exception("TTS error: %s", e)
        return b""


# ------------------------------------------------------------
# РОУТЕР ДЛЯ ТЕКСТА
# ------------------------------------------------------------
def route_intent(text: str) -> str:
    t = (text or "").lower()

    if "цена" in t or "стоит" in t or "анализ" in t or "прайс" in t:
        return "🧾 Напишите название или код анализа — подскажу ориентировочную стоимость (справочно)."
    if "запис" in t:
        return "📅 Укажите врача/направление и удобное время — передам администратору."
    if "график" in t or "режим" in t or "часы" in t:
        return "🕒 Режим работы (справочно): пн–пт 09:00–20:00, сб 09:00–17:00, вс 09:00–15:00. Уточните филиал, если их несколько."
    if "подготов" in t or "натощак" in t:
        return "ℹ️ Напишите название анализа — пришлю общие требования по подготовке (справочно)."

    return "Я помогу с услугами, ценами и записью. Сформулируйте запрос одним предложением."


# ------------------------------------------------------------
# HANDLERS
# ------------------------------------------------------------
async def log_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Логируем каждый апдейт, чтобы понимать, что вообще пришло в бота."""
    try:
        as_json = update.to_dict()
        log.info("UPDATE: %s", json.dumps(as_json, ensure_ascii=False)[:2000])
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(">>> /start от %s", update.effective_user.id)
    await update.message.reply_text(WELCOME, reply_markup=main_menu())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    log.info(">>> callback: %s", q.data)

    if q.data == "PRICES":
        await q.message.reply_text("🧾 Напишите название анализа — попробую подсказать (справочно).")
    elif q.data == "RECORD":
        await q.message.reply_text("📅 Напишите, к какому врачу/направлению хотите записаться и дату.")
    elif q.data == "PREP":
        await q.message.reply_text("ℹ️ Напишите название анализа — пришлю подготовку (справочно).")
    elif q.data == "CONTACTS":
        await q.message.reply_text("📍 РГ Клиник. Уточните филиал — дам адрес и режим.")
    else:
        await q.message.reply_text("Выберите действие из меню 👇", reply_markup=main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = (update.message.text or "").strip()
        log.info(">>> текст от %s: %s", update.effective_user.id, user_text)
        reply = route_intent(user_text)
        await update.message.reply_text(reply, reply_markup=main_menu())
    except Exception as e:
        log.exception("Ошибка в handle_text: %s", e)
        await update.message.reply_text("Тех. ошибка при обработке текста.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пришло голосовое из Telegram → скачиваем → шлём в Yandex → отвечаем."""
    try:
        voice = update.message.voice
        log.info(">>> голос от %s: duration=%s sec", update.effective_user.id, voice.duration)

        # 1. скачиваем голосовое из Telegram
        tg_file = await context.bot.get_file(voice.file_id)
        ogg_bytes = await tg_file.download_as_bytearray()
        log.info("Скачано голосовое: %s байт", len(ogg_bytes))

        # 2. отправляем в STT
        text_from_voice = await stt_yandex_ogg(bytes(ogg_bytes))
        if not text_from_voice:
            await update.message.reply_text("Не удалось распознать речь. Проверьте, что это русский язык, и повторите 🙏")
            return

        log.info("Распознано из голоса: %s", text_from_voice)

        # 3. получаем текст-ответ
        reply_text = route_intent(text_from_voice)

        # 4. пробуем озвучить
        tts_bytes = await tts_yandex_ogg(reply_text)
        if not tts_bytes:
            # если озвучка не сработала — отвечаем текстом
            msg = "Вы сказали: «{}»\n\n{}".format(text_from_voice, reply_text)
            await update.message.reply_text(msg)
            return

        # 5. если озвучка есть — отправляем voice
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(tts_bytes)
            tmp.flush()
            caption_out = "Вы сказали: «{}»\n\n{}".format(text_from_voice, reply_text)
            await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=caption_out)

    except Exception as e:
        log.exception("Ошибка голосового обработчика: %s", e)
        await update.message.reply_text("Тех. ошибка при обработке голосового.")


# ------------------------------------------------------------
# STARTUP: снимаем webhook
# ------------------------------------------------------------
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook снят, очередь очищена")
    except Exception as e:
        log.warning("Не удалось снять webhook: %s", e)


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # 0. лог всех апдейтов
    app.add_handler(MessageHandler(filters.ALL, log_all), group=0)

    # 1. команды
    app.add_handler(CommandHandler("start", start))

    # 2. кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 3. голос
    app.add_handler(MessageHandler(filters.VOICE, handle_voice), group=5)

    # 4. текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text), group=10)

    log.info("🤖 Бот запускается (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
