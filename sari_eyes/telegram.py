"""Direct Telegram alerts (fallback path when the brain is unreachable)."""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text: str, token: Optional[str] = None, chat_id: Optional[str] = None, timeout: float = 5.0) -> bool:
    """Send a plain-text message. Returns False (never raises) when credentials are missing or delivery fails."""
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    target_chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not target_chat:
        logger.error("Telegram alert not sent: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
        return False

    try:
        response = requests.post(
            API_URL.format(token=bot_token),
            json={"chat_id": target_chat, "text": text},
            timeout=timeout,
        )
    except Exception as exc:  # network errors embed the URL, which contains the token
        logger.warning("Telegram alert not sent: %s", type(exc).__name__)
        return False

    if response.status_code == 200:
        logger.info("Telegram alert sent")
        return True
    logger.warning("Telegram alert rejected with HTTP %s", response.status_code)
    return False
