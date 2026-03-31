import logging
import uuid
from datetime import timedelta

import pandas as pd
from t_tech.invest import (
    CandleInterval,
    Client,
    ExchangeOrderType,
    InstrumentIdType,
    MoneyValue,
    OrderDirection,
    OrderType,
    Quotation,
    StopOrderDirection,
    StopOrderExpirationType,
    StopOrderType,
)
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from t_tech.invest.utils import now

logger = logging.getLogger(__name__)


class TBankBroker:
    """Обёртка над T-Bank Invest API. Единственный модуль, знающий про SDK."""

    def __init__(self, token: str, sandbox: bool = True, app_name: str = "TraderBot"):
        self.token = token
        self.sandbox = sandbox
        self.app_name = app_name
        self._target = INVEST_GRPC_API_SANDBOX if sandbox else INVEST_GRPC_API
        mode = "SANDBOX" if sandbox else "LIVE"
        logger.info("[BROKER] Режим: %s", mode)

    def _client(self) -> Client:
        return Client(self.token, target=self._target, app_name=self.app_name)

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------

    def get_candles(self, figi: str, interval: CandleInterval, days: int) -> pd.DataFrame:
        """Получить свечи за последние N дней (с пагинацией, для live)."""
        with self._client() as client:
            candles = list(client.get_all_candles(
                figi=figi,
                from_=now() - timedelta(days=days),
                interval=interval,
            ))
        return _candles_to_dataframe(candles)

    def get_candles_history(self, figi: str, interval: CandleInterval, days: int) -> pd.DataFrame:
        """Получить полную историю свечей с пагинацией (для бэктеста)."""
        with self._client() as client:
            candles = list(client.get_all_candles(
                figi=figi,
                from_=now() - timedelta(days=days),
                interval=interval,
            ))
        return _candles_to_dataframe(candles)

    # ------------------------------------------------------------------
    # Аккаунт
    # ------------------------------------------------------------------

    def get_account_id(self) -> str:
        """Получить ID первого доступного аккаунта."""
        with self._client() as client:
            return client.users.get_accounts().accounts[0].id

    def get_instrument_info(self, figi: str) -> tuple[int, float]:
        """Получить (lot_size, min_price_increment) для инструмента."""
        with self._client() as client:
            instrument = client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                id=figi,
            ).instrument
        lot_size = instrument.lot
        step = _quotation_to_float(instrument.min_price_increment)
        return lot_size, step

    def get_portfolio_balance(self, account_id: str) -> float:
        """Получить баланс портфеля в RUB."""
        with self._client() as client:
            portfolio = client.operations.get_portfolio(account_id=account_id)
        return _quotation_to_float(portfolio.total_amount_portfolio)

    # ------------------------------------------------------------------
    # Ордера
    # ------------------------------------------------------------------

    def place_limit_order(
        self,
        account_id: str,
        figi: str,
        qty: int,
        direction: OrderDirection,
        price: float,
    ) -> str:
        """Разместить лимитный ордер. Вернуть order_id."""
        with self._client() as client:
            response = client.orders.post_order(
                figi=figi,
                quantity=qty,
                price=_float_to_quotation(price),
                direction=direction,
                account_id=account_id,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id=str(uuid.uuid4()),
            )
        logger.info("[BROKER] Limit order placed: %s, figi=%s, qty=%d, price=%.2f",
                     response.order_id, figi, qty, price)
        return response.order_id

    def place_stop_loss(
        self,
        account_id: str,
        figi: str,
        qty: int,
        direction: StopOrderDirection,
        stop_price: float,
    ) -> str:
        """Разместить стоп-лосс (GTC). Вернуть stop_order_id."""
        with self._client() as client:
            response = client.stop_orders.post_stop_order(
                figi=figi,
                quantity=qty,
                stop_price=_float_to_quotation(stop_price),
                direction=direction,
                account_id=account_id,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4()),
            )
        logger.info("[BROKER] Stop-loss placed: %s, figi=%s, stop=%.2f",
                     response.stop_order_id, figi, stop_price)
        return response.stop_order_id

    def place_take_profit(
        self,
        account_id: str,
        figi: str,
        qty: int,
        direction: StopOrderDirection,
        tp_price: float,
    ) -> str:
        """Разместить тейк-профит (GTC). Вернуть stop_order_id."""
        with self._client() as client:
            response = client.stop_orders.post_stop_order(
                figi=figi,
                quantity=qty,
                stop_price=_float_to_quotation(tp_price),
                direction=direction,
                account_id=account_id,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4()),
            )
        logger.info("[BROKER] Take-profit placed: %s, figi=%s, tp=%.2f",
                     response.stop_order_id, figi, tp_price)
        return response.stop_order_id

    def cancel_order(self, account_id: str, order_id: str) -> None:
        """Отменить обычный ордер."""
        with self._client() as client:
            client.orders.cancel_order(account_id=account_id, order_id=order_id)
        logger.info("[BROKER] Order cancelled: %s", order_id)

    def cancel_stop_order(self, account_id: str, stop_order_id: str) -> None:
        """Отменить стоп-ордер."""
        with self._client() as client:
            client.stop_orders.cancel_stop_order(
                account_id=account_id, stop_order_id=stop_order_id
            )
        logger.info("[BROKER] Stop order cancelled: %s", stop_order_id)

    def get_order_state(self, account_id: str, order_id: str):
        """Получить статус ордера."""
        with self._client() as client:
            return client.orders.get_order_state(
                account_id=account_id, order_id=order_id
            )

    def get_stop_orders(self, account_id: str) -> list:
        """Получить список активных стоп-ордеров."""
        with self._client() as client:
            return client.stop_orders.get_stop_orders(
                account_id=account_id
            ).stop_orders

    # ------------------------------------------------------------------
    # Песочница
    # ------------------------------------------------------------------

    def sandbox_create_account(self, name: str = "TraderBot") -> str:
        """Создать аккаунт в песочнице."""
        if not self.sandbox:
            raise RuntimeError("sandbox_create_account() called in live mode")
        with self._client() as client:
            response = client.sandbox.open_sandbox_account(name=name)
        logger.info("[SANDBOX] Account created: %s", response.account_id)
        return response.account_id

    def sandbox_fund(self, account_id: str, amount: float, currency: str = "rub") -> None:
        """Пополнить аккаунт в песочнице."""
        if not self.sandbox:
            raise RuntimeError("sandbox_fund() called in live mode")
        units = int(amount)
        nano = round((amount - units) * 1_000_000_000)
        with self._client() as client:
            client.sandbox.sandbox_pay_in(
                account_id=account_id,
                amount=MoneyValue(currency=currency, units=units, nano=nano),
            )
        logger.info("[SANDBOX] Funded %.2f %s to %s", amount, currency.upper(), account_id)


# ------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------

def _quotation_to_float(q) -> float:
    return q.units + q.nano / 1_000_000_000


def _float_to_quotation(value: float) -> Quotation:
    total_nano = round(value * 1_000_000_000)
    units = int(total_nano // 1_000_000_000)
    nano = int(total_nano % 1_000_000_000)
    return Quotation(units=units, nano=nano)


def round_to_step(price: float, step: float) -> float:
    """Округлить цену до ближайшего шага цены инструмента."""
    if step <= 0:
        return price
    step_nano = round(step * 1_000_000_000)
    if step_nano <= 0:
        return price
    price_nano = round(price * 1_000_000_000)
    snapped_nano = round(price_nano / step_nano) * step_nano
    return snapped_nano / 1_000_000_000


def _candles_to_dataframe(candles: list) -> pd.DataFrame:
    records = [
        {
            "time": c.time,
            "open": _quotation_to_float(c.open),
            "high": _quotation_to_float(c.high),
            "low": _quotation_to_float(c.low),
            "close": _quotation_to_float(c.close),
            "volume": c.volume,
        }
        for c in candles
    ]
    df = pd.DataFrame(records)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
    return df
