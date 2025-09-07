import requests
import logging
from typing import Tuple

TIMEOUT = (10, 20)  # (connect, read)

def tg_notify(text: str, bot_token: str, chat_id: str) -> bool:
    if not (bot_token and chat_id):
        logging.info("Mensaje simulado enviado")
        logging.info("Mensaje simulado: " + text)
        return True
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=data, timeout=TIMEOUT)
        r.raise_for_status()
        logging.info("Notificación enviada a Telegram")
        return True
    except Exception as e:
        logging.error(f"Telegram error: {e}")
        return False
