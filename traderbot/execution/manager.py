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
        """Открыть новую позицию."""
        # 1. Баланс
        balance = self.broker.get_portfolio_balance(self.account_id)

        # 2. Размер позиции
        qty = self.risk.position_size(balance, setup.entry_price, setup.stop_price)
        if qty < 1:
            logger.warning("[EXEC] Insufficient balance for %s, qty=0", ticker)
            return

        # 3. Определить направления ордеров для SDK
        if setup.direction == Signal.BUY:
            order_dir = OrderDirection.ORDER_DIRECTION_BUY
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
        else:
            order_dir = OrderDirection.ORDER_DIRECTION_SELL
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_BUY

        # 4. Разместить ордера
        entry_order_id = self.broker.place_limit_order(
            self.account_id, figi, qty, order_dir, setup.entry_price
        )
        sl_order_id = self.broker.place_stop_loss(
            self.account_id, figi, qty, stop_dir, setup.stop_price
        )
        tp_order_id = self.broker.place_take_profit(
            self.account_id, figi, qty, stop_dir, setup.target_price
        )

        # 5. Создать Position
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
            sl_order_id=sl_order_id,
            tp_order_id=tp_order_id,
        )

        # 6. Сохранить
        self.positions[figi] = position
        self.state.save_position(position)

        # 7. Уведомление
        msg = (
            f"\U0001f7e2 Открыта позиция {ticker} {setup.direction.value}\n"
            f"Вход: {setup.entry_price} | SL: {setup.stop_price} | TP: {setup.target_price}\n"
            f"Объём: {qty} | Причина: {setup.entry_reason}"
        )
        logger.info("[EXEC] %s", msg)
        if self.notifier:
            self.notifier.send(msg)

    def update(self, figi: str, current_price: float) -> None:
        """Обновить состояние открытой позиции (вызывается каждый цикл)."""
        if figi not in self.positions:
            return

        position = self.positions[figi]
        position.candles_held += 1
        self.state.update_candles_held(figi, position.candles_held)

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

        # Получить активные стоп-ордера на бирже
        active_stop_orders = self.broker.get_stop_orders(self.account_id)
        active_stop_ids = {so.stop_order_id for so in active_stop_orders}

        closed_figis = []
        for figi, position in self.positions.items():
            sl_active = position.sl_order_id in active_stop_ids
            tp_active = position.tp_order_id in active_stop_ids

            if sl_active and tp_active:
                logger.info("[EXEC] Recovery: %s position active, continuing", position.ticker)
                continue

            # Позиция была закрыта пока бот не работал
            if not sl_active and tp_active:
                # SL сработал
                self._close_position(figi, position.stop_price, "stop_loss")
            elif sl_active and not tp_active:
                # TP сработал
                self._close_position(figi, position.target_price, "take_profit")
            else:
                # Оба ордера отсутствуют — используем stop_price как worst case
                self._close_position(figi, position.stop_price, "stop_loss")
            closed_figis.append(figi)

        self.state.reset_stale_sl_counters()

        active_count = len(self.positions)
        logger.info("[EXEC] Recovery complete. Active positions: %d", active_count)
        if self.notifier:
            self.notifier.send(f"Бот перезапущен. Активных позиций: {active_count}")

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
