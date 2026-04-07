"""FSM-онбординг: email → T-Bank токен → валидация → active.
Также обрабатывает TOKEN_UPDATE — смену токена для активных клиентов и админов.

Регистрирует catch-all хендлер для текстовых сообщений. Когда FSM-состояние
клиента != IDLE, сообщение обрабатывается здесь.

Критически важно: сообщение с токеном удаляется немедленно после получения
(bot.delete_message). Токен никогда не попадает в логи.
"""
from __future__ import annotations

import logging
import re
import threading

import telebot

from traderbot.broker.tbank import TBankBroker
from traderbot.clients.models import ClientRole, ClientStatus
from traderbot.clients.registry import ClientRegistry
from traderbot.notifications.fsm import FSM, OnboardingState
from traderbot.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Ссылка на официальную документацию T-Bank для получения токена
TOKEN_HOWTO = (
    "Как получить токен T-Bank Invest:\n"
    "1. Откройте приложение T-Bank (Тинькофф Инвестиции)\n"
    "2. Перейдите в Настройки → Робот-советник → Токен\n"
    "3. Создайте токен с правами на торговлю\n"
    "4. Скопируйте и отправьте его сюда\n\n"
    "⚠️ Токен будет немедленно удалён из чата после отправки.\n"
    "Убедитесь, что никто не видит ваш экран."
)

# Обратная совместимость (использовалось внутри модуля)
_TOKEN_HOWTO = TOKEN_HOWTO


def register(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    fsm: FSM,
    notifier: TelegramNotifier | None,
    execs: dict | None = None,
    reload_event: threading.Event | None = None,
) -> None:
    """Зарегистрировать catch-all хендлер для FSM-состояний онбординга."""

    @bot.message_handler(func=lambda m: _is_onboarding(m, fsm), content_types=["text"])
    def handle_onboarding_text(message):
        chat_id = message.chat.id
        state = fsm.get_onboarding(chat_id)
        text = (message.text or "").strip()

        if state == OnboardingState.PENDING_NICKNAME:
            _handle_nickname(bot, registry, fsm, chat_id, text)
        elif state == OnboardingState.NICKNAME_UPDATE:
            _handle_nickname_update(bot, registry, fsm, chat_id, text)
        elif state == OnboardingState.PENDING_EMAIL:
            _handle_email(bot, registry, fsm, chat_id, text)
        elif state == OnboardingState.PENDING_TOKEN:
            _handle_token(bot, registry, fsm, notifier, message)
        elif state == OnboardingState.TOKEN_UPDATE:
            _handle_token_update(bot, registry, fsm, notifier, message,
                                 execs=execs, reload_event=reload_event)


def _is_onboarding(message, fsm: FSM) -> bool:
    """True если chat_id находится в активном FSM-состоянии онбординга."""
    if not message.text:
        return False
    if message.text.startswith("/"):
        return False  # Команды обрабатываются своими хендлерами
    state = fsm.get_onboarding(message.chat.id)
    return state in (
        OnboardingState.PENDING_NICKNAME,
        OnboardingState.NICKNAME_UPDATE,
        OnboardingState.PENDING_EMAIL,
        OnboardingState.PENDING_TOKEN,
        OnboardingState.TOKEN_UPDATE,
    )


# ---------------------------------------------------------------------------
# Шаг 0: никнейм (при /start)
# ---------------------------------------------------------------------------

def _handle_nickname(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    fsm: FSM,
    chat_id: int,
    text: str,
) -> None:
    client = registry.get_by_chat_id(chat_id)
    if client is None:
        fsm.clear_onboarding(chat_id)
        return

    if len(text) < 2 or len(text) > 32:
        bot.send_message(
            chat_id,
            "❌ Никнейм должен содержать от 2 до 32 символов. Введите снова:",
        )
        return

    registry.set_account_name(client.id, text)
    fsm.clear_onboarding(chat_id)
    logger.info("[ONBOARDING] client %d nickname saved: %s", client.id, text)

    bot.send_message(
        chat_id,
        f"✅ Никнейм сохранён: {text}\n\n"
        "Бот торгует на Московской бирже по алготорговой стратегии.\n\n"
        "Для доступа к торговле:\n"
        "  /pay — оформить подписку\n"
        "  /help — все команды",
    )


