"""
telegram.py — Send alert messages via Telegram Bot API.

Input:  token (str), chat_id (str), message (str)
Output: HTTP POST to api.telegram.org — logs on failure, never raises

Public API:
    send(token, chat_id, message)   send a text message
"""

import logging

import requests

logger = logging.getLogger(__name__)


def send(token: str, chat_id: str, message: str) -> None:
    """Send a Telegram message. Logs a warning and returns if credentials missing.

    Args:
        token:    Telegram Bot token (from BotFather)
        chat_id:  Telegram chat / user ID to send to
        message:  Text to send (HTML parse mode supported: <b>, <i>, <code>)

    Raises:
        Nothing — all errors are logged and swallowed.
    """
    if not token or not chat_id:
        logger.warning("Telegram alert skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram alert sent (chat_id=%s)", chat_id)
    except Exception as exc:
        logger.error("Telegram alert failed: %s", exc)
