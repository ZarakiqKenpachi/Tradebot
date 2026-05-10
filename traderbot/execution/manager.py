import logging
import time as _time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from t_tech.invest import OrderDirection, StopOrderDirection
from t_tech.invest.exceptions import RequestError

from traderbot.broker.tbank import TBankBroker, round_to_step
from traderbot.notifications.telegram import TelegramNotifier
from traderbot.risk.manager import RiskManager
from traderbot.types import Position, Setup, Signal, TradeRecord

logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")


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
        max_daily_sl: int = 5,
        max_open_positions: int = 4,
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
        self.max_daily_sl = max_daily_sl
        self.max_open_positions = max_open_positions
        self.client_id = client_id
        self.is_admin = is_admin
        # В памяти: figi → Position
        self.positions: dict[str, Position] = {}
        # Флаг: клиент отозван, но позиции ещё сопровождаются до SL/TP
        self._revoked: bool = False
        # Флаг: достигнут дневной лимит стоп-лоссов; main.py поставит клиента на паузу
        self._daily_sl_limit_reached: bool = False
        # Тикер, по которому сработал дневной лимит SL
        self._daily_sl_limit_ticker: str | None = None
        # Кэш стоп-ордеров (одинаков для всех тикеров одного клиента за цикл)
        self._stop_orders_cache: set[str] = set()
        self._stop_orders_cache_time: float = 0.0
        # Тикеры, о блокировке которых уже уведомлён админ (ticker, date)
        self._blocked_notified: set[tuple[str, str]] = set()
        # Буфер торговых событий для консолидации уведомлений
        self._trade_events: list[dict] = []

    def has_position(self, figi: str) -> bool:
        return figi in self.positions

    def is_ticker_blocked(self, ticker: str) -> bool:
        """Проверить, заблокирован ли тикер из-за серии SL."""
        today_msk = datetime.now(_MSK).date().isoformat()
        count, last_date = self.state.get_consecutive_sl(ticker)
        if last_date == today_msk and count >= self.max_consecutive_sl:
            return True
        return False

    def notify_ticker_blocked(self, ticker: str) -> None:
        """Уведомить о блокировке тикера (однократно за день)."""
        today_msk = datetime.now(_MSK).date().isoformat()
        key = (ticker, today_msk)
        if key in self._blocked_notified:
            return
        self._blocked_notified.add(key)
        count, _ = self.state.get_consecutive_sl(ticker)
        logger.warning(
            "[EXEC] Client %d: ticker %s blocked after %d consecutive SLs",
            self.client_id, ticker, count,
        )
        if self.notifier:
            admin_msg = (
                f"🚫 {ticker} заблокирован для клиента {self.client_id}: "
                f"{count} стоп-лоссов подряд (лимит {self.max_consecutive_sl})."
            )
            if not self.is_admin:
                self.notifier.send_admin(admin_msg)
            else:
                self.notifier.send_admin(admin_msg)

    # ------------------------------------------------------------------
    # Дивиденды
    # ------------------------------------------------------------------

    def _is_dividend_blocked(self, ticker: str, figi: str) -> bool:
        """Проверить, нельзя ли шортить из-за близкой дивидендной отсечки."""
        try:
            dividends = self.broker.get_dividends(figi, days_ahead=10)
        except Exception:
            logger.debug("[EXEC] Could not fetch dividends for %s", ticker)
            return False

        if not dividends:
            return False

        today = datetime.now(_MSK).date()
        for div in dividends:
            last_buy = div["last_buy_date"]
            if last_buy is None:
                continue
            last_buy_date = last_buy.date() if hasattr(last_buy, "date") else last_buy
            # Блокируем шорт за 3 дня до last_buy_date включительно
            if today <= last_buy_date and (last_buy_date - today).days <= 3:
                logger.info(
                    "[EXEC] %s SELL blocked: dividend cutoff %s (last_buy=%s, div=%.2f₽)",
                    ticker, div["record_date"], last_buy_date, div["dividend_net"],
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Уведомления
    # ------------------------------------------------------------------

    def _notify_trade(self, msg: str, client_msg: str | None = None) -> None:
        """Отправить уведомление о торговом событии (напрямую, без консолидации).

        Используется для warning-сообщений и recovery/reconcile.
        Для торговых событий в основном цикле используйте _emit_trade_event.
        """
        if not self.notifier:
            return
        admin_msg = msg if self.is_admin else f"[Клиент {self.client_id}]\n{msg}"
        self.notifier.send_admin(admin_msg)
        if not self.is_admin and client_msg is not None:
            self.notifier.send_to_client(self.client_id, client_msg)

    def _emit_trade_event(self, event: dict, client_msg: str | None = None) -> None:
        """Буферизировать торговое событие для консолидации.

        client_msg отправляется подписчику немедленно.
        Админ-уведомление формируется позже в _consolidate_and_send.
        """
        event["client_id"] = self.client_id
        self._trade_events.append(event)
        if not self.is_admin and client_msg is not None and self.notifier:
            self.notifier.send_to_client(self.client_id, client_msg)

    def drain_trade_events(self) -> list[dict]:
        """Вернуть и очистить буфер торговых событий."""
        events = self._trade_events
        self._trade_events = []
        return events

    def _determine_close_reason(self, position: Position, exit_price: float) -> str:
        """Определить причину закрытия по расстоянию от цены до SL/TP."""
        dist_to_sl = abs(exit_price - position.stop_price)
        dist_to_tp = abs(exit_price - position.target_price)
        return "take_profit" if dist_to_tp < dist_to_sl else "stop_loss"

    def open_position(self, ticker: str, figi: str, setup: Setup) -> bool:
        """Выставить лимитную заявку. SL/TP будут добавлены после исполнения.

        Returns True if a limit order was successfully placed.
        """
        # 0a. Лимит одновременных позиций
        if len(self.positions) >= self.max_open_positions:
            logger.info("[EXEC] %s: max open positions (%d) reached, skipping",
                        ticker, self.max_open_positions)
            return False

        # 0b. Dividend filter: нельзя шортить перед дивидендной отсечкой
        if setup.direction == Signal.SELL:
            if self._is_dividend_blocked(ticker, figi):
                return False

        # 1. Баланс, размер лота и шаг цены
        balance = self.broker.get_portfolio_balance(self.account_id)
        lot_size, price_step = self.broker.get_instrument_info(figi)
        if lot_size < 1:
            logger.error("[EXEC] Invalid lot_size=%d for %s from API, aborting", lot_size, ticker)
            return False
        logger.info("[EXEC] %s lot_size=%d price_step=%.6f", ticker, lot_size, price_step)

        # 2. Округлить цены до шага цены инструмента
        entry_price = round_to_step(setup.entry_price, price_step)
        stop_price = round_to_step(setup.stop_price, price_step)
        target_price = round_to_step(setup.target_price, price_step)

        # 2a. Limit order price validation:
        #     BUY limit cannot be above market, SELL limit cannot be below market
        try:
            market_price = self.broker.get_last_price(figi)
        except Exception:
            market_price = None
        if market_price is not None:
            if setup.direction == Signal.BUY and entry_price > market_price:
                logger.info(
                    "[EXEC] %s BUY limit %.2f > market %.2f → skipped",
                    ticker, entry_price, market_price,
                )
                return False
            elif setup.direction == Signal.SELL and entry_price < market_price:
                logger.info(
                    "[EXEC] %s SELL limit %.2f < market %.2f → skipped",
                    ticker, entry_price, market_price,
                )
                return False

        # 3. Размер позиции в лотах
        qty = self.risk.position_size(balance, entry_price, stop_price, lot_size)
        if qty < 1:
            logger.warning("[EXEC] Insufficient balance for %s, qty=0", ticker)
            return False

        # 4. Определить направление ордера
        if setup.direction == Signal.BUY:
            order_dir = OrderDirection.ORDER_DIRECTION_BUY
        else:
            order_dir = OrderDirection.ORDER_DIRECTION_SELL

        # 5. Разместить только лимитную заявку
        try:
            entry_order_id = self.broker.place_limit_order(
                self.account_id, figi, qty, order_dir, entry_price
            )
        except RequestError as e:
            if "30042" in str(e):
                logger.warning("[EXEC] %s: insufficient funds (30042), skipping", ticker)
                return False
            raise

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

        # 8. Уведомление — заявка выставлена (буфер для консолидации)
        logger.info(
            "[EXEC] Limit order %s %s price=%.2f qty=%d SL=%.2f TP=%.2f",
            ticker, setup.direction.value, entry_price, qty, stop_price, target_price,
        )
        self._emit_trade_event({
            "type": "limit_placed",
            "ticker": ticker,
            "direction": setup.direction.value,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "qty": qty,
            "entry_reason": setup.entry_reason,
        })  # подписчик не видит выставление лимитки
        return True

    def _get_real_exit_price(self, figi: str, fallback_price: float) -> float:
        """Получить реальную цену закрытия из истории операций.

        Сначала ищет фактическую цену исполнения в операциях T-Bank,
        если не найдено — последнюю рыночную цену, иначе — fallback.
        """
        try:
            price = self.broker.get_executed_price(self.account_id, figi)
            if price is not None:
                return price
        except Exception:
            logger.debug("[EXEC] get_executed_price failed for %s", figi)
        try:
            price = self.broker.get_last_price(figi)
            if price is not None:
                return price
        except Exception:
            logger.debug("[EXEC] get_last_price failed for %s, using fallback", figi)
        return fallback_price

    def _get_stop_orders_cached(self) -> set[str]:
        """Получить ID активных стоп-ордеров (с кэшем на 30 сек)."""
        now = _time.time()
        if self._stop_orders_cache_time and now - self._stop_orders_cache_time < 30:
            return self._stop_orders_cache
        try:
            orders = self.broker.get_stop_orders(self.account_id)
            self._stop_orders_cache = {so.stop_order_id for so in orders}
        except Exception:
            logger.exception("[EXEC] Failed to get stop orders for cache")
            # Вернуть старый кэш, если есть
        self._stop_orders_cache_time = now
        return self._stop_orders_cache

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

        # Уведомление — позиция открыта (буфер для консолидации)
        logger.info(
            "[EXEC] Position opened %s %s entry=%.2f SL=%.2f TP=%.2f qty=%d",
            position.ticker, position.direction.value, position.entry_price,
            position.stop_price, position.target_price, position.qty,
        )
        self._emit_trade_event(
            {
                "type": "position_opened",
                "ticker": position.ticker,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "qty": position.qty,
                "entry_reason": position.entry_reason,
            },
            f"\U0001f7e2 Открыта новая позиция {position.ticker}",
        )

    def _cancel_pending(self, figi: str) -> None:
        """Отменить неисполненную лимитку по таймауту."""
        position = self.positions[figi]
        try:
            self.broker.cancel_order(self.account_id, position.entry_order_id)
        except Exception:
            logger.debug("[EXEC] Could not cancel entry order %s", position.entry_order_id)
            # Race condition: лимитка могла исполниться между проверкой и отменой
            if self._is_order_filled(position.entry_order_id):
                logger.info("[EXEC] %s: order filled during cancel — activating", position.ticker)
                try:
                    self._activate_position(figi)
                except Exception as e:
                    logger.error("[EXEC] %s: failed to activate after race: %s", position.ticker, e)
                    self.state.remove_position(figi)
                    del self.positions[figi]
                    if self.notifier:
                        self.notifier.send_admin(
                            f"⚠️ {position.ticker}: лимитка исполнилась в момент отмены, "
                            f"но не удалось выставить SL/TP. Закройте вручную!"
                        )
                return

        self.state.remove_position(figi)
        del self.positions[figi]

        logger.info(
            "[EXEC] Limit cancelled %s %s timeout after %d candles",
            position.ticker, position.direction.value, position.pending_candles,
        )
        self._emit_trade_event({
            "type": "limit_cancelled_timeout",
            "ticker": position.ticker,
            "direction": position.direction.value,
            "entry_price": position.entry_price,
            "pending_candles": position.pending_candles,
        })  # подписчик не видит отмену лимитки

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

            # Проверить, не отменена ли заявка (вручную или биржей)
            if self._is_order_cancelled(position.entry_order_id):
                logger.info("[EXEC] %s pending order was cancelled externally, removing position",
                            position.ticker)
                self.state.remove_position(figi)
                del self.positions[figi]
                self._emit_trade_event({
                    "type": "limit_cancelled_external",
                    "ticker": position.ticker,
                    "direction": position.direction.value,
                    "entry_price": position.entry_price,
                })
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
        active_stop_ids = self._get_stop_orders_cached()
        sl_active = position.sl_order_id in active_stop_ids
        tp_active = position.tp_order_id in active_stop_ids

        if not sl_active and not tp_active:
            # Оба ордера исчезли — проверяем портфель перед закрытием
            if self._has_portfolio_position(figi):
                # Позиция ещё на счёте, но стоп-ордеров нет — отменены вручную
                logger.warning(
                    "[EXEC] %s: both SL/TP gone but position still in portfolio "
                    "— stop orders may have been cancelled manually",
                    position.ticker,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"⚠️ {position.ticker}: оба стоп-ордера исчезли, "
                        f"но позиция ещё в портфеле.\n"
                        f"Возможно, стоп-ордера отменены вручную через терминал.\n"
                        f"SL: {position.stop_price} | TP: {position.target_price}"
                    )
                return  # Не закрываем state — ждём ручного вмешательства
            exit_price = self._get_real_exit_price(figi, position.stop_price)
            reason = self._determine_close_reason(position, exit_price)
            logger.info("[EXEC] %s: both SL/TP gone, portfolio confirms closed, "
                        "%s, price=%.2f", position.ticker, reason, exit_price)
            self._close_position(figi, exit_price, reason)
            return
        if not tp_active and sl_active:
            # TP исчез, SL ещё жив — проверяем портфель
            if self._has_portfolio_position(figi):
                logger.warning(
                    "[EXEC] %s: TP gone but position still in portfolio — unexpected",
                    position.ticker,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"⚠️ {position.ticker}: тейк-профит исчез, "
                        f"но позиция ещё в портфеле. Проверьте счёт."
                    )
                return
            exit_price = self._get_real_exit_price(figi, position.target_price)
            logger.info("[EXEC] %s: TP filled, portfolio confirmed, price=%.2f",
                        position.ticker, exit_price)
            self._close_position(figi, exit_price, "take_profit")
            return
        if not sl_active and tp_active:
            # SL исчез, TP ещё жив — проверяем портфель
            if self._has_portfolio_position(figi):
                logger.warning(
                    "[EXEC] %s: SL gone but position still in portfolio — unexpected",
                    position.ticker,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"⚠️ {position.ticker}: стоп-лосс исчез, "
                        f"но позиция ещё в портфеле. Проверьте счёт."
                    )
                return
            exit_price = self._get_real_exit_price(figi, position.stop_price)
            logger.info("[EXEC] %s: SL filled, portfolio confirmed, price=%.2f",
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
                reason = self._determine_close_reason(position, exit_price)
                self._close_position(figi, exit_price, reason)
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
                order_id = self.broker.place_market_order(
                    self.account_id, figi, position.qty, exit_dir
                )
                # Получить реальную цену исполнения рыночного ордера
                fill_price = self.broker.get_order_fill_price(self.account_id, order_id)
                if fill_price is None:
                    fill_price = self.broker.get_executed_price(self.account_id, figi)
                if fill_price is not None:
                    exit_price = fill_price
                logger.info("[EXEC] %s: market exit placed, fill_price=%.2f",
                            position.ticker, exit_price)
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

        # 1b. "deleted": клиент удалён — оставляем SL/TP на бирже, только чистим state
        if reason == "deleted":
            self.state.remove_position(figi)
            del self.positions[figi]
            logger.info(
                "[EXEC] %s: client deleted, position left on exchange "
                "(SL: %s, TP: %s)",
                position.ticker, position.stop_price, position.target_price,
            )
            if self.notifier:
                self.notifier.send_admin(
                    f"ℹ️ {position.ticker} {position.direction.value}: позиция оставлена "
                    f"на счёте со стоп-ордерами "
                    f"(SL: {position.stop_price}, TP: {position.target_price})"
                )
            return

        # 1c. Отменить оставшиеся ордера (stop_loss, take_profit)
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
            daily_sl = self.state.get_daily_sl_count_for_ticker(position.ticker)
            if daily_sl >= self.max_daily_sl:
                self._daily_sl_limit_reached = True
                self._daily_sl_limit_ticker = position.ticker
                logger.warning(
                    "[EXEC] Client %d: daily SL limit reached for %s (%d/%d), signalling pause",
                    self.client_id, position.ticker, daily_sl, self.max_daily_sl,
                )
        elif reason in ("take_profit", "timeout"):
            self.state.reset_consecutive_sl(position.ticker)

        # 6. Уведомление (буфер для консолидации)
        logger.info(
            "[EXEC] Closed %s %s entry=%.2f exit=%.2f pnl=%.2f reason=%s",
            position.ticker, position.direction.value, position.entry_price,
            exit_price, pnl_net, reason,
        )
        client_msg = (
            f"\U0001f534 Позиция {position.ticker} закрыта | P&L: {pnl_net:+.2f} ₽"
        )
        self._emit_trade_event(
            {
                "type": "position_closed",
                "ticker": position.ticker,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "pnl": pnl_net,
                "close_reason": reason,
                "entry_reason": position.entry_reason,
                "candles_held": position.candles_held,
            },
            client_msg,
        )

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

        # Обратная проверка: позиции, которые бот отслеживает, но их нет в портфеле
        broker_figis = {sec.figi for sec in securities if int(sec.balance) != 0}
        active_stop_ids = self._get_stop_orders_cached()
        for figi, position in list(self.positions.items()):
            if position.status != "active":
                continue
            if figi not in broker_figis:
                # Определить причину: SL/TP сработал или закрыта вручную
                sl_gone = position.sl_order_id not in active_stop_ids
                tp_gone = position.tp_order_id not in active_stop_ids
                if sl_gone and not tp_gone:
                    reason = "stop_loss"
                    cause = "сработал стоп-лосс"
                elif tp_gone and not sl_gone:
                    reason = "take_profit"
                    cause = "сработал тейк-профит"
                elif sl_gone and tp_gone:
                    exit_price_tmp = self._get_real_exit_price(figi, position.stop_price)
                    reason = self._determine_close_reason(position, exit_price_tmp)
                    cause = f"оба ордера исчезли → определено как {reason}"
                else:
                    reason = "stop_loss"
                    cause = "закрыта вручную через терминал"

                fallback = position.target_price if reason == "take_profit" else position.stop_price
                exit_price = self._get_real_exit_price(figi, fallback)
                logger.warning(
                    "[EXEC] Reconcile: %s not in portfolio — %s, closing in bot (price=%.2f)",
                    position.ticker, cause, exit_price,
                )
                self._close_position(figi, exit_price, reason)
                if self.notifier:
                    self.notifier.send_admin(
                        f"⚠️ Reconcile: {position.ticker} отсутствует в портфеле.\n"
                        f"Причина: {cause}.\n"
                        f"Позиция закрыта в боте, P&L записан в журнал."
                    )

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

        found_total = len(self.positions)
        closed_during_downtime = 0

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

            # Позиция была закрыта пока бот не работал — проверить портфель
            if self._has_portfolio_position(figi):
                # Стоп-ордера изменились, но бумага всё ещё на счёте
                logger.warning(
                    "[EXEC] Recovery: %s stop orders changed but position still in portfolio "
                    "— stop orders may have been cancelled manually while bot was down",
                    position.ticker,
                )
                if self.notifier:
                    self.notifier.send_admin(
                        f"⚠️ Recovery: {position.ticker} — стоп-ордера исчезли, "
                        f"но позиция ещё в портфеле.\n"
                        f"Возможно, стоп-ордера отменены вручную.\n"
                        f"SL: {position.stop_price} | TP: {position.target_price}\n"
                        f"Бот продолжит отслеживать позицию."
                    )
                continue  # Оставляем в self.positions, продолжаем мониторинг

            closed_during_downtime += 1
            if not sl_active and tp_active:
                exit_price = self._get_real_exit_price(figi, position.stop_price)
                self._close_position(figi, exit_price, "stop_loss")
            elif sl_active and not tp_active:
                exit_price = self._get_real_exit_price(figi, position.target_price)
                self._close_position(figi, exit_price, "take_profit")
            else:
                exit_price = self._get_real_exit_price(figi, position.stop_price)
                reason = self._determine_close_reason(position, exit_price)
                self._close_position(figi, exit_price, reason)

        self.state.reset_stale_sl_counters()

        pending_count = sum(1 for p in self.positions.values() if p.status == "pending")
        active_count = sum(1 for p in self.positions.values() if p.status == "active")
        logger.info(
            "[EXEC] Recovery complete. Found: %d, closed during downtime: %d, "
            "still active: %d, pending: %d",
            found_total, closed_during_downtime, active_count, pending_count,
        )
        if self.notifier:
            self.notifier.send_admin(
                f"Бот перезапущен. Найдено позиций: {found_total} "
                f"(закрыто пока стоял: {closed_during_downtime}). "
                f"Активных: {active_count}, ожидающих: {pending_count}"
            )

    def cancel_pending_market_close(self) -> list[dict]:
        """Отменить все pending лимитки из-за закрытия рынка.

        Возвращает список событий (для консолидации).
        """
        events: list[dict] = []
        for figi in list(self.positions.keys()):
            position = self.positions[figi]
            if position.status != "pending":
                continue
            try:
                self.broker.cancel_order(self.account_id, position.entry_order_id)
            except Exception:
                logger.debug("[EXEC] Could not cancel entry order %s on market close",
                             position.entry_order_id)
                if self._is_order_filled(position.entry_order_id):
                    logger.info("[EXEC] %s: order filled during market close cancel — activating",
                                position.ticker)
                    try:
                        self._activate_position(figi)
                    except Exception:
                        logger.error("[EXEC] %s: failed to activate after market close race",
                                     position.ticker)
                        self.state.remove_position(figi)
                        del self.positions[figi]
                    continue

            self.state.remove_position(figi)
            del self.positions[figi]
            events.append({
                "type": "limit_cancelled_market_close",
                "ticker": position.ticker,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "client_id": self.client_id,
            })
            logger.info("[EXEC] %s: pending order cancelled (market close)", position.ticker)
        return events

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
