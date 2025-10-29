# bot_voice.py — добавление голосового помощника в существующий МедНавигатор РГ Клиник
# --------------------------------------------------------------
# Расширение к основному боту: поддержка голосовых сообщений через Yandex SpeechKit (SpeechSense)
# Авторизация: API Key + Folder ID из Render Environment

import os
import logging
import tempfile
import requests
from telegram import Update
from telegram.ext import MessageHandler, filters, ContextTypes

# --- ENV ---
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

URL_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
URL_TTS = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

HEADERS = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def route_intent(text: str) -> str:
    """Простая маршрутизация запросов (справочная логика)."""
    t = (text or "").lower()
    if "цена" in t or "стоит" in t:
        return "Цены на анализы можно уточнить по коду или названию. Какой анализ вас интересует?"
    if "запис" in t:
        return "Готов оформить запись. Укажите направление и удобное время."
    if "график" in t or "режим" in t or "часы" in t:
        return "Мы работаем ежедневно. Уточните клинику и дату, чтобы сказать точнее."
    return "Я помогу с услугами, ценами и записью. Сформулируйте запрос одним предложением, пожалуйста."


async def stt_yandex_ogg(ogg_bytes: bytes) -> str:
    params = {
        "folderId": YANDEX_FOLDER_ID,
        "lang": "ru-RU",
        "format": "oggopus",
        "profanityFilter": "false",
    }
    r = requests.post(URL_STT, headers=HEADERS, params=params, data=ogg_bytes, timeout=60)
    r.raise_for_status()
    for line in r.text.splitlines():
        if line.startswith("result="):
            return line.split("=", 1)[1].strip()
    return ""


async def tts_yandex_ogg(text: str) -> bytes:
    data = {
        "text": text,
        "lang": "ru-RU",
        "voice": "ermil",
        "emotion": "neutral",
        "speed": "1.0",
        "format": "oggopus",
        "folderId": YANDEX_FOLDER_ID,
    }
    with requests.post(URL_TTS, headers=HEADERS, data=data, stream=True, timeout=60) as r:
        r.raise_for_status()
        return b"".join(r.iter_content(4096))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
        logging.info("Получено голосовое: %s байт", len(ogg_bytes))

        user_text = await stt_yandex_ogg(ogg_bytes)
        if not user_text:
            await update.message.reply_text("Не удалось распознать речь. Повторите, пожалуйста 🙏")
            return

        reply_text = route_intent(user_text)
        tts_bytes = await tts_yandex_ogg(reply_text)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(tts_bytes)
            tmp.flush()
            await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=f"Вы сказали: «{user_text}»\n\n{reply_text}")

    except Exception as e:
        logging.exception("Ошибка при обработке голосового: %s", e)
        await update.message.reply_text("Произошла ошибка при обработке аудио.")


def register_voice_handler(app):
    """Регистрация голосового обработчика в основном приложении Telegram."""
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))


# Интеграция:
# В основном bot.py (МедНавигатор) добавить:
# from bot_voice import register_voice_handler
# ...
# app = build_app()
# register_voice_handler(app)
# app.run_polling()

# После этого бот начнет принимать голосовые сообщения, распознавать речь через Yandex SpeechKit
# и отвечать голосом.
