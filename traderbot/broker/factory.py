"""Фабрика TBankBroker из записи клиента.

Единственная функция: создать TBankBroker по токену клиента, получить
account_id и проверить подключение. Вся работа идёт только с T-Bank —
никакой абстракции поверх.

Используется в main.py при инициализации и в sync_execs при
подтягивании новых клиентов.
"""
from __future__ import annotations

import logging

from traderbot.broker.tbank import TBankBroker
from traderbot.clients.models import Client

logger = logging.getLogger(__name__)


def broker_from_client(client: Client, app_name: str = "TraderBot") -> tuple[TBankBroker, str]:
    """Создать TBankBroker из данных клиента и вернуть (broker, account_id).

    Бросает исключение при ошибке подключения — вызывающий код должен
    поймать и пометить клиента через registry.increment_errors().
    """
    if not client.tbank_token:
        raise ValueError(f"Client {client.id} has no tbank_token")
    if not client.tbank_account_id:
        raise ValueError(f"Client {client.id} has no tbank_account_id")

    broker = TBankBroker(
        token=client.tbank_token,
        sandbox=False,
        app_name=app_name,
    )

    # Лёгкая проверка подключения: получить баланс счёта.
    # Если токен невалиден или счёт недоступен — бросит исключение здесь,
    # до того как клиент попадёт в торговый цикл.
    balance = broker.get_portfolio_balance(client.tbank_account_id)
    logger.info(
        "[FACTORY] client=%d account=%s balance=%.2f RUB",
        client.id, client.tbank_account_id, balance,
    )

    return broker, client.tbank_account_id
