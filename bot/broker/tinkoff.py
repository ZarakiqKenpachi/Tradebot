import uuid
from datetime import timedelta

import pandas as pd
from t_tech.invest import (
    CandleInterval,
    Client,
    ExchangeOrderType,
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


class TinkoffBroker:
    def __init__(self, token: str, sandbox: bool = False):
        self.token = token
        self.sandbox = sandbox
        self._target = INVEST_GRPC_API_SANDBOX if sandbox else INVEST_GRPC_API
        if sandbox:
            print("[BROKER] Running in SANDBOX mode — no real money at risk.")

    def _client(self) -> Client:
        return Client(self.token, target=self._target)

    def get_all_candles(self, figi: str, interval: CandleInterval, days: int) -> pd.DataFrame:
        """Fetch historical candles with automatic pagination (use for backtesting)."""
        with self._client() as client:
            candles = list(client.get_all_candles(
                figi=figi,
                from_=now() - timedelta(days=days),
                interval=interval,
            ))
        return _candles_to_df(candles)

    def get_candles(self, figi: str, interval: CandleInterval, days: int) -> pd.DataFrame:
        """Fetch recent candles (single call, limited window — use for live trading)."""
        with self._client() as client:
            candles = client.market_data.get_candles(
                figi=figi,
                from_=now() - timedelta(days=days),
                to=now(),
                interval=interval,
            ).candles
        return _candles_to_df(candles)

    def get_account_id(self) -> str:
        with self._client() as client:
            return client.users.get_accounts().accounts[0].id

    def get_portfolio_balance(self, account_id: str) -> float:
        with self._client() as client:
            portfolio = client.operations.get_portfolio(account_id=account_id)
        return _q(portfolio.total_amount_portfolio)

    def place_limit_order(
        self,
        account_id: str,
        figi: str,
        quantity: int,
        direction: OrderDirection,
        price: float,
    ) -> str:
        with self._client() as client:
            response = client.orders.post_order(
                figi=figi,
                quantity=quantity,
                price=_to_quotation(price),
                direction=direction,
                account_id=account_id,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id=str(uuid.uuid4()),
            )
        return response.order_id

    def cancel_order(self, account_id: str, order_id: str):
        with self._client() as client:
            client.orders.cancel_order(account_id=account_id, order_id=order_id)

    def place_stop_loss(
        self, account_id: str, figi: str, quantity: int,
        direction: StopOrderDirection, stop_price: float,
    ) -> str:
        with self._client() as client:
            response = client.stop_orders.post_stop_order(
                figi=figi,
                quantity=quantity,
                stop_price=_to_quotation(stop_price),
                direction=direction,
                account_id=account_id,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4()),
            )
        return response.stop_order_id

    def place_take_profit(
        self, account_id: str, figi: str, quantity: int,
        direction: StopOrderDirection, tp_price: float,
    ) -> str:
        with self._client() as client:
            response = client.stop_orders.post_stop_order(
                figi=figi,
                quantity=quantity,
                stop_price=_to_quotation(tp_price),
                direction=direction,
                account_id=account_id,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4()),
            )
        return response.stop_order_id

    def cancel_stop_order(self, account_id: str, stop_order_id: str):
        with self._client() as client:
            client.stop_orders.cancel_stop_order(
                account_id=account_id, stop_order_id=stop_order_id
            )

    # ------------------------------------------------------------------
    # Sandbox-only helpers
    # ------------------------------------------------------------------

    def sandbox_create_account(self, name: str = "TraderBot") -> str:
        """Create a sandbox account and return its ID."""
        if not self.sandbox:
            raise RuntimeError("sandbox_create_account() called in live mode")
        with self._client() as client:
            response = client.sandbox.open_sandbox_account(name=name)
        print(f"[SANDBOX] Account created: {response.account_id}")
        return response.account_id

    def sandbox_fund(self, account_id: str, amount: float, currency: str = "rub"):
        """Deposit virtual money into a sandbox account."""
        if not self.sandbox:
            raise RuntimeError("sandbox_fund() called in live mode")
        units = int(amount)
        nano = round((amount - units) * 1_000_000_000)
        with self._client() as client:
            client.sandbox.sandbox_pay_in(
                account_id=account_id,
                amount=MoneyValue(currency=currency, units=units, nano=nano),
            )
        print(f"[SANDBOX] Funded {amount} {currency.upper()} to account {account_id}")


def _to_quotation(value: float) -> Quotation:
    units = int(value)
    nano = round((value - units) * 1_000_000_000)
    return Quotation(units=units, nano=nano)


def _q(quotation) -> float:
    return quotation.units + quotation.nano / 1_000_000_000


def _candles_to_df(candles) -> pd.DataFrame:
    records = [
        {
            "time": c.time,
            "open": _q(c.open),
            "high": _q(c.high),
            "low": _q(c.low),
            "close": _q(c.close),
            "volume": c.volume,
        }
        for c in candles
    ]
    df = pd.DataFrame(records)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
    return df
