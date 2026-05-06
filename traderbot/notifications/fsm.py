"""In-memory FSM для онбординга и admin-диалогов.

Хранит текущее состояние диалога для каждого chat_id. Персистентность не
нужна — при рестарте бота клиент просто начнёт шаг заново (состояние в БД
через clients.status является источником истины для восстановления).

FSM-состояния автоматически истекают через FSM_TIMEOUT_SEC секунд бездействия.
"""
from __future__ import annotations

import threading
import time
from enum import Enum

# Таймаут бездействия: 30 минут
FSM_TIMEOUT_SEC = 1800


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
    """Потокобезопасный in-memory FSM с автоматическим таймаутом.

    Использование:
        fsm.set_onboarding(chat_id, OnboardingState.PENDING_EMAIL)
        state = fsm.get_onboarding(chat_id)   # OnboardingState или IDLE
        fsm.clear_onboarding(chat_id)

        fsm.set_revoke_dialog(admin_chat_id, RevokeDialogState(...))
        dialog = fsm.get_revoke_dialog(admin_chat_id)
        fsm.clear_revoke_dialog(admin_chat_id)
    """

    def __init__(self, timeout_sec: int = FSM_TIMEOUT_SEC):
        self._lock = threading.Lock()
        self._onboarding: dict[int, OnboardingState] = {}
        self._onboarding_ts: dict[int, float] = {}  # chat_id → timestamp
        self._revoke: dict[int, RevokeDialogState] = {}
        self._rename: dict[int, tuple[int, float]] = {}  # admin_chat_id → (client_id, timestamp)
        self._timeout = timeout_sec

    # Onboarding
    def set_onboarding(self, chat_id: int, state: OnboardingState) -> None:
        with self._lock:
            self._onboarding[chat_id] = state
            self._onboarding_ts[chat_id] = time.monotonic()

    def get_onboarding(self, chat_id: int) -> OnboardingState:
        with self._lock:
            state = self._onboarding.get(chat_id, OnboardingState.IDLE)
            if state == OnboardingState.IDLE:
                return state
            ts = self._onboarding_ts.get(chat_id, 0)
            if time.monotonic() - ts > self._timeout:
                self._onboarding.pop(chat_id, None)
                self._onboarding_ts.pop(chat_id, None)
                return OnboardingState.IDLE
            return state

    def clear_onboarding(self, chat_id: int) -> None:
        with self._lock:
            self._onboarding.pop(chat_id, None)
            self._onboarding_ts.pop(chat_id, None)

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

    # Rename dialog (admin переименовывает клиента)
    def set_rename(self, admin_chat_id: int, client_id: int) -> None:
        with self._lock:
            self._rename[admin_chat_id] = (client_id, time.monotonic())

    def get_rename(self, admin_chat_id: int) -> int | None:
        with self._lock:
            entry = self._rename.get(admin_chat_id)
            if entry is None:
                return None
            client_id, ts = entry
            if time.monotonic() - ts > self._timeout:
                self._rename.pop(admin_chat_id, None)
                return None
            return client_id

    def clear_rename(self, admin_chat_id: int) -> None:
        with self._lock:
            self._rename.pop(admin_chat_id, None)
