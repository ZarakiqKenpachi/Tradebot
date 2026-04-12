import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from traderbot.config import AppConfig
from traderbot.risk.manager import RiskManager
from traderbot.strategies.base import BaseStrategy
from traderbot.strategies.registry import get_strategy
from traderbot.types import Setup, Signal, TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class _VirtualPosition:
    ticker: str
    figi: str
    direction: Signal
    entry_price: float
    stop_price: float
    target_price: float
    qty: int        # в лотах
    lot_size: int   # акций в одном лоте
    entry_time: datetime
    entry_reason: str
    candles_held: int = 0


class BacktestEngine:
    """Движок симуляции бэктеста."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.risk = RiskManager(config.risk_pct, config.max_position_pct, config.backtest_initial_balance)
        self.commission_pct = config.commission_pct
        self.max_candles_timeout = config.max_candles_timeout

    def run(self, all_data: dict[str, dict[str, pd.DataFrame]]) -> list[TradeRecord]:
        """
        Прогнать симуляцию по историческим данным.

        all_data: {ticker_name: {timeframe: DataFrame}}
        """
        trades: list[TradeRecord] = []
        balance = self.config.backtest_initial_balance

        for ticker_name, ticker_conf in self.config.tickers.items():
            if ticker_name not in all_data:
                continue

            candles_dict = all_data[ticker_name]
            strategy = get_strategy(ticker_conf.strategy)
            min_tf = strategy.required_timeframes[0]

            if min_tf not in candles_dict or candles_dict[min_tf].empty:
                continue

            df_entry = candles_dict[min_tf]
            ticker_trades = self._simulate_ticker(
                ticker_name, ticker_conf.figi, ticker_conf.lot_size,
                strategy, candles_dict, df_entry, balance
            )
            trades.extend(ticker_trades)

            for t in ticker_trades:
                balance += t.pnl

        return trades

    def _simulate_ticker(
        self,
        ticker: str,
        figi: str,
        lot_size: int,
        strategy: BaseStrategy,
        candles_dict: dict[str, pd.DataFrame],
        df_entry: pd.DataFrame,
        balance: float,
    ) -> list[TradeRecord]:
        """Симуляция для одного тикера с 1m-точностью входа/выхода."""
        df_1m = candles_dict.get("1m")
        trades: list[TradeRecord] = []
        position: _VirtualPosition | None = None
        pending: tuple[Setup, int] | None = None  # (setup, qty) — ожидаем заполнения лимитки
        used_setup_key: tuple | None = None  # блокировка повторного входа в тот же сетап
        min_bars = 20

        for i in range(min_bars, len(df_entry)):
            current_time = df_entry.index[i]
            bar_1m = self._get_1m_slice(df_1m, df_entry, i)

            # === Управление открытой позицией ===
            if position is not None:
                position.candles_held += 1

                exit_trade = self._scan_exit(position, bar_1m)
                if exit_trade:
                    trades.append(exit_trade)
                    balance += exit_trade.pnl
                    used_setup_key = (position.direction, position.entry_price, position.stop_price)
                    position = None
                elif position.candles_held >= self.max_candles_timeout:
                    if not bar_1m.empty:
                        exit_price = bar_1m.iloc[-1]["close"]
                        exit_ts = bar_1m.index[-1]
                    else:
                        exit_price = df_entry.iloc[i]["close"]
                        exit_ts = current_time
                    trade = self._close(position, exit_price, "timeout", exit_ts)
                    trades.append(trade)
                    balance += trade.pnl
                    used_setup_key = None  # таймаут — разрешаем переоткрытие
                    position = None
                continue

            # === Ожидание заполнения лимитного ордера ===
            if pending is not None:
                setup, qty = pending
                result = self._scan_fill(setup, qty, lot_size, ticker, figi, bar_1m)
                if result == "invalidated":
                    pending = None
                    # Сетап аннулирован — ищем новый в этом же баре (ниже)
                elif result is not None:
                    position = result
                    pending = None
                    # Проверить оставшиеся 1m-свечи того же бара после точки входа
                    remaining = self._remaining_1m(bar_1m, result.entry_time)
                    if not remaining.empty:
                        exit_trade = self._scan_exit(position, remaining)
                        if exit_trade:
                            trades.append(exit_trade)
                            balance += exit_trade.pnl
                            used_setup_key = (position.direction, position.entry_price, position.stop_price)
                            position = None
                    continue
                else:
                    continue  # Ещё ждём заполнения

            # === Поиск нового сетапа ===
            # Не искать точки входа в выходные дни (МСК)
            current_time_msk = current_time.tz_convert("Europe/Moscow")
            if current_time_msk.weekday() >= 5:  # Сб=5, Вс=6
                continue

            window = {tf: df[df.index <= current_time] for tf, df in candles_dict.items() if tf != "1m"}
            setup = strategy.find_setup(window)
            if setup is None:
                used_setup_key = None  # сетап сменился — снимаем блокировку
                continue

            # Пропустить сетап, если это тот же, по которому уже был SL/TP
            setup_key = (setup.direction, setup.entry_price, setup.stop_price)
            if used_setup_key == setup_key:
                continue
            used_setup_key = None  # новый сетап — сбрасываем
            qty = self.risk.position_size(balance, setup.entry_price, setup.stop_price, lot_size)
            if qty < 1:
                continue

            strategy.on_trade_opened()

            # Пробуем заполнить прямо на 1m-свечах текущего бара
            result = self._scan_fill(setup, qty, lot_size, ticker, figi, bar_1m)
            if result == "invalidated":
                pass  # Не вошли
            elif result is not None:
                position = result
                # Проверить оставшиеся 1m-свечи того же бара после точки входа
                remaining = self._remaining_1m(bar_1m, result.entry_time)
                if not remaining.empty:
                    exit_trade = self._scan_exit(position, remaining)
                    if exit_trade:
                        trades.append(exit_trade)
                        balance += exit_trade.pnl
                        used_setup_key = (position.direction, position.entry_price, position.stop_price)
                        position = None
            else:
                pending = (setup, qty)  # Ждём следующих баров

        # Закрыть оставшуюся позицию по последней цене
        if position is not None:
            if df_1m is not None and not df_1m.empty:
                exit_price = df_1m.iloc[-1]["close"]
                exit_ts = df_1m.index[-1]
            else:
                exit_price = df_entry.iloc[-1]["close"]
                exit_ts = df_entry.index[-1]
            trade = self._close(position, exit_price, "end_of_data", exit_ts)
            trades.append(trade)

        logger.info("[BACKTEST] %s: %d trades", ticker, len(trades))
        return trades

    # ── Вспомогательные методы ──────────────────────────────────────────────

    def _remaining_1m(
        self, bar_1m: pd.DataFrame, entry_time: datetime
    ) -> pd.DataFrame:
        """1m-свечи в том же баре, строго после момента входа."""
        if bar_1m is None or bar_1m.empty:
            return pd.DataFrame()
        entry_ts = pd.Timestamp(entry_time)
        return bar_1m[bar_1m.index > entry_ts]

    def _get_1m_slice(
        self, df_1m: pd.DataFrame | None, df_entry: pd.DataFrame, i: int
    ) -> pd.DataFrame:
        """Срез 1m-свечей для i-го бара df_entry: (prev_close, current_close]."""
        if df_1m is None or df_1m.empty:
            return pd.DataFrame()
        start = df_entry.index[i - 1]
        end = df_entry.index[i]
        return df_1m[(df_1m.index > start) & (df_1m.index <= end)]

    def _scan_exit(
        self, pos: _VirtualPosition, bar_1m: pd.DataFrame
    ) -> TradeRecord | None:
        """Пройтись по 1m-свечам, найти первое касание SL или TP.

        Если свеча гэпнула через уровень (open уже за SL/TP),
        используем open свечи как реальную цену исполнения (проскальзывание).
        """
        for ts, c in bar_1m.iterrows():
            if pos.direction == Signal.BUY:
                if c["low"] <= pos.stop_price:
                    # Гэп через SL: open ниже stop_price → исполнение по open
                    price = min(c["open"], pos.stop_price)
                    return self._close(pos, price, "stop_loss", ts)
                if c["high"] >= pos.target_price:
                    # Гэп через TP: open выше target → исполнение по open
                    price = max(c["open"], pos.target_price)
                    return self._close(pos, price, "take_profit", ts)
            else:
                if c["high"] >= pos.stop_price:
                    # SHORT: гэп через SL вверх → open выше stop
                    price = max(c["open"], pos.stop_price)
                    return self._close(pos, price, "stop_loss", ts)
                if c["low"] <= pos.target_price:
                    # SHORT: гэп через TP вниз → open ниже target
                    price = min(c["open"], pos.target_price)
                    return self._close(pos, price, "take_profit", ts)
        return None

    def _scan_fill(
        self,
        setup: Setup,
        qty: int,
        lot_size: int,
        ticker: str,
        figi: str,
        bar_1m: pd.DataFrame,
    ) -> "_VirtualPosition | str | None":
        """
        Ищет заполнение лимитного ордера на 1m-свечах.
        Возвращает: _VirtualPosition (заполнен), 'invalidated' (SL пробит до входа), None (не заполнен).
        """
        for ts, c in bar_1m.iterrows():
            if setup.direction == Signal.BUY:
                # SL пробит до входа — аннулируем
                if c["low"] <= setup.stop_price:
                    return "invalidated"
                # Цена откатилась к уровню входа — заполнение
                if c["low"] <= setup.entry_price:
                    return self._make_position(setup, qty, lot_size, ticker, figi, ts)
            else:
                if c["high"] >= setup.stop_price:
                    return "invalidated"
                if c["high"] >= setup.entry_price:
                    return self._make_position(setup, qty, lot_size, ticker, figi, ts)
        return None

    def _make_position(
        self, setup: Setup, qty: int, lot_size: int, ticker: str, figi: str, ts: pd.Timestamp
    ) -> _VirtualPosition:
        entry_dt = ts.to_pydatetime()
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        return _VirtualPosition(
            ticker=ticker,
            figi=figi,
            direction=setup.direction,
            entry_price=setup.entry_price,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            qty=qty,
            lot_size=lot_size,
            entry_time=entry_dt,
            entry_reason=setup.entry_reason,
        )

    def _close(
        self,
        pos: _VirtualPosition,
        exit_price: float,
        reason: str,
        exit_time,
    ) -> TradeRecord:
        shares = pos.qty * pos.lot_size
        if pos.direction == Signal.BUY:
            pnl = (exit_price - pos.entry_price) * shares
        else:
            pnl = (pos.entry_price - exit_price) * shares

        avg_price = (pos.entry_price + exit_price) / 2
        commission = 2 * self.commission_pct * avg_price * shares
        pnl_net = pnl - commission

        if hasattr(exit_time, "to_pydatetime"):
            exit_dt = exit_time.to_pydatetime()
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
        elif isinstance(exit_time, datetime):
            exit_dt = exit_time
        else:
            exit_dt = datetime.now(timezone.utc)

        return TradeRecord(
            ticker=pos.ticker,
            figi=pos.figi,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            qty=pos.qty,
            pnl=pnl_net,
            commission=commission,
            entry_time=pos.entry_time,
            exit_time=exit_dt,
            entry_reason=pos.entry_reason,
            exit_reason=reason,
            candles_held=pos.candles_held,
        )
