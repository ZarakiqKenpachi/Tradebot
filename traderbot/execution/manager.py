import logging
from datetime import date, datetime, timezone

from t_tech.invest import OrderDirection, StopOrderDirection

from traderbot.broker.tbank import TBankBroker, round_to_step
from traderbot.notifications.telegram import TelegramNotifier
from traderbot.risk.manager import RiskManager
from traderbot.types import Position, Setup, Signal, TradeRecord

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Управление жизненным циклом позиций."""

    PENDING_TIMEOUT_CANDLES = 20  # Отмена лимитки через 20 свечей 30m

    def __init__(
        self,
        broker: TBankBroker,
        risk: RiskManager,
        journal,
        state,
        notifier: TelegramNotifier | None,
        account_id: str,
        commission_pct: float,
        max_candles_timeout: int,
        max_consecutive_sl: int = 3,
        client_id: int = 0,
        is_admin: bool = False,
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
        self.client_id = client_id
        self.is_admin = is_admin
        # В памяти: figi → Position
        self.positions: dict[str, Position] = {}
        # Флаг: клиент отозван, но позиции ещё сопровождаются до SL/TP
        self._revoked: bool = False

    def has_position(self, figi: str) -> bool:
        return figi in self.positions

    def is_ticker_blocked(self, ticker: str) -> bool:
        """Проверить, заблокирован ли тикер из-за серии SL."""
        count, last_date = self.state.get_consecutive_sl(ticker)
        if last_date == date.today().isoformat() and count >= self.max_consecutive_sl:
            return True
        return False

    # ------------------------------------------------------------------
    # Уведомления
    # ------------------------------------------------------------------

    def _notify_trade(self, msg: str, client_msg: str | None = None) -> None:
        """Отправить уведомление о торговом событии.

        - Подробное сообщение уходит ВСЕМ администраторам через send_admin.
        - Для подписчиков: отдельное краткое сообщение (client_msg).
          Если client_msg=None — подписчик НЕ получает уведомление (лимитки и т.п.).
        """
        if not self.notifier:
            return
        admin_msg = msg if self.is_admin else f"[Клиент {self.client_id}]\n{msg}"
        self.notifier.send_admin(admin_msg)
        if not self.is_admin and client_msg is not None:
            self.notifier.send_to_client(self.client_id, client_msg)

    def open_position(self, ticker: str, figi: str, setup: Setup) -> None:
        """Выставить лимитную заявку. SL/TP будут добавлены после исполнения."""
        # 1. Баланс, размер лота и шаг цены
        balance = self.broker.get_portfolio_balance(self.account_id)
        lot_size, price_step = self.broker.get_instrument_info(figi)
        if lot_size < 1:
            logger.error("[EXEC] Invalid lot_size=%d for %s from API, aborting", lot_size, ticker)
            return
        logger.info("[EXEC] %s lot_size=%d price_step=%.6f", ticker, lot_size, price_step)

        # 2. Округлить цены до шага цены инструмента
        entry_price = round_to_step(setup.entry_price, price_step)
        stop_price = round_to_step(setup.stop_price, price_step)
        target_price = round_to_step(setup.target_price, price_step)

        # 3. Размер позиции в лотах
        qty = self.risk.position_size(balance, entry_price, stop_price, lot_size)
        if qty < 1:
            logger.warning("[EXEC] Insufficient balance for %s, qty=0", ticker)
            return

        # 4. Определить направление ордера
        if setup.direction == Signal.BUY:
            order_dir = OrderDirection.ORDER_DIRECTION_BUY
        else:
            order_dir = OrderDirection.ORDER_DIRECTION_SELL

        # 5. Разместить только лимитную заявку
        entry_order_id = self.broker.place_limit_order(
            self.account_id, figi, qty, order_dir, entry_price
        )

        # 6. Создать Position в статусе pending (без SL/TP)
        position = Position(
            ticker=ticker,
            figi=figi,
            direction=setup.direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            qty=qty,
            lot_size=lot_size,
            entry_time=datetime.now(timezone.utc),
            entry_reason=setup.entry_reason,
            entry_order_id=entry_order_id,
            status="pending",
        )

        # 7. Сохранить
        self.positions[figi] = position
        self.state.save_position(position)

        # 8. Уведомление — заявка выставлена
        msg = (
            f"\U0001f4cb Лимитная заявка {ticker} {setup.direction.value}\n"
            f"Цена: {entry_price} | Объём: {qty}\n"
            f"SL: {stop_price} | TP: {target_price}\n"
            f"Причина: {setup.entry_reason}"
        )
        logger.info("[EXEC] %s", msg)
        self._notify_trade(msg)  # подписчик не видит выставление лимитки

    def _get_real_exit_price(self, figi: str, fallback_price: float) -> float:
        """Получить реальную цену из последней сделки по инструменту.

        Стоп-ордер исполняется маркет-ордером, цена может отличаться от заданной.
        Если не удаётся получить — возвращает fallback.
        """
        try:
            price = self.broker.get_last_price(figi)
            if price is not None:
                return price
        except Exception:
            logger.debug("[EXEC] Could not get last price for %s, using fallback", figi)
        return fallback_price

    def _get_portfolio_qty(self, figi: str) -> int | None:
        """Получить реальное количество бумаги в портфеле. None при ошибке."""
        try:
            securities = self.broker.get_positions(self.account_id)
            for sec in securities:
                if sec.figi == figi:
                    return int(sec.balance)
            return 0
        except Exception:
            logger.exception("[EXEC] Failed to get portfolio qty for figi=%s", figi)
            return None

    def _has_portfolio_position(self, figi: str) -> bool:
        """Проверить, есть ли бумага в портфеле на счёте."""
        try:
            securities = self.broker.get_positions(self.account_id)
            for sec in securities:
                if sec.figi == figi and int(sec.balance) != 0:
                    return True
        except Exception:
            logger.exception("[EXEC] Failed to check portfolio for figi=%s", figi)
        return False

    def _activate_position(self, figi: str) -> None:
        """Лимитка исполнена — выставить SL/TP и перевести позицию в active."""
        position = self.positions[figi]

        # Проверить наличие бумаги в портфеле перед выставлением SL/TP
        if not self._has_portfolio_position(figi):
            logger.warning(
                "[EXEC] %s: no position in portfolio, skipping SL/TP placement",
                position.ticker,
            )
            self.state.remove_position(figi)
            del self.positions[figi]
            if self.notifier:
                self.notifier.send_admin(
                    f"⚠️ {position.ticker}: лимитка исполнена, но бумаги нет в портфеле.\n"
                    f"Позиция удалена из state. Проверьте счёт вручную."
                )
            return

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
            f"Объём: {position.qty}\n"
            f"Причина: {position.entry_reason}"
        )
        logger.info("[EXEC] %s", msg)
        self._notify_trade(msg, f"\U0001f7e2 Открыта новая позиция {position.ticker}")

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
        self._notify_trade(msg)  # подписчик не видит отмену лимитки

    def _is_order_filled(self, order_id: str) -> bool:
        """Проверить, исполнена ли лимитная заявка."""
        try:
            state = self.broker.get_order_state(self.account_id, order_id)
            return state.execution_report_status.name == "EXECUTION_REPORT_STATUS_FILL"
        except Exception:
            logger.debug("[EXEC] Could not get order state for %s", order_id)
            return False

    def _is_order_cancelled(self, order_id: str) -> bool:
        """Проверить, отменена ли лимитная заявка (вручную или биржей)."""
        try:
            state = self.broker.get_order_state(self.account_id, order_id)
            return state.execution_report_status.name in (
                "EXECUTION_REPORT_STATUS_CANCELLED",
                "EXECUTION_REPORT_STATUS_REJECTED",
            )
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

            # Проверить, не отменена ли заявка вручную через терминал
            if self._is_order_cancelled(position.entry_order_id):
                logger.info("[EXEC] %s pending order was cancelled externally, removing position",
                            position.ticker)
                self.state.remove_position(figi)
                del self.positions[figi]
                msg = (
                    f"\u274c Лимитная заявка {position.ticker} {position.direction.value} "
                    f"отменена через терминал\nЦена: {position.entry_price}"
                )
                logger.info("[EXEC] %s", msg)
                self._notify_trade(msg)  # подписчик не видит отмену лимитки
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
        # Проверить, не закрылась ли позиция по SL/TP на бирже
        active_stop_orders = self.broker.get_stop_orders(self.account_id)
        active_stop_ids = {so.stop_order_id for so in active_stop_orders}
        sl_active = position.sl_order_id in active_stop_ids
        tp_active = position.tp_order_id in active_stop_ids

        if not sl_active and not tp_active:
            # Оба ордера исчезли — позиция закрыта биржей (SL сработал, TP отменился)
            exit_price = self._get_real_exit_price(figi, position.stop_price)
            logger.info("[EXEC] %s: both SL/TP gone, assuming stop_loss, real price=%.2f",
                        position.ticker, exit_price)
            self._close_position(figi, exit_price, "stop_loss")
            return
        if not tp_active and sl_active:
            # TP исчез, SL ещё жив — значит TP сработал
            exit_price = self._get_real_exit_price(figi, position.target_price)
            logger.info("[EXEC] %s: TP filled (SL still active), real price=%.2f",
                        position.ticker, exit_price)
            self._close_position(figi, exit_price, "take_profit")
            return
        if not sl_active and tp_active:
            # SL исчез, TP ещё жив — значит SL сработал
            exit_price = self._get_real_exit_price(figi, position.stop_price)
            logger.info("[EXEC] %s: SL filled (TP still active), real price=%.2f",
                        position.ticker, exit_price)
            self._close_position(figi, exit_price, "stop_loss")
            return

        if new_candle:
            position.candles_held += 1
            position.last_candle_time = last_candle_time
            self.state.update_candles_held(figi, position.candles_held, last_candle_time)
            logger.debug("[EXEC] %s candles_held=%d, last_candle=%s",
                         position.ticker, position.candles_held, last_candle_time)

        # Проверить таймаут
        if position.candles_held >= self.max_candles_timeout:
            # Перед закрытием убедиться, что бумага ещё в портфеле
            real_qty = self._get_portfolio_qty(figi)
            if real_qty is not None and real_qty == 0:
                logger.warning("[EXEC] %s: timeout but no position in portfolio, cleaning state",
                               position.ticker)
                self._cancel_orders_safe(position)
                exit_price = self._get_real_exit_price(figi, current_price)
                self._close_position(figi, exit_price, "stop_loss")
                return
            logger.info("[EXEC] Timeout for %s after %d candles",
                         position.ticker, position.candles_held)
            self._close_position(figi, current_price, "timeout")

    def _close_position(self, figi: str, exit_price: float, reason: str) -> None:
        """Закрыть позицию."""
        position = self.positions[figi]

        # 1a. Позиция на бирже всё ещё открыта — закрыть по рынку.
        # Для stop_loss/take_profit её уже закрыл сам стоп-ордер.
        # Для timeout и revoked нужно выставить рыночный ордер вручную.
        if reason in ("timeout", "revoked"):
            exit_dir = (OrderDirection.ORDER_DIRECTION_SELL
                        if position.direction == Signal.BUY
                        else OrderDirection.ORDER_DIRECTION_BUY)
            try:
                self.broker.place_market_order(
                    self.account_id, figi, position.qty, exit_dir
                )
                logger.info("[EXEC] %s: market exit placed for timeout", position.ticker)
            except Exception:
                logger.exception(
                    "[EXEC] %s: FAILED to place market exit on timeout — position still open!",
                    position.ticker,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"\u26a0\ufe0f Не удалось закрыть {position.ticker} по рынку "
                        f"при таймауте. Попытаюсь снова на следующем цикле."
                    )
                # Не удаляем позицию из state — будет повторная попытка.
                return

        # 1b. Отменить оставшиеся ордера
        self._cancel_orders_safe(position)

        # 2. Рассчитать PnL (qty — в лотах, умножаем на lot_size для перевода в акции)
        shares = position.qty * position.lot_size
        if position.direction == Signal.BUY:
            pnl = (exit_price - position.entry_price) * shares
        else:
            pnl = (position.entry_price - exit_price) * shares

        avg_price = (position.entry_price + exit_price) / 2
        commission = 2 * self.commission_pct * avg_price * shares
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
        _reason_label = {
            "stop_loss": "стоп-лосс",
            "take_profit": "тейк-профит",
            "timeout": "по времени",
            "revoked": "принудительно",
        }
        msg = (
            f"\U0001f534 Закрыта позиция {position.ticker} {position.direction.value}\n"
            f"Вход: {position.entry_price} \u2192 Выход: {exit_price}\n"
            f"P&L: {pnl_net:+.2f} RUB | Причина закрытия: {_reason_label.get(reason, reason)}\n"
            f"Причина входа: {position.entry_reason}\n"
            f"Длительность: {position.candles_held} свечей"
        )
        logger.info("[EXEC] %s", msg)
        client_msg = (
            f"\U0001f534 Позиция {position.ticker} закрыта | P&L: {pnl_net:+.2f} ₽"
        )
        self._notify_trade(msg, client_msg)

    def reconcile_with_broker(self, known_tickers_by_figi: dict[str, str]) -> None:
        """Сверить фактические позиции на счёте со state бота.

        Если на счёте есть бумага из конфигурации, которую бот не отслеживает
        (например, после сбоя закрытия по таймауту), — закрыть её по рынку
        и уведомить админа.
        """
        try:
            securities = self.broker.get_positions(self.account_id)
        except Exception:
            logger.exception("[EXEC] Reconcile: failed to fetch broker positions")
            return

        tracked_figis = set(self.positions.keys())
        orphans: list[tuple[str, str, int]] = []
        for sec in securities:
            figi = sec.figi
            balance = int(sec.balance)
            if balance == 0:
                continue
            if figi not in known_tickers_by_figi:
                continue
            if figi in tracked_figis:
                continue
            orphans.append((known_tickers_by_figi[figi], figi, balance))

        if not orphans:
            logger.info("[EXEC] Reconcile: OK (tracked=%d)", len(tracked_figis))
            return

        for ticker, figi, balance in orphans:
            side = "LONG" if balance > 0 else "SHORT"
            abs_balance = abs(balance)
            try:
                lot_size, _ = self.broker.get_instrument_info(figi)
            except Exception:
                logger.exception("[EXEC] Reconcile: failed lot_size for %s", ticker)
                if self.notifier:
                    self.notifier.send_admin(
                        f"\u26a0\ufe0f Сирота {ticker} {side} {abs_balance} шт: "
                        f"не удалось получить lot_size, закройте вручную."
                    )
                continue

            if lot_size < 1 or abs_balance % lot_size != 0:
                logger.error(
                    "[EXEC] Reconcile: %s balance %d not multiple of lot_size %d",
                    ticker, abs_balance, lot_size,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"\u26a0\ufe0f Сирота {ticker} {side} {abs_balance} шт: "
                        f"нечётный баланс (lot_size={lot_size}), закройте вручную."
                    )
                continue

            qty_lots = abs_balance // lot_size
            exit_dir = (OrderDirection.ORDER_DIRECTION_SELL if balance > 0
                        else OrderDirection.ORDER_DIRECTION_BUY)
            try:
                self.broker.place_market_order(
                    self.account_id, figi, qty_lots, exit_dir
                )
                logger.warning(
                    "[EXEC] Orphan closed by market: %s %s %d shares (%d lots)",
                    ticker, side, abs_balance, qty_lots,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"\u26a0\ufe0f Закрыта неотслеживаемая позиция {ticker} "
                        f"{side} {abs_balance} шт. по рынку."
                    )
            except Exception:
                logger.exception("[EXEC] Reconcile: failed to close orphan %s", ticker)
                if self.notifier:
                    self.notifier.send_admin(
                        f"\u26a0\ufe0f Не удалось закрыть сироту {ticker} {side} "
                        f"{abs_balance} шт. — закройте вручную!"
                    )

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
                    try:
                        self._activate_position(figi)
                    except Exception as e:
                        logger.error("[EXEC] Recovery: failed to activate %s: %s", position.ticker, e)
                        self.state.remove_position(figi)
                        del self.positions[figi]
                        if self.notifier:
                            self.notifier.send_admin(
                                f"⚠️ Не удалось выставить SL/TP для {position.ticker} "
                                f"при восстановлении (цена за пределами лимитов).\n"
                                f"SL: {position.stop_price} | TP: {position.target_price}\n"
                                f"Закройте позицию вручную!"
                            )
                elif self._is_order_cancelled(position.entry_order_id):
                    logger.info("[EXEC] Recovery: %s pending order was cancelled externally, removing",
                                position.ticker)
                    self.state.remove_position(figi)
                    del self.positions[figi]
                    if self.notifier and self.is_admin:
                        self.notifier.send_to_client(
                            self.client_id,
                            f"\u274c Лимитная заявка {position.ticker} {position.direction.value} "
                            f"была отменена через терминал\nЦена: {position.entry_price}"
                        )
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

            # Позиция была закрыта пока бот не работал — получить реальную цену
            if not sl_active and tp_active:
                exit_price = self._get_real_exit_price(figi, position.stop_price)
                self._close_position(figi, exit_price, "stop_loss")
            elif sl_active and not tp_active:
                exit_price = self._get_real_exit_price(figi, position.target_price)
                self._close_position(figi, exit_price, "take_profit")
            else:
                exit_price = self._get_real_exit_price(figi, position.stop_price)
                self._close_position(figi, exit_price, "stop_loss")

        self.state.reset_stale_sl_counters()

        pending_count = sum(1 for p in self.positions.values() if p.status == "pending")
        active_count = sum(1 for p in self.positions.values() if p.status == "active")
        logger.info("[EXEC] Recovery complete. Active: %d, Pending: %d", active_count, pending_count)
        if self.notifier:
            self.notifier.send_admin(
                f"Бот перезапущен. Активных: {active_count}, ожидающих: {pending_count}"
            )

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
