import logging
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from traderbot.broker.tbank import round_to_step
from traderbot.config import AppConfig
from traderbot.risk.manager import RiskManager
from traderbot.strategies.base import BaseStrategy
from traderbot.strategies.registry import get_strategy
from traderbot.types import Setup, Signal, TradeRecord

logger = logging.getLogger(__name__)

# Таймаут ожидания лимитного ордера — как в live ExecutionManager
PENDING_TIMEOUT_CANDLES = 20  # 30m-свечей
_DAYS_LIMIT = 15               # скользящее окно данных для стратегий
_MIN_BARS = 20                 # минимум баров для разогрева


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
    balance_at_entry: float = 0.0
    candles_held: int = 0
    last_30m_time: pd.Timestamp | None = None  # для счёта в 30m-свечах (как в live)


@dataclass
class _TickerState:
    """Мутабельное состояние одного тикера в процессе симуляции."""
    ticker: str
    figi: str
    lot_size: int
    price_step: float
    strategy: BaseStrategy
    candles_dict: dict
    df_entry: pd.DataFrame
    df_1m: pd.DataFrame | None
    df_30m: pd.DataFrame | None
    dividend_dates: list = field(default_factory=list)  # last_buy_date objects
    # мутабельное
    position: _VirtualPosition | None = None
    pending: tuple | None = None              # (setup, qty, balance_at_signal)
    pending_30m_count: int = 0
    pending_last_30m: pd.Timestamp | None = None
    consecutive_sl: dict = field(default_factory=dict)
    sl_date: dict = field(default_factory=dict)


