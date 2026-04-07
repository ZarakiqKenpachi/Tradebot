"""Рассылка уведомлений через Telegram (multi-tenant).

Два канала:
- send_to_client(client_id, msg) — конкретному клиенту по client_id
  (ищет tg_chat_id в ClientRegistry).
- send_admin(msg) — всем admin chat_ids из конфига.
- send_to_all_active(msg) — broadcast всем active-клиентам.

При 403 от Telegram (пользователь заблокировал бота) — только логируем,
запись в БД не трогаем (удаление через отдельную admin-команду).
"""
import logging

import telebot

from traderbot.clients.registry import ClientRegistry

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        bot: telebot.TeleBot,
        registry: ClientRegistry,
        admin_chat_ids: list[str],
    ):
        self.bot = bot
        self.registry = registry
        self.admin_chat_ids = admin_chat_ids

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def send_to_client(self, client_id: int, message: str) -> None:
        """Отправить сообщение клиенту по его client_id."""
        client = self.registry.get_by_id(client_id)
        if client is None:
            logger.warning("[TELEGRAM] send_to_client: client %d not found", client_id)
            return
        self._send_safe(str(client.tg_chat_id), message)

    def send_admin(self, message: str) -> None:
        """Отправить сообщение всем администраторам."""
        if not self.admin_chat_ids:
            logger.debug("[TELEGRAM] send_admin: no admin_chat_ids configured")
            return
        for chat_id in self.admin_chat_ids:
            self._send_safe(chat_id, message)

    def send_to_all_active(self, message: str) -> None:
        """Broadcast всем active-клиентам."""
        from traderbot.clients.models import ClientStatus
        clients = self.registry.list_by_status(ClientStatus.ACTIVE)
        for client in clients:
            self._send_safe(str(client.tg_chat_id), message)

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _send_safe(self, chat_id: str, message: str) -> None:
        try:
            self.bot.send_message(chat_id, message)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 403:
                logger.info("[TELEGRAM] chat_id=%s blocked the bot", chat_id)
            else:
                logger.error("[TELEGRAM] API error for chat_id=%s: %s", chat_id, e)
        except Exception:
            logger.exception("[TELEGRAM] Failed to send to chat_id=%s", chat_id)
