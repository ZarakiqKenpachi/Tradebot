"""Модели данных для реестра клиентов."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ClientRole(str, Enum):
    """Роль клиента в системе.

    - ADMIN: self-run аккаунт владельца бота (токен в config.admin.tokens).
      Бесплатный, всегда активный, не требует онбординга.
    - SUBSCRIBER: платный клиент, создаётся через Telegram-онбординг.
    """

    ADMIN = "admin"
    SUBSCRIBER = "subscriber"


class ClientStatus(str, Enum):
    """Статус клиента (life-cycle state machine).

    Переходы:
        pending_payment → pending_email   (после /grant или успешной оплаты)
        pending_email   → pending_token   (после ввода email в /setup)
        pending_token   → active          (после валидации T-Bank токена)
        pending_token   → pending_token   (при ошибке валидации — повторная попытка)
        active          → paused          (по /pause или /pause_client)
        paused          → active          (по /resume или /resume_client)
        active          → expired         (по истечении paid_until)
        *               → revoked         (по /revoke, терминальное состояние)
    """

    PENDING_PAYMENT = "pending_payment"
    PENDING_EMAIL = "pending_email"
    PENDING_TOKEN = "pending_token"
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class Client:
    """Запись клиента в реестре.

    Соответствует строке таблицы `clients` в SQLite. Токен хранится в
    открытом виде (решение владельца проекта — см. плану фазы).
    """

    id: int
    tg_chat_id: int
    role: ClientRole
    status: ClientStatus
    email: str | None
    account_name: str | None
    tbank_token: str | None
    tbank_account_id: str | None
    paid_until: datetime | None
    consecutive_errors: int
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        """repr без утечки токена (на случай, если объект попадёт в лог)."""
        token_mask = "None"
        if self.tbank_token:
            tail = self.tbank_token[-4:] if len(self.tbank_token) >= 4 else "***"
            token_mask = f"***{tail}"
        return (
            f"Client(id={self.id}, tg_chat_id={self.tg_chat_id}, role={self.role.value}, "
            f"status={self.status.value}, email={self.email!r}, "
            f"tbank_token={token_mask}, tbank_account_id={self.tbank_account_id!r}, "
            f"paid_until={self.paid_until}, consecutive_errors={self.consecutive_errors})"
        )