# ---------------------------------------------------------------------------
# Смена никнейма через /nickname
# ---------------------------------------------------------------------------

def _handle_nickname_update(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    fsm: FSM,
    chat_id: int,
    text: str,
) -> None:
    client = registry.get_by_chat_id(chat_id)
    if client is None:
        fsm.clear_onboarding(chat_id)
        return

    if len(text) < 2 or len(text) > 32:
        bot.send_message(
            chat_id,
            "❌ Никнейм должен содержать от 2 до 32 символов. Введите снова:",
        )
        return

    old_name = client.account_name or "(не задан)"
    registry.set_account_name(client.id, text)
    fsm.clear_onboarding(chat_id)
    logger.info("[ONBOARDING] client %d nickname updated: %s → %s", client.id, old_name, text)

    bot.send_message(chat_id, f"✅ Никнейм обновлён: {text}")


# ---------------------------------------------------------------------------
# Шаг 1: email
# ---------------------------------------------------------------------------

def _handle_email(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    fsm: FSM,
    chat_id: int,
    text: str,
) -> None:
    client = registry.get_by_chat_id(chat_id)
    if client is None:
        fsm.clear_onboarding(chat_id)
        return

    if not _EMAIL_RE.match(text):
        bot.send_message(
            chat_id,
            "❌ Некорректный email. Введите адрес в формате name@domain.com:",
        )
        return

    registry.set_email(client.id, text)
    registry.update_status(client.id, ClientStatus.PENDING_TOKEN)
    fsm.set_onboarding(chat_id, OnboardingState.PENDING_TOKEN)
    logger.info("[ONBOARDING] client %d email saved", client.id)

    bot.send_message(
        chat_id,
        f"✅ Email сохранён: {text}\n\n"
        f"Шаг 2 из 2: введите ваш T-Bank Invest токен.\n\n{_TOKEN_HOWTO}",
    )


# ---------------------------------------------------------------------------
# Шаг 2: токен
# ---------------------------------------------------------------------------

def _handle_token(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    fsm: FSM,
    notifier: TelegramNotifier | None,
    message,
) -> None:
    chat_id = message.chat.id
    token = (message.text or "").strip()

    client = registry.get_by_chat_id(chat_id)
    if client is None:
        fsm.clear_onboarding(chat_id)
        return

    # 1. Удалить сообщение с токеном немедленно
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        logger.debug("[ONBOARDING] Could not delete token message for chat_id=%d", chat_id)

    bot.send_message(chat_id, "🔐 Токен получен. Исходное сообщение удалено.\nПроверяю подключение...")

    # 2. Валидация токена через API
    fsm.set_onboarding(chat_id, OnboardingState.VALIDATING)
    try:
        broker = TBankBroker(token=token, sandbox=False, app_name="TraderBot-validate")
        account_id = broker.get_account_id()
        balance = broker.get_portfolio_balance(account_id)
    except Exception as e:
        logger.warning("[ONBOARDING] client %d token validation failed: %s", client.id, type(e).__name__)
        fsm.set_onboarding(chat_id, OnboardingState.PENDING_TOKEN)
        bot.send_message(
            chat_id,
            "❌ Не удалось подключиться к T-Bank с этим токеном.\n\n"
            f"Ошибка: {_safe_error(e)}\n\n"
            "Проверьте токен и попробуйте снова:",
        )
        return

    # 3. Сохранить токен и account_id
    registry.set_token_and_account(client.id, token, account_id)
    registry.update_status(client.id, ClientStatus.ACTIVE)
    registry.reset_errors(client.id)
    fsm.clear_onboarding(chat_id)

    tail = token[-4:] if len(token) >= 4 else "***"
    logger.info("[ONBOARDING] client %d activated: account=%s balance=%.2f",
                client.id, account_id, balance)

    bot.send_message(
        chat_id,
        f"✅ Подключение успешно!\n\n"
        f"Счёт: {account_id}\n"
        f"Баланс: {balance:,.2f} ₽\n\n"
        "Бот начнёт торговать на вашем счёте в течение минуты.\n"
        "Используйте /status для отслеживания позиций.",
    )

    # 4. Уведомить администратора
    if notifier:
        notifier.send_admin(
            f"✅ Новый клиент активирован!\n"
            f"ID: {client.id} | chat_id: {chat_id}\n"
            f"Email: {client.email}\n"
            f"Счёт: {account_id} | Баланс: {balance:,.2f} ₽\n"
            f"Токен: ***{tail}"
        )