class BacktestEngine:
    """Движок симуляции бэктеста."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.risk = RiskManager(config.risk_pct, config.max_position_pct, config.backtest_initial_balance)
        self.commission_pct = config.commission_pct
        self.max_candles_timeout = config.max_candles_timeout
        self.max_consecutive_sl = config.max_consecutive_sl
        self.slippage_pct = config.backtest_slippage_pct
        self.max_open_positions = config.max_open_positions

    def run(
        self,
        all_data: dict[str, dict[str, pd.DataFrame]],
        trading_schedule: dict | None = None,
        dividend_data: dict[str, list] | None = None,
    ) -> list[TradeRecord]:
        """
        Прогнать симуляцию по историческим данным.

        all_data: {ticker_name: {timeframe: DataFrame}}
        trading_schedule: {date: TradingDay} из TBankBroker.get_trading_schedule()
        dividend_data: {ticker_name: [last_buy_date, ...]} для фильтрации шортов

        Все тикеры обрабатываются в едином хронологическом порядке,
        поэтому баланс обновляется сразу после каждой закрытой сделки
        и влияет на размер следующих позиций по любому тикеру.
        """
        trades: list[TradeRecord] = []
        balance = self.config.backtest_initial_balance

        # Инициализировать состояние для каждого тикера
        states: dict[str, _TickerState] = {}
        for ticker_name, ticker_conf in self.config.tickers.items():
            if ticker_name not in all_data:
                continue
            candles_dict = all_data[ticker_name]
            strategy = get_strategy(ticker_conf.strategy)
            min_tf = strategy.required_timeframes[0]
            if min_tf not in candles_dict or candles_dict[min_tf].empty:
                continue
            states[ticker_name] = _TickerState(
                ticker=ticker_name,
                figi=ticker_conf.figi,
                lot_size=ticker_conf.lot_size,
                price_step=ticker_conf.price_step,
                strategy=strategy,
                candles_dict=candles_dict,
                df_entry=candles_dict[min_tf],
                df_1m=candles_dict.get("1m"),
                df_30m=candles_dict.get("30m"),
                dividend_dates=(dividend_data or {}).get(ticker_name, []),
                consecutive_sl={ticker_name: 0},
                sl_date={ticker_name: ""},
            )

        if not states:
            return trades

        # Собрать единый хронологический список событий (timestamp, ticker, bar_index)
        events: list[tuple] = []
        for ticker_name, state in states.items():
            for i in range(_MIN_BARS, len(state.df_entry)):
                events.append((state.df_entry.index[i], ticker_name, i))
        events.sort(key=lambda e: e[0])

        # Обработать все события с общим балансом
        for ts, ticker_name, i in events:
            open_count = sum(
                1 for s in states.values()
                if s.position is not None or s.pending is not None
            )
            new_trades, balance = self._process_bar(
                states[ticker_name], i, balance, trading_schedule, open_count
            )
            trades.extend(new_trades)

        # Закрыть оставшиеся позиции по последней цене
        for ticker_name, state in states.items():
            if state.position is not None:
                if state.df_1m is not None and not state.df_1m.empty:
                    exit_price = state.df_1m.iloc[-1]["close"]
                    exit_ts = state.df_1m.index[-1]
                else:
                    exit_price = state.df_entry.iloc[-1]["close"]
                    exit_ts = state.df_entry.index[-1]
                trade = self._close(state.position, exit_price, "end_of_data", exit_ts)
                trades.append(trade)
                balance += trade.pnl

            ticker_count = sum(1 for t in trades if t.ticker == ticker_name)
            logger.info("[BACKTEST] %s: %d trades", ticker_name, ticker_count)

        return trades

    def _process_bar(
        self,
        state: _TickerState,
        i: int,
        balance: float,
        trading_schedule: dict | None,
        open_count: int = 0,
    ) -> tuple[list[TradeRecord], float]:
        """Обработать один бар df_entry[i] для тикера. Возвращает сделки + обновлённый баланс."""
        MSK = ZoneInfo("Europe/Moscow")
        trades: list[TradeRecord] = []

        current_time = state.df_entry.index[i]
        bar_1m = self._get_1m_slice(state.df_1m, state.df_entry, i)
        current_30m = self._get_30m_bar_at(state.df_30m, current_time)

        # === Управление открытой позицией ===
        if state.position is not None:
            # Инкремент candles_held только при появлении новой 30m-свечи (как в live)
            if current_30m is not None and current_30m != state.position.last_30m_time:
                state.position.candles_held += 1
                state.position.last_30m_time = current_30m

            exit_trade = self._scan_exit(state.position, bar_1m)
            if exit_trade:
                trades.append(exit_trade)
                balance += exit_trade.pnl
                self._update_consecutive_sl(
                    exit_trade.exit_reason, state.ticker,
                    state.consecutive_sl, state.sl_date, exit_trade.exit_time, MSK,
                )
                state.position = None
                return trades, balance

            if state.position.candles_held >= self.max_candles_timeout:
                if not bar_1m.empty:
                    exit_price = self._apply_slippage(bar_1m.iloc[-1]["close"], state.position.direction)
                    exit_ts = bar_1m.index[-1]
                else:
                    exit_price = self._apply_slippage(state.df_entry.iloc[i]["close"], state.position.direction)
                    exit_ts = current_time
                trade = self._close(state.position, exit_price, "timeout", exit_ts)
                trades.append(trade)
                balance += trade.pnl
                self._update_consecutive_sl(
                    "timeout", state.ticker,
                    state.consecutive_sl, state.sl_date, trade.exit_time, MSK,
                )
                state.position = None

            return trades, balance  # позиция открыта или только что закрыта по таймауту

        # === Ожидание заполнения лимитного ордера ===
        if state.pending is not None:
            if current_30m is not None and current_30m != state.pending_last_30m:
                state.pending_30m_count += 1
                state.pending_last_30m = current_30m

            if state.pending_30m_count >= PENDING_TIMEOUT_CANDLES:
                logger.debug("[BACKTEST] %s pending timeout after %d 30m candles",
                             state.ticker, state.pending_30m_count)
                state.pending = None
                state.pending_30m_count = 0
                state.pending_last_30m = None
                # Fall through to setup search
            else:
                setup, qty, pending_balance = state.pending
                result = self._scan_fill(setup, qty, state.lot_size, state.ticker, state.figi,
                                         bar_1m, pending_balance)
                if result == "invalidated":
                    state.pending = None
                    state.pending_30m_count = 0
                    state.pending_last_30m = None
                    # Fall through to setup search
                elif result is not None:
                    result.last_30m_time = current_30m
                    state.position = result
                    state.pending = None
                    state.pending_30m_count = 0
                    state.pending_last_30m = None
                    remaining = self._remaining_1m(bar_1m, result.entry_time)
                    if not remaining.empty:
                        exit_trade = self._scan_exit(state.position, remaining)
                        if exit_trade:
                            trades.append(exit_trade)
                            balance += exit_trade.pnl
                            self._update_consecutive_sl(
                                exit_trade.exit_reason, state.ticker,
                                state.consecutive_sl, state.sl_date, exit_trade.exit_time, MSK,
                            )
                            state.position = None
                    return trades, balance  # лимитка заполнена, следующий бар управляет позицией
                else:
                    return trades, balance  # ещё ждём заполнения

        # === Поиск нового сетапа ===
        current_time_msk = current_time.tz_convert(MSK)
        current_time_utc = current_time.to_pydatetime()
        if current_time_utc.tzinfo is None:
            current_time_utc = current_time_utc.replace(tzinfo=timezone.utc)

        # Фильтр торгового расписания (праздники + торговые часы)
        if trading_schedule is not None:
            day_info = trading_schedule.get(current_time_msk.date())
            if day_info is None or not day_info.is_trading_day:
                return trades, balance
            in_main = bool(
                day_info.start_time and day_info.end_time
                and day_info.start_time <= current_time_utc <= day_info.end_time
            )
            in_evening = bool(
                day_info.evening_start_time and day_info.evening_end_time
                and day_info.evening_start_time <= current_time_utc <= day_info.evening_end_time
            )
            if not in_main and not in_evening:
                return trades, balance
        else:
            if current_time_msk.weekday() >= 5:  # Сб=5, Вс=6
                return trades, balance

        # Блокировка тикера после серии стопов (как ExecutionManager.is_ticker_blocked)
        today_msk = current_time_msk.date().isoformat()
        if (state.sl_date[state.ticker] == today_msk
                and state.consecutive_sl[state.ticker] >= self.max_consecutive_sl):
            return trades, balance

        # Скользящее окно данных — идентично live (main.py)
        window_start = current_time - timedelta(days=_DAYS_LIMIT)
        window = {
            tf: df[(df.index > window_start) & (df.index <= current_time)]
            for tf, df in state.candles_dict.items() if tf != "1m"
        }
        setup = state.strategy.find_setup(window)
        if setup is None:
            return trades, balance

        # Dividend filter: нельзя шортить перед дивидендной отсечкой
        if setup.direction == Signal.SELL and state.dividend_dates:
            bar_date = current_time.date() if hasattr(current_time, 'date') else None
            if bar_date and _is_near_dividend(bar_date, state.dividend_dates):
                return trades, balance

        # Округлить цены до шага инструмента (как round_to_step в live ExecutionManager)
        if state.price_step > 0:
            setup = dc_replace(
                setup,
                entry_price=round_to_step(setup.entry_price, state.price_step),
                stop_price=round_to_step(setup.stop_price, state.price_step),
                target_price=round_to_step(setup.target_price, state.price_step),
            )

        # Limit order price validation:
        # BUY limit cannot be above current price, SELL limit cannot be below
        if bar_1m is not None and not bar_1m.empty:
            current_price = float(bar_1m.iloc[0]["open"])
        else:
            current_price = 0.0
        if current_price > 0:
            is_buy = setup.direction.value == "BUY"
            if is_buy and setup.entry_price > current_price:
                return trades, balance
            if not is_buy and setup.entry_price < current_price:
                return trades, balance

        # Лимит одновременных позиций
        if open_count >= self.max_open_positions:
            return trades, balance

        qty = self.risk.position_size(balance, setup.entry_price, setup.stop_price, state.lot_size)
        if qty < 1:
            return trades, balance

        state.strategy.on_trade_opened()

        result = self._scan_fill(setup, qty, state.lot_size, state.ticker, state.figi, bar_1m, balance)
        if result == "invalidated":
            pass
        elif result is not None:
            result.last_30m_time = current_30m
            state.position = result
            remaining = self._remaining_1m(bar_1m, result.entry_time)
            if not remaining.empty:
                exit_trade = self._scan_exit(state.position, remaining)
                if exit_trade:
                    trades.append(exit_trade)
                    balance += exit_trade.pnl
                    self._update_consecutive_sl(
                        exit_trade.exit_reason, state.ticker,
                        state.consecutive_sl, state.sl_date, exit_trade.exit_time, MSK,
                    )
                    state.position = None
        else:
            state.pending = (setup, qty, balance)
            state.pending_30m_count = 0
            state.pending_last_30m = current_30m

        return trades, balance

    # ── Вспомогательные методы ──────────────────────────────────────────────

    @staticmethod
    def _update_consecutive_sl(
        reason: str,
        ticker: str,
        consecutive_sl: dict[str, int],
        sl_date: dict[str, str],
        exit_time: datetime,
        tz,
    ) -> None:
        """Обновить счётчик consecutive SL — идентично ExecutionManager._close_position."""
        if reason == "stop_loss":
            consecutive_sl[ticker] = consecutive_sl.get(ticker, 0) + 1
            sl_date[ticker] = exit_time.astimezone(tz).date().isoformat()
        elif reason in ("take_profit", "timeout"):
            consecutive_sl[ticker] = 0

    @staticmethod
    def _get_30m_bar_at(
        df_30m: pd.DataFrame | None, current_time: pd.Timestamp
    ) -> pd.Timestamp | None:
        """Последний закрытый 30m-бар на момент current_time."""
        if df_30m is None or df_30m.empty:
            return None
        bars = df_30m[df_30m.index <= current_time]
        return bars.index[-1] if not bars.empty else None

    def _remaining_1m(self, bar_1m: pd.DataFrame, entry_time: datetime) -> pd.DataFrame:
        """1m-свечи в том же баре, строго после момента входа."""
        if bar_1m is None or bar_1m.empty:
            return pd.DataFrame()
        entry_ts = pd.Timestamp(entry_time)
        return bar_1m[bar_1m.index > entry_ts]

    def _apply_slippage(self, price: float, direction: Signal) -> float:
        """Ухудшить цену на slippage_pct для рыночного ордера (SL, timeout).

        BUY-позиция закрывается продажей → цена ниже.
        SELL-позиция закрывается покупкой → цена выше.
        """
        if self.slippage_pct == 0.0:
            return price
        if direction == Signal.BUY:
            return price * (1.0 - self.slippage_pct)
        else:
            return price * (1.0 + self.slippage_pct)

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
                    price = self._apply_slippage(min(c["open"], pos.stop_price), pos.direction)
                    return self._close(pos, price, "stop_loss", ts)
                if c["high"] >= pos.target_price:
                    price = max(c["open"], pos.target_price)  # лимитный TP — без проскальзывания
                    return self._close(pos, price, "take_profit", ts)
            else:
                if c["high"] >= pos.stop_price:
                    price = self._apply_slippage(max(c["open"], pos.stop_price), pos.direction)
                    return self._close(pos, price, "stop_loss", ts)
                if c["low"] <= pos.target_price:
                    price = min(c["open"], pos.target_price)  # лимитный TP — без проскальзывания
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
        balance: float = 0.0,
    ) -> "_VirtualPosition | str | None":
        """
        Ищет заполнение лимитного ордера на 1m-свечах.
        Возвращает: _VirtualPosition (заполнен), 'invalidated' (SL пробит до входа), None (не заполнен).
        """
        for ts, c in bar_1m.iterrows():
            if setup.direction == Signal.BUY:
                if c["low"] <= setup.stop_price:
                    return "invalidated"
                if c["low"] <= setup.entry_price:
                    return self._make_position(setup, qty, lot_size, ticker, figi, ts, balance)
            else:
                if c["high"] >= setup.stop_price:
                    return "invalidated"
                if c["high"] >= setup.entry_price:
                    return self._make_position(setup, qty, lot_size, ticker, figi, ts, balance)
        return None

    def _make_position(
        self, setup: Setup, qty: int, lot_size: int, ticker: str, figi: str,
        ts: pd.Timestamp, balance: float = 0.0,
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
            balance_at_entry=balance,
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

        if hasattr(exit_time, "to_pydatetime"):
            exit_dt = exit_time.to_pydatetime()
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
        elif isinstance(exit_time, datetime):
            exit_dt = exit_time
        else:
            exit_dt = datetime.now(timezone.utc)

        # Комиссия за перенос маржинальных позиций через ночь (T-Bank тарифы)
        position_value = pos.entry_price * shares
        borrowed = max(0.0, position_value - pos.balance_at_entry)
        overnights = self._count_overnights(pos.entry_time, exit_dt)
        margin_cost = self._margin_overnight_cost(borrowed) * overnights

        pnl_net = pnl - commission - margin_cost

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

    @staticmethod
    def _count_overnights(entry_time: datetime, exit_time: datetime) -> int:
        """Количество пересечений полуночи МСК между входом и выходом."""
        MSK = ZoneInfo("Europe/Moscow")
        t_in  = entry_time.astimezone(MSK)
        t_out = exit_time.astimezone(MSK)
        day_in  = t_in.date()
        day_out = t_out.date()
        return (day_out - day_in).days

    @staticmethod
    def _margin_overnight_cost(borrowed: float) -> float:
        """Стоимость переноса маржинальной позиции за одну ночь (T-Bank тарифы, руб.)."""
        if borrowed <= 5_000:
            return 0.0
        elif borrowed <= 50_000:
            return 42.5
        elif borrowed <= 100_000:
            return 85.0
        elif borrowed <= 250_000:
            return 210.0
        elif borrowed <= 500_000:
            return 420.0
        elif borrowed <= 1_000_000:
            return 827.5
        elif borrowed <= 2_500_000:
            return 2_037.5
        elif borrowed <= 5_000_000:
            return 4_000.0
        else:
            return 7_775.0


def _is_near_dividend(bar_date, dividend_dates: list, days_before: int = 3) -> bool:
    """Check if bar_date is within days_before of any dividend last_buy_date."""
    from datetime import date as date_type
    for d in dividend_dates:
        if isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date_type):
            continue
        diff = (d - bar_date).days
        if 0 <= diff <= days_before:
            return True
    return False
