from dataclasses import dataclass, field

import pandas as pd
from t_tech.invest import OrderDirection, StopOrderDirection

from bot.logs.journal import TradeJournal
from bot.risk.manager import RiskManager
from bot.strategies.base import Signal
from bot.strategies.ict import ICTSetup

MAX_CANDLES_30M = 10


@dataclass
class _Position:
    figi: str
    direction: Signal
    entry_price: float
    stop_price: float
    target_price: float
    qty: int
    entry_order_id: str
    sl_order_id: str = ""
    tp_order_id: str = ""
    candles_held: int = 0


class ExecutionManager:
    def __init__(self, broker, risk: RiskManager, journal: TradeJournal, account_id: str):
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.account_id = account_id
        self._positions: dict[str, _Position] = {}

    def has_position(self, figi: str) -> bool:
        return figi in self._positions

    def open_position(self, figi: str, setup: ICTSetup):
        balance = self.broker.get_portfolio_balance(self.account_id)
        qty = self.risk.position_size(balance, setup.entry_price, setup.stop_price)
        if qty < 1:
            print(f"[EXEC] Position size too small for {figi}, skipping.")
            return

        entry_direction = (
            OrderDirection.ORDER_DIRECTION_BUY
            if setup.direction == Signal.BUY
            else OrderDirection.ORDER_DIRECTION_SELL
        )
        # Closing direction for SL/TP is the opposite of entry
        close_direction = (
            StopOrderDirection.STOP_ORDER_DIRECTION_SELL
            if setup.direction == Signal.BUY
            else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
        )

        entry_order_id = self.broker.place_limit_order(
            self.account_id, figi, qty, entry_direction, setup.entry_price
        )

        # Place SL and TP on the exchange immediately
        sl_order_id = self.broker.place_stop_loss(
            self.account_id, figi, qty, close_direction, setup.stop_price
        )
        tp_order_id = self.broker.place_take_profit(
            self.account_id, figi, qty, close_direction, setup.target_price
        )

        self._positions[figi] = _Position(
            figi=figi,
            direction=setup.direction,
            entry_price=setup.entry_price,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            qty=qty,
            entry_order_id=entry_order_id,
            sl_order_id=sl_order_id,
            tp_order_id=tp_order_id,
        )
        print(
            f"[EXEC] {setup.direction.value} limit placed | {figi} "
            f"entry={setup.entry_price} sl={setup.stop_price} tp={setup.target_price} qty={qty}"
        )

    def update(self, figi: str, df_30m: pd.DataFrame):
        """Call once per 30m candle — only handles timeout, SL/TP are on the exchange."""
        if figi not in self._positions or df_30m.empty:
            return

        pos = self._positions[figi]
        pos.candles_held += 1

        if pos.candles_held >= MAX_CANDLES_30M:
            price = df_30m.iloc[-1]["close"]
            self._close(pos, price, "timeout")

    def _close(self, pos: _Position, exit_price: float, reason: str):
        del self._positions[pos.figi]

        # Cancel the entry limit order and both stop orders
        for order_id in [pos.entry_order_id]:
            try:
                self.broker.cancel_order(self.account_id, order_id)
            except Exception:
                pass
        for stop_id in [pos.sl_order_id, pos.tp_order_id]:
            if stop_id:
                try:
                    self.broker.cancel_stop_order(self.account_id, stop_id)
                except Exception:
                    pass

        pnl = (
            (exit_price - pos.entry_price) * pos.qty
            if pos.direction == Signal.BUY
            else (pos.entry_price - exit_price) * pos.qty
        )
        self.journal.log_trade(
            figi=pos.figi,
            direction=pos.direction.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            pnl=pnl,
            reason=reason,
            candles_held=pos.candles_held,
        )
        print(f"[EXEC] Closed {pos.direction.value} {pos.figi} | exit={exit_price} reason={reason}")
