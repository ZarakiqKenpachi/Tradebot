"""Протокол платёжного провайдера.

Любой реальный провайдер (YooKassa, CryptoBot и т.д.) должен реализовать
этот интерфейс. Сейчас используется ManualProvider — ручное подтверждение
оплаты администратором через /grant.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PaymentProvider(Protocol):
    """Интерфейс провайдера платежей."""

    def create_invoice(
        self,
        client_id: int,
        amount_rub: float,
        period_days: int,
    ) -> str:
        """Создать счёт на оплату. Возвращает текст для отправки клиенту
        (ссылку на оплату, реквизиты или инструкцию)."""
        ...

    def check_status(self, external_id: str) -> str:
        """Проверить статус платежа. Возвращает 'pending' | 'paid' | 'failed'."""
        ...
