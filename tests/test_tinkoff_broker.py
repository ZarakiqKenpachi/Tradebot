"""
Tests for TinkoffBroker — mocks the t_tech.invest SDK so no real token needed.
Run with: py -3.12 -m pytest tests/test_tinkoff_broker.py -v
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from t_tech.invest import CandleInterval, OrderDirection
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX

from bot.broker.tinkoff import TinkoffBroker, _q

PATCH_CLIENT = "bot.broker.tinkoff.Client"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quotation(value: float):
    q = MagicMock()
    q.units = int(value)
    q.nano = round((value - int(value)) * 1_000_000_000)
    return q


def _fake_candle(open_, high, low, close, volume, time="2024-01-01T10:00:00+00:00"):
    c = MagicMock()
    c.open = _quotation(open_)
    c.high = _quotation(high)
    c.low = _quotation(low)
    c.close = _quotation(close)
    c.volume = volume
    c.time = datetime.fromisoformat(time)
    return c


def _setup(sandbox=False):
    """Returns (broker, mock_client, patcher). Caller must call patcher.stop()."""
    patcher = patch(PATCH_CLIENT)
    mock_cls = patcher.start()
    broker = TinkoffBroker(token="test", sandbox=sandbox)
    mock_client = mock_cls.return_value.__enter__.return_value
    return broker, mock_client, mock_cls, patcher


# ---------------------------------------------------------------------------
# _q helper
# ---------------------------------------------------------------------------

def test_quotation_to_float_whole():
    assert _q(_quotation(100.0)) == pytest.approx(100.0)


def test_quotation_to_float_decimal():
    assert _q(_quotation(123.456)) == pytest.approx(123.456, rel=1e-6)


# ---------------------------------------------------------------------------
# Sandbox vs live target
# ---------------------------------------------------------------------------

class TestTarget:
    def test_live_uses_production_endpoint(self):
        broker, _, mock_cls, p = _setup(sandbox=False)
        broker.get_account_id()
        p.stop()
        mock_cls.assert_called_with("test", target=INVEST_GRPC_API)

    def test_sandbox_uses_sandbox_endpoint(self):
        broker, _, mock_cls, p = _setup(sandbox=True)
        broker.get_account_id()
        p.stop()
        mock_cls.assert_called_with("test", target=INVEST_GRPC_API_SANDBOX)


# ---------------------------------------------------------------------------
# get_candles
# ---------------------------------------------------------------------------

class TestGetCandles:
    def test_returns_dataframe(self):
        broker, mc, _, p = _setup()
        mc.market_data.get_candles.return_value.candles = [_fake_candle(100, 110, 90, 105, 1000)]
        df = broker.get_candles("BBG000", CandleInterval.CANDLE_INTERVAL_1_MIN, days=1)
        p.stop()
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_ohlcv_values_correct(self):
        broker, mc, _, p = _setup()
        mc.market_data.get_candles.return_value.candles = [_fake_candle(100, 110, 90, 105, 999)]
        df = broker.get_candles("BBG000", CandleInterval.CANDLE_INTERVAL_1_MIN, days=1)
        p.stop()
        assert df.iloc[0]["open"] == pytest.approx(100.0)
        assert df.iloc[0]["high"] == pytest.approx(110.0)
        assert df.iloc[0]["low"] == pytest.approx(90.0)
        assert df.iloc[0]["close"] == pytest.approx(105.0)
        assert df.iloc[0]["volume"] == 999

    def test_empty_response_returns_empty_dataframe(self):
        broker, mc, _, p = _setup()
        mc.market_data.get_candles.return_value.candles = []
        df = broker.get_candles("BBG000", CandleInterval.CANDLE_INTERVAL_1_MIN, days=1)
        p.stop()
        assert df.empty

    def test_multiple_candles_sorted_by_time(self):
        broker, mc, _, p = _setup()
        mc.market_data.get_candles.return_value.candles = [
            _fake_candle(105, 115, 95, 110, 500, "2024-01-01T11:00:00+00:00"),
            _fake_candle(100, 110, 90, 105, 999, "2024-01-01T10:00:00+00:00"),
        ]
        df = broker.get_candles("BBG000", CandleInterval.CANDLE_INTERVAL_1_MIN, days=1)
        p.stop()
        assert df.index[0] < df.index[1]

    def test_index_is_datetime(self):
        broker, mc, _, p = _setup()
        mc.market_data.get_candles.return_value.candles = [_fake_candle(100, 110, 90, 105, 1)]
        df = broker.get_candles("BBG000", CandleInterval.CANDLE_INTERVAL_1_MIN, days=1)
        p.stop()
        assert pd.api.types.is_datetime64_any_dtype(df.index)


# ---------------------------------------------------------------------------
# get_account_id
# ---------------------------------------------------------------------------

class TestGetAccountId:
    def test_returns_first_account(self):
        broker, mc, _, p = _setup()
        mc.users.get_accounts.return_value.accounts = [MagicMock(id="acc-1")]
        result = broker.get_account_id()
        p.stop()
        assert result == "acc-1"


# ---------------------------------------------------------------------------
# get_portfolio_balance
# ---------------------------------------------------------------------------

class TestGetPortfolioBalance:
    def test_returns_total_amount(self):
        broker, mc, _, p = _setup()
        mc.operations.get_portfolio.return_value.total_amount_portfolio = _quotation(75000.50)
        result = broker.get_portfolio_balance("acc-1")
        p.stop()
        assert result == pytest.approx(75000.50, rel=1e-4)


# ---------------------------------------------------------------------------
# place_limit_order
# ---------------------------------------------------------------------------

class TestPlaceLimitOrder:
    def test_returns_order_id(self):
        broker, mc, _, p = _setup()
        mc.orders.post_order.return_value.order_id = "order-abc"
        result = broker.place_limit_order("acc-1", "BBG000", 10, OrderDirection.ORDER_DIRECTION_BUY, 250.50)
        p.stop()
        assert result == "order-abc"

    def test_order_id_is_valid_uuid(self):
        import uuid
        broker, mc, _, p = _setup()
        mc.orders.post_order.return_value.order_id = "x"
        broker.place_limit_order("acc-1", "BBG000", 1, OrderDirection.ORDER_DIRECTION_BUY, 100.0)
        p.stop()
        call_kwargs = mc.orders.post_order.call_args.kwargs
        uuid.UUID(call_kwargs["order_id"])  # raises if not valid UUID


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    def test_calls_cancel_with_correct_args(self):
        broker, mc, _, p = _setup()
        broker.cancel_order("acc-1", "order-abc")
        p.stop()
        mc.orders.cancel_order.assert_called_once_with(account_id="acc-1", order_id="order-abc")
