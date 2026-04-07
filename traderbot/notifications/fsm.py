"""In-memory FSM для онбординга и admin-диалогов.

Хранит текущее состояние диалога для каждого chat_id. Персистентность не
нужна — при рестарте бота клиент просто начнёт шаг заново (состояние в БД
через clients.status является источником истины для восстановления).
"""
from __future__ import annotations

import threading
from enum import Enum


class OnboardingState(str, Enum):
    """Шаги онбординга клиента."""
    IDLE = "idle"
    PENDING_NICKNAME = "pending_nickname"
    PENDING_EMAIL = "pending_email"
    PENDING_TOKEN = "pending_token"
    VALIDATING = "validating"
    TOKEN_UPDATE = "token_update"      # смена токена для уже активных/админов
    NICKNAME_UPDATE = "nickname_update"  # смена никнейма через /nickname


class RevokeDialogState:
    """Состояние admin-диалога /revoke для конкретного клиента."""

    def __init__(self, target_client_id: int, positions_summary: str):
        self.target_client_id = target_client_id
        self.positions_summary = positions_summary


class FSM:
    """Потокобезопасный in-memory FSM.

    Использование:
        fsm.set_onboarding(chat_id, OnboardingState.PENDING_EMAIL)
        state = fsm.get_onboarding(chat_id)   # OnboardingState или IDLE
        fsm.clear_onboarding(chat_id)

        fsm.set_revoke_dialog(admin_chat_id, RevokeDialogState(...))
        dialog = fsm.get_revoke_dialog(admin_chat_id)
        fsm.clear_revoke_dialog(admin_chat_id)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._onboarding: dict[int, OnboardingState] = {}
        self._revoke: dict[int, RevokeDialogState] = {}

    # Onboarding
    def set_onboarding(self, chat_id: int, state: OnboardingState) -> None:
        with self._lock:
            self._onboarding[chat_id] = state

    def get_onboarding(self, chat_id: int) -> OnboardingState:
        with self._lock:
            return self._onboarding.get(chat_id, OnboardingState.IDLE)

    def clear_onboarding(self, chat_id: int) -> None:
        with self._lock:
            self._onboarding.pop(chat_id, None)

    # Revoke dialog
    def set_revoke_dialog(self, admin_chat_id: int, dialog: RevokeDialogState) -> None:
        with self._lock:
            self._revoke[admin_chat_id] = dialog

    def get_revoke_dialog(self, admin_chat_id: int) -> RevokeDialogState | None:
        with self._lock:
            return self._revoke.get(admin_chat_id)

    def clear_revoke_dialog(self, admin_chat_id: int) -> None:
        with self._lock:
            self._revoke.pop(admin_chat_id, None)
