import logging

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Отправка уведомлений в Telegram."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, message: str) -> None:
        """
        Отправить текстовое сообщение в Telegram.
        При ошибке — логировать, но НЕ прерывать работу бота.
        """
        try:
            response = requests.post(
                self._url,
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            if not response.ok:
                logger.error("[TELEGRAM] HTTP %d: %s", response.status_code, response.text)
        except Exception:
            logger.exception("[TELEGRAM] Failed to send message")
