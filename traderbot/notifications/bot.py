"""Telegram-бот: инициализация, polling, регистрация хендлеров.

Бот работает в отдельном daemon-потоке через long-polling. Хендлеры
клиентских команд — в handlers/client.py, онбординга — handlers/onboarding.py,
административных — handlers/admin.py (фаза 7).
"""
import logging
import threading

import telebot

from traderbot.clients.db import Database
from traderbot.clients.registry import ClientRegistry
from traderbot.notifications.fsm import FSM

logger = logging.getLogger(__name__)


class TelegramBot:
    """Инкапсулирует telebot.TeleBot, хендлеры и фоновое polling."""

    def __init__(
        self,
        token: str,
        registry: ClientRegistry,
        db: Database,
        execs: dict,                        # mutable dict[int, ExecutionManager] из main
        fsm: FSM,
        notifier=None,                      # TelegramNotifier, задаётся после создания
        payment_provider=None,              # ManualProvider или другой
        reload_event: threading.Event = None,  # сигнал для /reload_clients
    ):
        self.bot = telebot.TeleBot(token, parse_mode=None)
        self.registry = registry
        self.db = db
        self.execs = execs
        self.fsm = fsm
        self.notifier = notifier
        self.payment_provider = payment_provider
        self.reload_event = reload_event or threading.Event()
        self._thread: threading.Thread | None = None
        self._register_handlers()

    def set_notifier(self, notifier) -> None:
        """Установить notifier после инициализации (избегаем circular dep)."""
        self.notifier = notifier

    def start(self) -> None:
        """Запустить long-polling в daemon-потоке."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_polling, daemon=True, name="telegram-bot")
        self._thread.start()
        logger.info("[TG-BOT] polling thread started")

    def _run_polling(self) -> None:
        while True:
            try:
                self.bot.infinity_polling(timeout=30, long_polling_timeout=25, skip_pending=True)
            except Exception:
                logger.exception("[TG-BOT] polling crashed, restarting in 5s")
                import time
                time.sleep(5)

    def _register_handlers(self) -> None:
        """Зарегистрировать все хендлеры команд."""
        from traderbot.notifications.handlers import client as client_handlers
        from traderbot.notifications.handlers import onboarding as onboarding_handlers

        # Клиентские команды (/start, /help, /pay, /setup, /status, /pause, /resume)
        client_handlers.register(
            self.bot, self.registry, self.db, self.execs, self.fsm,
            payment_provider=self.payment_provider,
        )

        # Онбординг FSM (email → токен → валидация + смена токена /mytoken)
        # notifier передаётся по ссылке через self, чтобы работать после set_notifier
        onboarding_handlers.register(
            self.bot,
            self.registry,
            self.fsm,
            notifier=self,           # передаём self как proxy — см. send_admin ниже
            execs=self.execs,        # для обновления после смены токена
            reload_event=self.reload_event,
        )

        # Админка
        from traderbot.notifications.handlers import admin as admin_handlers
        admin_handlers.register(
            self.bot, self.registry, self.db, self.execs,
            self.fsm, self, self.reload_event,
        )

    def send_admin(self, msg: str) -> None:
        """Прокси: позволяет передавать self вместо notifier в onboarding."""
        if self.notifier:
            self.notifier.send_admin(msg)
