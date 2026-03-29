import logging
from datetime import date, datetime, timezone

from t_tech.invest import OrderDirection, StopOrderDirection

from traderbot.broker.tbank import TBankBroker
from traderbot.journal.writer import TradeJournal
from traderbot.notifications.telegram import TelegramNotifier
from traderbot.risk.manager import RiskManager
from traderbot.state.store import StateStore
from traderbot.types import Position, Setup, Signal, TradeRecord

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Управление жизненным циклом позиций."""

    PENDING_TIMEOUT_CANDLES = 20  # Отмена лимитки через 20 свечей 30m

    def __init__(
        self,
        broker: TBankBroker,
        risk: RiskManager,
        journal: TradeJournal,
        state: StateStore,
        notifier: TelegramNotifier | None,
        account_id: str,
        commission_pct: float,
        max_candles_timeout: int,
        max_consecutive_sl: int = 3,
    ):
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.state = state
        self.notifier = notifier
        self.account_id = account_id
        self.commission_pct = commission_pct
        self.max_candles_timeout = max_candles_timeout
        self.max_consecutive_sl = max_consecutive_sl
        # В памяти: figi → Position
        self.positions: dict[str, Position] = {}

    def has_position(self, figi: str) -> bool:
        return figi in self.positions

    def is_ticker_blocked(self, ticker: str) -> bool:
        """Проверить, заблокирован ли тикер из-за серии SL."""
        count, last_date = self.state.get_consecutive_sl(ticker)
        if last_date == date.today().isoformat() and count >= self.max_consecutive_sl:
            return True
        return False

    def open_position(self, ticker: str, figi: str, setup: Setup) -> None:
        """Выставить лимитную заявку. SL/TP будут добавлены после исполнения."""
        # 1. Баланс
        balance = self.broker.get_portfolio_balance(self.account_id)

        # 2. Размер позиции
        qty = self.risk.position_size(balance, setup.entry_price, setup.stop_price)
        if qty < 1:
            logger.warning("[EXEC] Insufficient balance for %s, qty=0", ticker)
            return

        # 3. Определить направление ордера
        if setup.direction == Signal.BUY:
            order_dir = OrderDirection.ORDER_DIRECTION_BUY
        else:
            order_dir = OrderDirection.ORDER_DIRECTION_SELL

        # 4. Разместить только лимитную заявку
        entry_order_id = self.broker.place_limit_order(
            self.account_id, figi, qty, order_dir, setup.entry_price
        )

        # 5. Создать Position в статусе pending (без SL/TP)
        position = Position(
            ticker=ticker,
            figi=figi,
            direction=setup.direction,
            entry_price=setup.entry_price,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            qty=qty,
            entry_time=datetime.now(timezone.utc),
            entry_reason=setup.entry_reason,
            entry_order_id=entry_order_id,
            status="pending",
        )

        # 6. Сохранить
        self.positions[figi] = position
        self.state.save_position(position)

        # 7. Уведомление — заявка выставлена
        msg = (
            f"\U0001f4cb Лимитная заявка {ticker} {setup.direction.value}\n"
            f"Цена: {setup.entry_price} | Объём: {qty}\n"
            f"SL: {setup.stop_price} | TP: {setup.target_price}\n"
            f"Причина: {setup.entry_reason}"
        )
        logger.info("[EXEC] %s", msg)
        if self.notifier:
            self.notifier.send(msg)

    def _activate_position(self, figi: str) -> None:
        """Лимитка исполнена — выставить SL/TP и перевести позицию в active."""
        position = self.positions[figi]

        if position.direction == Signal.BUY:
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
        else:
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_BUY

        sl_order_id = self.broker.place_stop_loss(
            self.account_id, figi, position.qty, stop_dir, position.stop_price
        )
        tp_order_id = self.broker.place_take_profit(
            self.account_id, figi, position.qty, stop_dir, position.target_price
        )

        position.sl_order_id = sl_order_id
        position.tp_order_id = tp_order_id
        position.status = "active"
        position.entry_time = datetime.now(timezone.utc)
        position.candles_held = 0
        position.last_candle_time = None
        self.state.save_position(position)

        # Уведомление — позиция открыта
        msg = (
            f"\U0001f7e2 Позиция открыта {position.ticker} {position.direction.value}\n"
            f"Вход: {position.entry_price} | SL: {position.stop_price} | TP: {position.target_price}\n"
            f"Объём: {position.qty}"
        )
        logger.info("[EXEC] %s", msg)
        if self.notifier:
            self.notifier.send(msg)

    def _cancel_pending(self, figi: str) -> None:
        """Отменить неисполненную лимитку по таймауту."""
        position = self.positions[figi]
        try:
            self.broker.cancel_order(self.account_id, position.entry_order_id)
        except Exception:
            logger.debug("[EXEC] Could not cancel entry order %s", position.entry_order_id)

        self.state.remove_position(figi)
        del self.positions[figi]

        msg = (
            f"\u274c Лимитная заявка {position.ticker} {position.direction.value} отменена\n"
            f"Цена: {position.entry_price} | Не исполнена за {position.pending_candles} свечей"
        )
        logger.info("[EXEC] %s", msg)
        if self.notifier:
            self.notifier.send(msg)

    def _is_order_filled(self, order_id: str) -> bool:
        """Проверить, исполнена ли лимитная заявка."""
        try:
            state = self.broker.get_order_state(self.account_id, order_id)
            return state.execution_report_status.name == "EXECUTION_REPORT_STATUS_FILL"
        except Exception:
            logger.debug("[EXEC] Could not get order state for %s", order_id)
            return False

    def update(self, figi: str, current_price: float, last_candle_time: datetime) -> None:
        """Обновить состояние позиции (вызывается каждый цикл).
        pending: проверить исполнение лимитки, считать таймаут ожидания.
        active: считать таймаут позиции в 30m свечах.
        """
        if figi not in self.positions:
            return

        position = self.positions[figi]
        new_candle = position.last_candle_time is None or last_candle_time > position.last_candle_time

        if position.status == "pending":
            # Проверить исполнение лимитки
            if self._is_order_filled(position.entry_order_id):
                self._activate_position(figi)
                return

            # Считать свечи ожидания
            if new_candle:
                position.pending_candles += 1
                position.last_candle_time = last_candle_time
                self.state.save_position(position)
                logger.debug("[EXEC] %s pending_candles=%d",
                             position.ticker, position.pending_candles)

            if position.pending_candles >= self.PENDING_TIMEOUT_CANDLES:
                logger.info("[EXEC] Pending timeout for %s after %d candles",
                            position.ticker, position.pending_candles)
                self._cancel_pending(figi)
            return

        # status == "active"
        if new_candle:
            position.candles_held += 1
            position.last_candle_time = last_candle_time
            self.state.update_candles_held(figi, position.candles_held, last_candle_time)
            logger.debug("[EXEC] %s candles_held=%d, last_candle=%s",
                         position.ticker, position.candles_held, last_candle_time)

        # Проверить таймаут
        if position.candles_held >= self.max_candles_timeout:
            logger.info("[EXEC] Timeout for %s after %d candles",
                         position.ticker, position.candles_held)
            self._close_position(figi, current_price, "timeout")

    def _close_position(self, figi: str, exit_price: float, reason: str) -> None:
        """Закрыть позицию."""
        position = self.positions[figi]

        # 1. Отменить оставшиеся ордера
        self._cancel_orders_safe(position)

        # 2. Рассчитать PnL
        if position.direction == Signal.BUY:
            pnl = (exit_price - position.entry_price) * position.qty
        else:
            pnl = (position.entry_price - exit_price) * position.qty

        avg_price = (position.entry_price + exit_price) / 2
        commission = 2 * self.commission_pct * avg_price * position.qty
        pnl_net = pnl - commission

        # 3. Записать в журнал
        record = TradeRecord(
            ticker=position.ticker,
            figi=position.figi,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop_price=position.stop_price,
            target_price=position.target_price,
            qty=position.qty,
            pnl=pnl_net,
            commission=commission,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc),
            entry_reason=position.entry_reason,
            exit_reason=reason,
            candles_held=position.candles_held,
        )
        self.journal.log_trade(record)

        # 4. Удалить из state и памяти
        self.state.remove_position(figi)
        del self.positions[figi]

        # 5. Обновить счётчик SL
        if reason == "stop_loss":
            self.state.increment_consecutive_sl(position.ticker)
        elif reason in ("take_profit", "timeout"):
            self.state.reset_consecutive_sl(position.ticker)

        # 6. Уведомление
        msg = (
            f"\U0001f534 Закрыта позиция {position.ticker} {position.direction.value}\n"
            f"Вход: {position.entry_price} \u2192 Выход: {exit_price}\n"
            f"P&L: {pnl_net:+.2f} RUB | Причина: {reason}\n"
            f"Длительность: {position.candles_held} свечей"
        )
        logger.info("[EXEC] %s", msg)
        if self.notifier:
            self.notifier.send(msg)

    def recover(self) -> None:
        """Восстановить позиции из state после перезапуска."""
        self.positions = self.state.get_positions()
        if not self.positions:
            logger.info("[EXEC] Recovery: no saved positions")
            self.state.reset_stale_sl_counters()
            return

        for figi, position in list(self.positions.items()):
            if position.status == "pending":
                # Проверить, исполнилась ли лимитка пока бот был выключен
                if self._is_order_filled(position.entry_order_id):
                    logger.info("[EXEC] Recovery: %s pending order filled, activating", position.ticker)
                    self._activate_position(figi)
                else:
                    logger.info("[EXEC] Recovery: %s pending order still waiting", position.ticker)
                continue

            # status == "active"
            active_stop_orders = self.broker.get_stop_orders(self.account_id)
            active_stop_ids = {so.stop_order_id for so in active_stop_orders}

            sl_active = position.sl_order_id in active_stop_ids
            tp_active = position.tp_order_id in active_stop_ids

            if sl_active and tp_active:
                logger.info("[EXEC] Recovery: %s position active, continuing", position.ticker)
                continue

            # Позиция была закрыта пока бот не работал
            if not sl_active and tp_active:
                self._close_position(figi, position.stop_price, "stop_loss")
            elif sl_active and not tp_active:
                self._close_position(figi, position.target_price, "take_profit")
            else:
                self._close_position(figi, position.stop_price, "stop_loss")

        self.state.reset_stale_sl_counters()

        pending_count = sum(1 for p in self.positions.values() if p.status == "pending")
        active_count = sum(1 for p in self.positions.values() if p.status == "active")
        logger.info("[EXEC] Recovery complete. Active: %d, Pending: %d", active_count, pending_count)
        if self.notifier:
            self.notifier.send(f"Бот перезапущен. Активных: {active_count}, ожидающих: {pending_count}")

    def _cancel_orders_safe(self, position: Position) -> None:
        """Безопасно отменить все ордера позиции (игнорировать ошибки)."""
        for order_id in [position.entry_order_id]:
            try:
                self.broker.cancel_order(self.account_id, order_id)
            except Exception:
                logger.debug("[EXEC] Could not cancel order %s (may be filled/expired)", order_id)

        for stop_id in [position.sl_order_id, position.tp_order_id]:
            try:
                self.broker.cancel_stop_order(self.account_id, stop_id)
            except Exception:
                logger.debug("[EXEC] Could not cancel stop order %s (may be filled/expired)", stop_id)
