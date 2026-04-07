"""Ручной провайдер оплаты (заглушка).

Клиент получает инструкцию с реквизитами, переводит деньги, пишет
администратору. Администратор подтверждает через /grant <chat_id> <days>.

Это рабочий end-to-end flow: платёжной интеграции нет, но вся цепочка
(оплата → онбординг → торговля) работает. Позже ManualProvider заменяется
на YooKassaProvider или другой, код вокруг не меняется.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from traderbot.clients.db import Database
from traderbot.clients.registry import ClientRegistry

logger = logging.getLogger(__name__)


class ManualProvider:
    """Ручное подтверждение оплаты администратором."""

    def __init__(
        self,
        registry: ClientRegistry,
        db: Database,
        price_rub: float,
        period_days: int,
        admin_contact: str = "",    # например "@yourusername" или номер телефона
    ):
        self.registry = registry
        self.db = db
        self.price_rub = price_rub
        self.period_days = period_days
        self.admin_contact = admin_contact

    def create_invoice(self, client_id: int, amount_rub: float, period_days: int) -> str:
        """Сохранить запись об ожидаемом платеже и вернуть инструкцию для клиента."""
        self._save_payment(client_id, amount_rub, period_days)

        contact_line = f"\n\nПосле перевода напишите: {self.admin_contact}" if self.admin_contact else ""
        price_str = f"{amount_rub:.0f}" if amount_rub > 0 else "уточните у администратора"

        return (
            f"Для оформления подписки на {period_days} дней:\n\n"
            f"Сумма: {price_str} ₽\n"
            f"Реквизиты: уточните у администратора{contact_line}\n\n"
            "После подтверждения оплаты администратором вы получите доступ к настройке.\n"
            "Следующий шаг: /setup"
        )

    def check_status(self, external_id: str) -> str:
        """ManualProvider всегда pending — активация только через /grant."""
        return "pending"

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _save_payment(self, client_id: int, amount_rub: float, period_days: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.write() as cur:
            cur.execute(
                """
                INSERT INTO payments (client_id, provider, amount_rub, status,
                                      external_id, period_days, created_at, paid_at)
                VALUES (?, 'manual', ?, 'pending', NULL, ?, ?, NULL)
                """,
                (client_id, amount_rub, period_days, now),
            )
        logger.info("[PAYMENTS] manual invoice created: client=%d amount=%.0f days=%d",
                    client_id, amount_rub, period_days)