def _handle_token_update(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    fsm: FSM,
    notifier,
    message,
    execs: dict | None,
    reload_event,
) -> None:
    """Смена токена для уже активных клиентов и администраторов (/mytoken).

    Подписка и статус не меняются. Если у клиента есть открытые позиции,
    они будут сопровождаться старым брокером до закрытия — новый токен
    вступит в силу в следующем цикле sync_execs.
    """
    chat_id = message.chat.id
    token = (message.text or "").strip()

    client = registry.get_by_chat_id(chat_id)
    if client is None:
        fsm.clear_onboarding(chat_id)
        return

    # 1. Удалить сообщение с токеном немедленно
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        logger.debug("[TOKEN_UPDATE] Could not delete token message for chat_id=%d", chat_id)

    bot.send_message(chat_id, "🔐 Токен получен. Исходное сообщение удалено.\nПроверяю подключение...")

    # 2. Валидация токена через API
    fsm.set_onboarding(chat_id, OnboardingState.VALIDATING)
    try:
        broker = TBankBroker(token=token, sandbox=False, app_name="TraderBot-validate")
        account_id = broker.get_account_id()
        balance = broker.get_portfolio_balance(account_id)
    except Exception as e:
        logger.warning("[TOKEN_UPDATE] client %d token validation failed: %s",
                       client.id, type(e).__name__)
        fsm.set_onboarding(chat_id, OnboardingState.TOKEN_UPDATE)
        bot.send_message(
            chat_id,
            "❌ Не удалось подключиться к T-Bank с этим токеном.\n\n"
            f"Ошибка: {_safe_error(e)}\n\n"
            "Проверьте токен и попробуйте снова:",
        )
        return

    # 3. Сохранить токен и account_id
    registry.set_token_and_account(client.id, token, account_id)
    registry.reset_errors(client.id)

    # Если это TG-only admin без статуса active — активируем
    if client.role == ClientRole.ADMIN and not client.tbank_token:
        registry.update_status(client.id, ClientStatus.ACTIVE)

    fsm.clear_onboarding(chat_id)

    tail = token[-4:] if len(token) >= 4 else "***"
    logger.info("[TOKEN_UPDATE] client %d token updated: account=%s balance=%.2f",
                client.id, account_id, balance)

    # 4. Обновить execs: если нет открытых позиций — убрать и пересоздать с новым токеном
    has_positions = False
    if execs is not None:
        em = execs.get(client.id)
        if em is not None:
            if em.positions:
                has_positions = True
                # Позиции есть — новый токен вступит в силу после их закрытия
            else:
                del execs[client.id]

    if reload_event is not None:
        reload_event.set()

    # 5. Сообщение пользователю
    if has_positions:
        position_note = (
            "\n\n⚠️ У вас есть открытые позиции. Они будут сопровождаться "
            "старым подключением до закрытия. Новый токен вступит в силу "
            "автоматически после их закрытия."
        )
    else:
        position_note = "\n\nИзменения вступят в силу в течение минуты."

    bot.send_message(
        chat_id,
        f"✅ Токен успешно обновлён!\n\n"
        f"Счёт: {account_id}\n"
        f"Баланс: {balance:,.2f} ₽"
        f"{position_note}",
    )

    # 6. Уведомить администраторов
    if notifier:
        role_label = "Администратор" if client.role == ClientRole.ADMIN else "Клиент"
        name = client.account_name or client.email or str(client.id)
        notifier.send_admin(
            f"🔄 Токен обновлён\n"
            f"{role_label} #{client.id} ({name})\n"
            f"chat_id: {chat_id}\n"
            f"Счёт: {account_id} | Баланс: {balance:,.2f} ₽\n"
            f"Токен: ***{tail}"
        )


def _safe_error(e: Exception) -> str:
    """Вернуть читаемое описание ошибки без стектрейса и токена."""
    msg = str(e)
    # Обрезать длинные grpc-сообщения
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return msg
