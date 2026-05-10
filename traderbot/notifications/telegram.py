"""Рассылка уведомлений через Telegram (multi-tenant).

Два канала:
- send_to_client(client_id, msg) — конкретному клиенту по client_id
  (ищет tg_chat_id в ClientRegistry).
- send_admin(msg) — всем admin chat_ids из конфига.
- send_to_all_active(msg) — broadcast всем active-клиентам.

При 403 от Telegram (пользователь заблокировал бота) — только логируем,
запись в БД не трогаем (удаление через отдельную admin-команду).

Если Telegram недоступен — сообщения сохраняются в очередь и отправляются
при восстановлении связи с пометкой «отправлено позже».
"""
import logging
import threading
import time
from collections import deque
from datetime import datetime

import telebot

from traderbot.clients.registry import ClientRegistry

logger = logging.getLogger(__name__)

# Ошибки, сигнализирующие о недоступности сети (retry имеет смысл)
_NETWORK_ERRORS = (
    ConnectionError,
    TimeoutError,
    OSError,
)


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

        self._queue: deque[tuple[str, str, datetime]] = deque()
        self._lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None

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
        except _NETWORK_ERRORS:
            logger.warning("[TELEGRAM] Сеть недоступна, сообщение в очередь (chat_id=%s)", chat_id)
            self._enqueue(chat_id, message)
        except Exception:
            logger.exception("[TELEGRAM] Failed to send to chat_id=%s", chat_id)

    def _enqueue(self, chat_id: str, message: str) -> None:
        ts = datetime.now()
        with self._lock:
            self._queue.append((chat_id, message, ts))
        self._ensure_flush_thread()

    def _ensure_flush_thread(self) -> None:
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="tg-queue-flush",
        )
        self._flush_thread.start()

    def _flush_loop(self) -> None:
        """Пробует отправить накопленные сообщения каждые 15 секунд."""
        while True:
            time.sleep(15)
            with self._lock:
                if not self._queue:
                    return  # очередь пуста — поток завершается
                chat_id, message, ts = self._queue[0]

            # Пробуем отправить первое сообщение как проверку связи
            delayed_msg = self._format_delayed(message, ts)
            try:
                self.bot.send_message(chat_id, delayed_msg)
            except _NETWORK_ERRORS:
                logger.debug("[TELEGRAM] Сеть всё ещё недоступна, retry через 15с")
                continue
            except telebot.apihelper.ApiTelegramException as e:
                if e.error_code == 403:
                    logger.info("[TELEGRAM] chat_id=%s blocked the bot (queued msg dropped)", chat_id)
                else:
                    logger.error("[TELEGRAM] API error for queued msg chat_id=%s: %s", chat_id, e)
            except Exception:
                logger.exception("[TELEGRAM] Failed to send queued msg to chat_id=%s", chat_id)

            # Первое сообщение обработано (отправлено или дропнуто) — убираем из очереди
            with self._lock:
                if self._queue:
                    self._queue.popleft()

    @staticmethod
    def _format_delayed(message: str, original_time: datetime) -> str:
        ts_str = original_time.strftime("%H:%M:%S")
        return f"[⏳ отправлено позже, время события {ts_str}]\n\n{message}"
