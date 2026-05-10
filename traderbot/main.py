"""
Multi-tenant точка входа TraderBot.

Один процесс обслуживает множество клиентских T-Bank счетов одновременно:
  - Свечи тянутся ОДИН РАЗ на тикер через market-data токен.
  - Сигнал стратегии вычисляется ОДИН РАЗ на тикер за цикл.
  - Ордера размещаются per-client через их собственные токены.

Запуск: py -3.12 -m traderbot.main
"""
import logging
import os
import pathlib
import signal
import threading
import time
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from traderbot.broker.factory import broker_from_client
from traderbot.broker.tbank import TBankBroker
from traderbot.clients.db import Database
from traderbot.clients.models import ClientRole, ClientStatus
from traderbot.clients.registry import ClientRegistry
from traderbot.config import AppConfig, load_config
from traderbot.data.feed import DataFeed
from traderbot.execution.manager import ExecutionManager
from traderbot.journal.multi_writer import ClientJournalView, MultiTradeJournal
from traderbot.journal.sqlite_writer import SqliteTradeJournal
from traderbot.journal.writer import TradeJournal
from traderbot.market_schedule import MarketSchedule
from traderbot.notifications.bot import TelegramBot
from traderbot.notifications.fsm import FSM
from traderbot.notifications.handlers.admin import _build_daily_report
from traderbot.notifications.telegram import TelegramNotifier
from traderbot.payments.manual import ManualProvider
from traderbot.risk.manager import RiskManager
from traderbot.state.client_view import ClientStateView
from traderbot.state.sqlite_store import SqliteStateStore
from traderbot.strategies.registry import get_strategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reason labels (shared)
# ---------------------------------------------------------------------------
_REASON_LABELS = {
    "stop_loss": "стоп-лосс",
    "take_profit": "тейк-профит",
    "timeout": "по времени",
    "revoked": "принудительно",
    "deleted": "удаление клиента",
}

# Порог consecutive_errors, после которого клиент автоматически ставится на паузу
MAX_CLIENT_ERRORS = 5
# Как часто (сек) проверять новых активных клиентов в БД
REGISTRY_REFRESH_SEC = 60
# Как часто (сек) сверять реальные позиции на бирже со state (только в торговые часы)
RECONCILE_INTERVAL_SEC = 300
# Как часто (сек) отправлять heartbeat в торговое время
HEARTBEAT_INTERVAL_SEC = 3600
# Как часто (сек) пытаться переподключить Telegram при ошибке инициализации
TELEGRAM_RETRY_SEC = 300

MSK = ZoneInfo("Europe/Moscow")

# Клиенты, которым уже отправлено уведомление об отсутствии токена (дедупликация)
_no_token_warned: set[int] = set()




# ---------------------------------------------------------------------------
# Консолидация торговых уведомлений
# ---------------------------------------------------------------------------

def _get_client_name(registry: ClientRegistry, client_id: int) -> str:
    client = registry.get_by_id(client_id)
    if client:
        return client.account_name or client.email or f"#{client_id}"
    return f"#{client_id}"


def _consolidate_and_send(
    events: list[dict],
    registry: ClientRegistry,
    notifier,
) -> None:
    """Консолидировать буферизированные торговые события и отправить админу."""
    if not events or notifier is None:
        return

    # Группировка по типу события и ключевым полям
    from collections import defaultdict

    groups: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for ev in events:
        etype = ev["type"]
        if etype == "limit_placed":
            key = (ev["ticker"], ev["direction"], ev["entry_price"],
                   ev["stop_price"], ev["target_price"], ev.get("entry_reason", ""))
        elif etype == "position_opened":
            key = (ev["ticker"], ev["direction"], ev["entry_price"],
                   ev["stop_price"], ev["target_price"], ev.get("entry_reason", ""))
        elif etype == "position_closed":
            key = (ev["ticker"], ev["direction"], ev["entry_price"],
                   ev["exit_price"], ev.get("close_reason", ""), ev.get("entry_reason", ""))
        elif etype in ("limit_cancelled_timeout", "limit_cancelled_external",
                       "limit_cancelled_market_close"):
            key = (ev["ticker"], ev["direction"], ev["entry_price"])
        else:
            key = (ev.get("ticker", ""), ev.get("direction", ""))
        groups[etype][key].append(ev)

    # Форматирование и отправка
    for etype, keyed in groups.items():
        for key, evts in keyed.items():
            msg = _format_consolidated_message(etype, evts, registry)
            if msg:
                notifier.send_admin(msg)


def _format_consolidated_message(
    etype: str, events: list[dict], registry: ClientRegistry,
) -> str:
    """Сформировать одно консолидированное сообщение из группы событий."""
    ev0 = events[0]
    clients_info = []
    for ev in events:
        name = _get_client_name(registry, ev["client_id"])
        clients_info.append((name, ev))

    if etype == "limit_placed":
        lines = [
            f"\U0001f4cb Выставлена лимитная заявка {ev0['ticker']} {ev0['direction']}",
            f"Цена: {ev0['entry_price']} | SL: {ev0['stop_price']} | TP: {ev0['target_price']}",
            f"Причина: {ev0.get('entry_reason', '—')}",
        ]
        for name, ev in clients_info:
            lines.append(f"  💼 {name}: {ev['qty']} лот.")
        return "\n".join(lines)

    if etype == "position_opened":
        lines = [
            f"\U0001f7e2 Позиция открыта {ev0['ticker']} {ev0['direction']}",
            f"Вход: {ev0['entry_price']} | SL: {ev0['stop_price']} | TP: {ev0['target_price']}",
            f"Причина: {ev0.get('entry_reason', '—')}",
        ]
        for name, ev in clients_info:
            lines.append(f"  💼 {name}: {ev['qty']} лот.")
        return "\n".join(lines)

    if etype == "position_closed":
        reason_label = _REASON_LABELS.get(ev0.get("close_reason", ""), ev0.get("close_reason", ""))
        lines = [
            f"\U0001f534 Закрыта позиция {ev0['ticker']} {ev0['direction']}",
            f"Вход: {ev0['entry_price']} \u2192 Выход: {ev0['exit_price']}",
            f"Причина закрытия: {reason_label}",
            f"Причина входа: {ev0.get('entry_reason', '—')}",
            f"Длительность: {ev0.get('candles_held', 0)} свечей",
        ]
        for name, ev in clients_info:
            lines.append(f"  💼 {name}: {ev['pnl']:+.2f} ₽")
        return "\n".join(lines)

    if etype == "limit_cancelled_timeout":
        lines = [
            f"\u274c Лимитная заявка {ev0['ticker']} {ev0['direction']} отменена",
            f"Цена: {ev0['entry_price']} | Не исполнена за {ev0.get('pending_candles', '?')} свечей",
        ]
        if len(clients_info) > 1:
            names = ", ".join(name for name, _ in clients_info)
            lines.append(f"  Клиенты: {names}")
        return "\n".join(lines)

    if etype == "limit_cancelled_external":
        lines = [
            f"\u274c Лимитная заявка {ev0['ticker']} {ev0['direction']} отменена",
            f"Цена: {ev0['entry_price']}",
        ]
        if len(clients_info) > 1:
            names = ", ".join(name for name, _ in clients_info)
            lines.append(f"  Клиенты: {names}")
        return "\n".join(lines)

    if etype == "limit_cancelled_market_close":
        lines = [
            f"\u274c Лимитная заявка {ev0['ticker']} {ev0['direction']} отменена (закрытие рынка)",
            f"Цена: {ev0['entry_price']}",
        ]
        if len(clients_info) > 1:
            names = ", ".join(name for name, _ in clients_info)
            lines.append(f"  Клиенты: {names}")
        return "\n".join(lines)

    return ""


def _get_today_pnl(db, now_msk: datetime) -> float:
    """Суммарный P&L по всем закрытым сделкам за текущий торговый день (МСК)."""
    today_start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_msk.astimezone(timezone.utc)
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE exit_time >= ?",
                (today_start_utc.isoformat(),),
            )
            row = cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception:
        logger.exception("[MAIN] _get_today_pnl failed")
        return 0.0


def _send_daily_summary(db, registry, notifier, now_msk: datetime) -> None:
    """Отправить итоги торгового дня всем администраторам."""
    if notifier is None:
        return
    try:
        text = _build_daily_report(db, registry, now_msk)
    except Exception:
        logger.exception("[MAIN] daily_summary: failed to build report")
        return
    notifier.send_admin(text)
    logger.info("[MAIN] Daily summary sent")


def _send_weekly_summary(db, notifier, now_msk: datetime) -> None:
    """Отправить итоги торговой недели всем администраторам (пятница после закрытия)."""
    if notifier is None:
        return

    from datetime import timedelta

    # Начало недели (понедельник 00:00 МСК)
    days_since_monday = now_msk.weekday()
    week_start_msk = (now_msk - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_utc = week_start_msk.astimezone(timezone.utc)

    try:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, direction, pnl, exit_time, exit_reason
                FROM trades
                WHERE exit_time >= ?
                ORDER BY exit_time ASC
                """,
                (week_start_utc.isoformat(),),
            )
            trades = cur.fetchall()
    except Exception:
        logger.exception("[MAIN] weekly_summary: DB query failed")
        return

    week_label = f"{week_start_msk.strftime('%d.%m')}–{now_msk.strftime('%d.%m.%Y')}"

    if not trades:
        notifier.send_admin(f"📊 Итоги недели {week_label}: сделок не было.")
        return

    total_pnl = sum(float(t["pnl"]) for t in trades)
    wins = sum(1 for t in trades if float(t["pnl"]) > 0)
    losses = len(trades) - wins
    win_rate = wins / len(trades) * 100

    # По тикерам
    ticker_stats: dict[str, dict] = {}
    for t in trades:
        tk = t["ticker"]
        if tk not in ticker_stats:
            ticker_stats[tk] = {"pnl": 0.0, "total": 0, "wins": 0}
        ticker_stats[tk]["pnl"] += float(t["pnl"])
        ticker_stats[tk]["total"] += 1
        if float(t["pnl"]) > 0:
            ticker_stats[tk]["wins"] += 1

    # По дням
    day_pnl: dict[str, float] = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t["exit_time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            day_key = dt.astimezone(MSK).strftime("%d.%m %a")
        except Exception:
            day_key = "??"
        day_pnl[day_key] = day_pnl.get(day_key, 0.0) + float(t["pnl"])

    lines = [
        f"📊 Итоги недели {week_label}",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Сделок: {len(trades)} | P&L: {total_pnl:+.2f} ₽",
        f"✅ Прибыльных: {wins} ({win_rate:.0f}%) | ❌ Убыточных: {losses}",
    ]

    # По дням
    lines.append("\n📅 По дням:")
    for day, pnl in day_pnl.items():
        icon = "✅" if pnl >= 0 else "❌"
        lines.append(f"  {icon} {day}: {pnl:+.2f} ₽")

    best_day = max(day_pnl.items(), key=lambda x: x[1])
    worst_day = min(day_pnl.items(), key=lambda x: x[1])
    lines.append(f"\nЛучший день: {best_day[0]} ({best_day[1]:+.2f} ₽)")
    lines.append(f"Худший день: {worst_day[0]} ({worst_day[1]:+.2f} ₽)")

    # По тикерам
    if len(ticker_stats) > 1:
        lines.append("\n📈 По тикерам:")
        for tk, s in sorted(ticker_stats.items(), key=lambda x: -x[1]["pnl"]):
            tk_wr = s["wins"] / s["total"] * 100
            icon = "✅" if s["pnl"] >= 0 else "❌"
            lines.append(f"  {icon} {tk}: {s['pnl']:+.2f} ₽ | {s['total']} сд. | WR {tk_wr:.0f}%")

    notifier.send_admin("\n".join(lines))
    logger.info("[MAIN] Weekly summary sent: %d trades, P&L=%.2f RUB", len(trades), total_pnl)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler(
        "logs/bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


# ---------------------------------------------------------------------------
# Bootstrap: admin clients from config → DB
# ---------------------------------------------------------------------------

def bootstrap_admin_clients(registry: ClientRegistry, config: AppConfig) -> None:
    """Upsert admin-клиентов из config.admin.tokens в БД.

    Это делается при каждом старте, чтобы новые токены из конфига сразу
    попадали в реестр без ручного вмешательства.

    Дополнительно: все chat_id из TELEGRAM_ADMIN_CHAT_IDS, у которых нет
    T-Bank токена, регистрируются как TG-only admins (могут использовать
    admin-команды бота, но не торгуют от своего имени).
    """
    admin_chat_ids = [
        x.strip()
        for x in os.environ.get(config.admin.chat_ids_env, "").split(",")
        if x.strip()
    ]

    token_chat_ids: set[int] = set()

    for i, token_cfg in enumerate(config.admin.tokens):
        # chat_id для конкретного токена
        chat_id_raw = os.environ.get(token_cfg.chat_id_env, "")
        if not chat_id_raw and admin_chat_ids:
            chat_id_raw = admin_chat_ids[0]
        if not chat_id_raw:
            logger.warning("[MAIN] Admin token '%s': no chat_id configured, skipping", token_cfg.name)
            continue
        try:
            tg_chat_id = int(chat_id_raw)
        except ValueError:
            logger.error("[MAIN] Admin token '%s': invalid chat_id '%s'", token_cfg.name, chat_id_raw)
            continue

        # Получить account_id через API (token_cfg.token_env содержит уже resolved токен)
        try:
            broker = TBankBroker(token=token_cfg.token_env, sandbox=False, app_name="TraderBot-bootstrap")
            account_id = broker.get_account_id()
        except Exception:
            logger.exception("[MAIN] Admin token '%s': failed to get account_id", token_cfg.name)
            continue

        registry.upsert_admin(
            tg_chat_id=tg_chat_id,
            account_name=token_cfg.name,
            tbank_token=token_cfg.token_env,
            tbank_account_id=account_id,
        )
        token_chat_ids.add(tg_chat_id)
        logger.info("[MAIN] Admin '%s' bootstrapped: chat_id=%d account=%s",
                    token_cfg.name, tg_chat_id, account_id)

    # TG-only admins: есть в TELEGRAM_ADMIN_CHAT_IDS, но нет T-Bank токена
    for chat_id_str in admin_chat_ids:
        try:
            tg_chat_id = int(chat_id_str)
        except ValueError:
            logger.error("[MAIN] Invalid chat_id in %s: '%s'", config.admin.chat_ids_env, chat_id_str)
            continue
        if tg_chat_id in token_chat_ids:
            continue  # уже зарегистрирован с токеном выше
        registry.upsert_admin(tg_chat_id=tg_chat_id, account_name="admin")
        logger.info("[MAIN] TG-only admin bootstrapped: chat_id=%d", tg_chat_id)


# ---------------------------------------------------------------------------
# Migrate legacy JSON state (однократно)
# ---------------------------------------------------------------------------

def migrate_legacy_state(registry: ClientRegistry, sqlite_state: SqliteStateStore,
                         config: AppConfig) -> None:
    """Перелить данные из старых JSON-файлов в SQLite (однократно)."""
    import json

    # state/positions.json → positions для первого admin-клиента
    positions_path = pathlib.Path("state") / "positions.json"
    migrated_path = positions_path.with_suffix(".json.migrated")
    if positions_path.exists() and not migrated_path.exists():
        admins = registry.list_by_status(ClientStatus.ACTIVE)
        admins = [c for c in admins if c.role == ClientRole.ADMIN]
        if admins:
            admin_client = admins[0]
            try:
                with open(positions_path, encoding="utf-8") as f:
                    data = json.load(f)

                from traderbot.types import Position, Signal
                for figi, raw in data.get("positions", {}).items():
                    last_ct = (datetime.fromisoformat(raw["last_candle_time"])
                               if raw.get("last_candle_time") else None)
                    pos = Position(
                        ticker=raw["ticker"], figi=raw["figi"],
                        direction=Signal(raw["direction"]),
                        entry_price=raw["entry_price"], stop_price=raw["stop_price"],
                        target_price=raw["target_price"], qty=raw["qty"],
                        lot_size=raw.get("lot_size", 1),
                        entry_time=datetime.fromisoformat(raw["entry_time"]),
                        entry_reason=raw.get("entry_reason", ""),
                        entry_order_id=raw.get("entry_order_id", ""),
                        sl_order_id=raw.get("sl_order_id", ""),
                        tp_order_id=raw.get("tp_order_id", ""),
                        status=raw.get("status", "active"),
                        candles_held=raw.get("candles_held", 0),
                        pending_candles=raw.get("pending_candles", 0),
                        last_candle_time=last_ct,
                    )
                    sqlite_state.save_position(admin_client.id, pos)
                    logger.info("[MIGRATE] Position %s → client_id=%d", figi, admin_client.id)

                for ticker, sl_data in data.get("consecutive_sl", {}).items():
                    count = sl_data.get("count", 0)
                    for _ in range(count):
                        sqlite_state.increment_consecutive_sl(admin_client.id, ticker)

                positions_path.rename(migrated_path)
                logger.info("[MIGRATE] positions.json → .migrated (%d positions)",
                            len(data.get("positions", {})))
            except Exception:
                logger.exception("[MIGRATE] Failed to migrate positions.json")

    # state/telegram_subscribers.json → clients (pending_payment)
    subs_path = pathlib.Path("state") / "telegram_subscribers.json"
    subs_migrated = subs_path.with_suffix(".json.migrated")
    if subs_path.exists() and not subs_migrated.exists():
        try:
            with open(subs_path, encoding="utf-8") as f:
                subs_data = json.load(f)
            count = 0
            for chat_id_str in subs_data:
                try:
                    _, created = registry.get_or_create_subscriber(int(chat_id_str))
                    if created:
                        count += 1
                except Exception:
                    logger.exception("[MIGRATE] subscriber chat_id=%s", chat_id_str)
            subs_path.rename(subs_migrated)
            logger.info("[MIGRATE] telegram_subscribers.json → .migrated (%d imported)", count)
        except Exception:
            logger.exception("[MIGRATE] Failed to migrate telegram_subscribers.json")


# ---------------------------------------------------------------------------
# Per-client ExecutionManager factory
# ---------------------------------------------------------------------------

def _build_exec(client, config: AppConfig, sqlite_state: SqliteStateStore,
                multi_journal: MultiTradeJournal, notifier) -> ExecutionManager | None:
    """Создать ExecutionManager для клиента. Возвращает None при ошибке подключения."""
    try:
        broker, account_id = broker_from_client(client, app_name="TraderBot")
    except Exception:
        logger.exception("[MAIN] broker_from_client failed for client %d", client.id)
        return None

    deposit = None
    for attempt in range(1, 4):
        try:
            deposit = broker.get_portfolio_balance(account_id)
            break
        except Exception:
            if attempt < 3:
                logger.warning(
                    "[MAIN] get_portfolio_balance attempt %d/3 failed for client %d, retrying in %ds",
                    attempt, client.id, attempt * 2,
                )
                time.sleep(attempt * 2)
            else:
                logger.exception("[MAIN] get_portfolio_balance failed for client %d after 3 attempts", client.id)
                return None

    risk = RiskManager(config.risk_pct, config.max_position_pct, deposit)
    state_view = ClientStateView(sqlite_state, client.id)
    journal_view = ClientJournalView(multi_journal, client.id)

    em = ExecutionManager(
        broker=broker,
        risk=risk,
        journal=journal_view,
        state=state_view,
        notifier=notifier,
        account_id=account_id,
        commission_pct=config.commission_pct,
        max_candles_timeout=config.max_candles_timeout,
        max_consecutive_sl=config.max_consecutive_sl,
        max_daily_sl=config.max_daily_sl,
        max_open_positions=config.max_open_positions,
        client_id=client.id,
        is_admin=(client.role == ClientRole.ADMIN),
    )
    return em


# ---------------------------------------------------------------------------
# sync_execs: подтянуть новых активных клиентов, убрать неактивных
# ---------------------------------------------------------------------------

def _expire_overdue_subscribers(registry: ClientRegistry, execs: dict, notifier) -> None:
    """Перевести клиентов с истёкшим paid_until из active → expired.

    Вызывается в начале каждого sync_execs. Только role=subscriber проверяется —
    у admin paid_until=NULL, они никогда не истекают.
    """
    now = datetime.now(timezone.utc)
    active_clients = registry.list_active()
    for client in active_clients:
        if client.role == ClientRole.ADMIN:
            continue
        if client.paid_until and client.paid_until < now:
            registry.update_status(client.id, ClientStatus.EXPIRED)
            execs.pop(client.id, None)
            logger.info("[MAIN] Client %d subscription expired (paid_until=%s), status→expired",
                        client.id, client.paid_until.isoformat())
            if notifier:
                notifier.send_admin(
                    f"⏰ Подписка истекла: клиент {client.id} "
                    f"(до {client.paid_until.strftime('%d.%m.%Y')}), статус → expired"
                )
                notifier.send_to_client(
                    client.id,
                    "⏰ Ваша подписка истекла. Торговля приостановлена.\n"
                    "Для продления напишите /pay или обратитесь к администратору: @MakeRFGreatAgain"
                )


def _warn_expiring_soon(registry: ClientRegistry, notifier) -> None:
    """Отправить предупреждение клиентам, чья подписка скоро истекает.

    Пороги: 7 дней, 3 дня, 1 день, 1 час.
    Чтобы не спамить при каждом вызове sync_execs (раз в 60 сек), уведомление
    отправляется только один раз за каждый порог: проверяем, укладывается ли
    paid_until ровно в окно [порог, порог + REGISTRY_REFRESH_SEC].
    """
    _THRESHOLDS = [
        (7 * 86400, "7 дней"),
        (3 * 86400, "3 дня"),
        (1 * 86400, "1 день"),
        (3600,      "1 час"),
    ]
    now = datetime.now(timezone.utc)
    active_clients = registry.list_active()
    for client in active_clients:
        if client.role == ClientRole.ADMIN or not client.paid_until:
            continue
        delta_sec = (client.paid_until - now).total_seconds()
        for threshold_sec, label in _THRESHOLDS:
            if threshold_sec >= delta_sec > threshold_sec - REGISTRY_REFRESH_SEC:
                if notifier:
                    notifier.send_to_client(
                        client.id,
                        f"⚠️ Ваша подписка истекает через {label} "
                        f"({client.paid_until.strftime('%d.%m.%Y %H:%M')}).\n"
                        "Для продления напишите /pay или обратитесь к администратору: @MakeRFGreatAgain",
                    )
                    notifier.send_admin(
                        f"⏳ Подписка клиента {client.id} истекает через {label} "
                        f"({client.paid_until.strftime('%d.%m.%Y %H:%M')})"
                    )
                logger.info("[MAIN] Client %d subscription expires in %s (paid_until=%s)",
                            client.id, label, client.paid_until.isoformat())
                break


def sync_execs(
    execs: dict,
    registry: ClientRegistry,
    config: AppConfig,
    sqlite_state: SqliteStateStore,
    multi_journal: MultiTradeJournal,
    notifier,
) -> None:
    """Синхронизировать dict execs с актуальным состоянием БД.

    - Отправить предупреждения о скором истечении подписки (за 7 и 1 день).
    - Перевести клиентов с истёкшим paid_until → expired.
    - Добавить новых active-клиентов (появились после /setup).
    - Убрать paused/expired клиентов (новые позиции не открываем,
      но если есть открытые — em остаётся до их закрытия через флаг _revoked).
    - Для revoked: если позиций нет — убрать немедленно.
    """
    _warn_expiring_soon(registry, notifier)
    _expire_overdue_subscribers(registry, execs, notifier)
    active_clients = registry.list_active()
    active_ids = {c.id for c in active_clients}

    # Добавить новых
    for client in active_clients:
        if client.id not in execs:
            # Клиент без токена: одноразовое уведомление, без инкремента ошибок
            if not client.tbank_token or not client.tbank_account_id:
                if client.id not in _no_token_warned:
                    _no_token_warned.add(client.id)
                    logger.warning(
                        "[MAIN] Client %d has no tbank_token/account_id — skipping", client.id
                    )
                    if notifier:
                        notifier.send_admin(
                            f"⚠️ Клиент {client.id} не имеет токена T-Bank.\n"
                            f"Чтобы добавить токен — попросите клиента пройти онбординг (/setup).\n"
                            f"Чтобы отозвать — используйте /revoke {client.tg_chat_id}."
                        )
                continue
            em = _build_exec(client, config, sqlite_state, multi_journal, notifier)
            if em is None:
                registry.increment_errors(client.id)
                if notifier:
                    notifier.send_admin(f"⚠️ Клиент {client.id} не подключился")
                continue
            try:
                em.recover()
            except Exception:
                logger.exception("[MAIN] recover() failed for client %d", client.id)
            execs[client.id] = em
            logger.info("[MAIN] Client %d added to trading loop", client.id)
            if notifier:
                notifier.send_admin(f"✅ Клиент {client.id} добавлен в торговый цикл")

    # Убрать тех, кто больше не active
    for client_id in list(execs.keys()):
        if client_id in active_ids:
            continue
        em = execs[client_id]
        # Если клиент отозван или достигнут дневной лимит SL — оставляем
        # для сопровождения открытых позиций до SL/TP
        if (em._revoked or em._daily_sl_limit_reached) and em.positions:
            continue
        # Иначе убираем
        del execs[client_id]
        logger.info("[MAIN] Client %d removed from trading loop", client_id)


# ---------------------------------------------------------------------------
# handle_client_error: изоляция ошибок
# ---------------------------------------------------------------------------

def handle_client_error(
    client_id: int,
    registry: ClientRegistry,
    execs: dict,
    notifier,
) -> None:
    """Инкрементировать ошибки клиента, при превышении порога — auto-pause."""
    errors = registry.increment_errors(client_id)
    if errors >= MAX_CLIENT_ERRORS:
        registry.update_status(client_id, ClientStatus.PAUSED)
        execs.pop(client_id, None)
        logger.warning("[MAIN] Client %d auto-paused after %d consecutive errors",
                       client_id, errors)
        if notifier:
            notifier.send_admin(
                f"⚠️ Клиент {client_id} автоматически приостановлен "
                f"после {errors} ошибок подряд."
            )


# ---------------------------------------------------------------------------
# handle_daily_sl_limit: пауза клиента при достижении дневного лимита SL
# ---------------------------------------------------------------------------

def handle_daily_sl_limit(
    client_id: int,
    registry: ClientRegistry,
    execs: dict,
    notifier,
) -> None:
    """Поставить клиента на паузу из-за дневного лимита стоп-лоссов.

    Статус в БД → PAUSED. Если у клиента ещё есть открытые позиции,
    em остаётся в execs со флагом _daily_sl_limit_reached (не открывает новых,
    сопровождает существующие до SL/TP). Когда позиций не останется —
    sync_execs уберёт em сам.
    """
    registry.update_status(client_id, ClientStatus.PAUSED)
    em = execs.get(client_id)
    ticker = em._daily_sl_limit_ticker if em else None
    if em and not em.positions:
        execs.pop(client_id, None)
    ticker_info = f" по {ticker}" if ticker else ""
    logger.warning(
        "[MAIN] Client %d paused: daily SL limit reached%s", client_id, ticker_info
    )
    if notifier:
        notifier.send_admin(
            f"🛑 Клиент {client_id} приостановлен: достигнут дневной лимит стоп-лоссов{ticker_info}."
        )
        if em:
            notifier.send_to_client(
                client_id,
                f"🛑 Торговля приостановлена: достигнут дневной лимит стоп-лоссов{ticker_info}.\n"
                "Для возобновления используйте /resume или обратитесь к администратору.",
            )


# ---------------------------------------------------------------------------
# Position snapshot для /trades (оставлен для совместимости с handlers)
# ---------------------------------------------------------------------------

def format_positions_snapshot(execs: dict) -> str:
    """Текстовый снимок всех позиций по всем клиентам."""
    lines = []
    for client_id, em in execs.items():
        positions = list(em.positions.values())
        if not positions:
            continue
        lines.append(f"\n--- Клиент {client_id} ---")
        for p in positions:
            lines.append(
                f"{'🟢' if p.status == 'active' else '📋'} {p.ticker} {p.direction.value} "
                f"({p.status})\n"
                f"Вход: {p.entry_price} | SL: {p.stop_price} | TP: {p.target_price}\n"
                f"Объём: {p.qty} | Свечей: {p.candles_held}"
            )
    return "\n".join(lines) if lines else "Открытых позиций нет."


def _log_setup(db: Database, ticker: str, strategy_name: str, setup,
               action: str, market_price: float | None,
               open_positions: int, candle_time_30m) -> None:
    """Record every setup found by the live bot for sim comparison."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        ct = str(candle_time_30m) if candle_time_30m else None
        with db.write() as cur:
            cur.execute(
                "INSERT INTO setup_log "
                "(ts, ticker, strategy, direction, entry_price, stop_price, "
                " target_price, entry_reason, action, market_price, "
                " open_positions, candle_time_30m) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, ticker, strategy_name, setup.direction.value,
                 setup.entry_price, setup.stop_price, setup.target_price,
                 setup.entry_reason, action, market_price, open_positions, ct),
            )
    except Exception:
        logger.debug("[MAIN] Failed to log setup for %s", ticker, exc_info=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()

    # Флаг остановки: устанавливается KeyboardInterrupt или SIGTERM
    _stop = threading.Event()

    def _shutdown_handler(signum, frame):  # noqa: ARG001
        logger.info("[MAIN] Signal %s received, shutting down...", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    # На Windows SIGBREAK (Ctrl+Break) → тоже корректная остановка
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown_handler)

    config_path = str(pathlib.Path(__file__).parent / "config.yaml")
    config = load_config(config_path)

    # 1. БД
    os.makedirs(os.path.dirname(config.database.path) or ".", exist_ok=True)
    db = Database(config.database.path)
    db.init_schema()
    registry = ClientRegistry(db)

    # 2. Общие компоненты
    sqlite_state = SqliteStateStore(db)
    sqlite_journal = SqliteTradeJournal(db)
    csv_journal = TradeJournal(config.journal_path)
    multi_journal = MultiTradeJournal(sqlite_journal, csv_journal)

    # 3. Market data broker (один для всех)
    md_broker = TBankBroker(
        token=config.market_data.token_env,
        sandbox=False,
        app_name=config.market_data.app_name,
    )
    feed = DataFeed(md_broker)
    first_figi = next(iter(config.tickers.values())).figi
    schedule = MarketSchedule.from_figi(md_broker, first_figi)

    # 4. Bootstrap admin-клиентов из конфига → БД
    bootstrap_admin_clients(registry, config)

    # 5. Миграция legacy JSON → SQLite (однократно)
    migrate_legacy_state(registry, sqlite_state, config)

    # 6. Стратегии (общие)
    strategies = {}
    for ticker_name, ticker_conf in config.tickers.items():
        strategies[ticker_name] = get_strategy(ticker_conf.strategy)

    known_tickers_by_figi = {tc.figi: name for name, tc in config.tickers.items()}

    # 7. execs — сначала пустой dict, затем передаётся в TelegramBot по ссылке
    execs: dict[int, ExecutionManager] = {}
    # Тикеры, отключённые админом через /disable_ticker (shared mutable set)
    disabled_tickers: set[str] = set()

    # 8. Telegram: бот + notifier (execs уже существует, передаём по ссылке)
    notifier = None
    tg_bot = None
    fsm = FSM()
    reload_event = threading.Event()
    payments = ManualProvider(
        registry=registry,
        db=db,
        price_rub=config.subscription.price_rub,
        period_days=config.subscription.period_days,
        admin_contact="@MakeRFGreatAgain",
    )

    def _try_init_telegram() -> tuple:
        """Попытка инициализировать Telegram. Возвращает (tg_bot, notifier) или (None, None)."""
        nonlocal tg_bot, notifier
        try:
            tg_bot = TelegramBot(
                token=config.telegram.token,
                registry=registry,
                db=db,
                execs=execs,
                fsm=fsm,
                payment_provider=payments,
                reload_event=reload_event,
                disabled_tickers=disabled_tickers,
                config=config,
            )
            notifier = TelegramNotifier(
                bot=tg_bot.bot,
                registry=registry,
                admin_chat_ids=config.telegram.admin_chat_ids,
            )
            tg_bot.set_notifier(notifier)
            tg_bot.start()
            logger.info("[MAIN] Telegram connected successfully")
            return tg_bot, notifier
        except Exception:
            logger.exception("[MAIN] Failed to init Telegram, trading continues without notifications")
            tg_bot = None
            notifier = None
            return None, None

    if config.telegram.enabled:
        _try_init_telegram()

    # 9. Инициализировать ExecutionManager для каждого active-клиента
    for client in registry.list_active():
        if not client.tbank_token or not client.tbank_account_id:
            _no_token_warned.add(client.id)
            logger.warning("[MAIN] Client %d has no tbank_token/account_id at startup — skipping", client.id)
            if notifier:
                notifier.send_to_client(
                    client.id,
                    "⚠️ Для начала торговли необходимо добавить токен T-Bank.\n"
                    "Используйте /mytoken чтобы установить токен.",
                )
                notifier.send_admin(
                    f"⚠️ Клиент {client.id} не имеет токена T-Bank.\n"
                    f"Чтобы добавить — попросите пройти /setup.\n"
                    f"Чтобы отозвать — /revoke {client.tg_chat_id}."
                )
            continue
        em = _build_exec(client, config, sqlite_state, multi_journal, notifier)
        if em is None:
            registry.increment_errors(client.id)
            if notifier:
                notifier.send_admin(f"⚠️ Клиент {client.id} не инициализирован при старте")
            continue
        try:
            em.recover()
            registry.reset_errors(client.id)
        except Exception:
            logger.exception("[MAIN] recover() failed for client %d", client.id)
        execs[client.id] = em

    # 10. Уведомить о старте + однократно — клиенты без никнейма
    mode_str = f"multi-tenant | clients={len(execs)} | tickers={len(config.tickers)}"
    logger.info("[MAIN] Bot started. %s", mode_str)
    if notifier:
        notifier.send_admin(f"🚀 Бот запущен. {mode_str}")
        for client in registry.list_active():
            if not client.account_name:
                notifier.send_to_client(
                    client.id,
                    "👋 Привет! У вас не задан никнейм.\n"
                    "Используйте /nickname чтобы задать имя для вашего профиля.",
                )

    # 10. Основной цикл
    last_reconcile = 0.0
    last_refresh = 0.0
    last_heartbeat = 0.0
    last_tg_retry = 0.0
    _market_was_open: bool | None = None   # None = первая итерация
    _daily_summary_sent_date: date | None = None

    try:
        while not _stop.is_set():
            try:
                # Переподключение Telegram, если не удалось при старте
                if config.telegram.enabled and tg_bot is None:
                    if time.time() - last_tg_retry >= TELEGRAM_RETRY_SEC:
                        logger.info("[MAIN] Retrying Telegram connection...")
                        _try_init_telegram()
                        last_tg_retry = time.time()
                        # Обновить notifier во всех ExecutionManager
                        if notifier:
                            for em in execs.values():
                                em.notifier = notifier

                now_msk = datetime.now(timezone.utc).astimezone(MSK)
                schedule.refresh()
                market_open = schedule.is_open(now_msk)

                # Детектировать открытие рынка → уведомление с балансами
                if _market_was_open is False and market_open:
                    logger.info("[MAIN] Market opened at %s MSK", now_msk.strftime("%H:%M"))
                    if notifier:
                        lines = [
                            f"🟢 Рынок открылся ({now_msk.strftime('%H:%M')} МСК)",
                            f"Активных клиентов в цикле: {len(execs)}",
                        ]
                        total_balance = 0.0
                        for cid, em in execs.items():
                            client = registry.get_by_id(cid)
                            name = (client.account_name or client.email or f"#{cid}") if client else f"#{cid}"
                            try:
                                bal = em.broker.get_portfolio_balance(em.account_id)
                                total_balance += bal
                                lines.append(f"  💼 {name}: {bal:,.2f} ₽")
                            except Exception:
                                lines.append(f"  💼 {name}: баланс недоступен")
                        if execs:
                            lines.append(f"Итого: {total_balance:,.2f} ₽")
                        notifier.send_admin("\n".join(lines))

                # Детектировать закрытие рынка → отмена лимиток + дневная статистика
                if _market_was_open is True and not market_open:
                    logger.info("[MAIN] Market closed at %s MSK", now_msk.strftime("%H:%M"))
                    # Отменить все pending лимитки с корректным сообщением
                    market_close_events: list[dict] = []
                    for client_id, em in list(execs.items()):
                        try:
                            market_close_events.extend(em.cancel_pending_market_close())
                        except Exception:
                            logger.exception("[MAIN] cancel_pending_market_close failed for client %d",
                                             client_id)
                    _consolidate_and_send(market_close_events, registry, notifier)
                    _send_daily_summary(db, registry, notifier, now_msk)
                    _daily_summary_sent_date = now_msk.date()
                    # Пятница → недельный отчёт
                    if now_msk.weekday() == 4:
                        _send_weekly_summary(db, notifier, now_msk)

                # Fallback: бот запущен после закрытия рынка —
                # отправить итоги дня, если были сделки и ещё не отправлялись
                if _market_was_open is None and not market_open:
                    if now_msk.weekday() < 5 and now_msk.date() not in schedule._holidays:
                        if now_msk.date() != _daily_summary_sent_date:
                            # Не отправляем пустой отчёт при старте —
                            # только если сегодня реально были сделки
                            if _get_today_pnl(db, now_msk) != 0.0:
                                logger.info("[MAIN] Bot started after market close, sending daily summary")
                                _send_daily_summary(db, registry, notifier, now_msk)
                            _daily_summary_sent_date = now_msk.date()

                _market_was_open = market_open

                # Подтянуть новых клиентов / убрать неактивных
                # (по таймеру или по сигналу от /reload_clients)
                if time.time() - last_refresh >= REGISTRY_REFRESH_SEC or reload_event.is_set():
                    if reload_event.is_set():
                        logger.info("[MAIN] Manual reload triggered via /reload_clients")
                        reload_event.clear()
                    prev_count = len(execs)
                    sync_execs(execs, registry, config, sqlite_state, multi_journal, notifier)
                    new_count = len(execs)
                    if new_count != prev_count and notifier:
                        notifier.send_admin(
                            f"🔄 Клиентов в торговом цикле: {new_count}"
                        )
                    last_refresh = time.time()

                # Вне торговых часов: пропускаем API-вызовы, тихо ждём
                if not market_open:
                    _stop.wait(timeout=config.poll_interval_sec)
                    continue

                # Периодическая сверка со счётом (per-client, только в торговое время)
                if time.time() - last_reconcile >= RECONCILE_INTERVAL_SEC:
                    for client_id, em in list(execs.items()):
                        try:
                            em.reconcile_with_broker(known_tickers_by_figi)
                        except Exception:
                            logger.exception("[MAIN] reconcile failed for client %d", client_id)
                            handle_client_error(client_id, registry, execs, notifier)
                    last_reconcile = time.time()

                # Heartbeat раз в час + проверка связи с API
                if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                    if notifier:
                        active_pos = sum(
                            sum(1 for p in em.positions.values() if p.status == "active")
                            for em in execs.values()
                        )
                        pending_pos = sum(
                            sum(1 for p in em.positions.values() if p.status == "pending")
                            for em in execs.values()
                        )
                        pnl_today = _get_today_pnl(db, now_msk)

                        # Нереализованный P&L по открытым активным позициям
                        floating_pnl = 0.0
                        for em in execs.values():
                            for figi_hb, pos in em.positions.items():
                                if pos.status != "active":
                                    continue
                                try:
                                    price = em.broker.get_last_price(figi_hb)
                                    if price is None:
                                        continue
                                    shares = pos.qty * pos.lot_size
                                    if pos.direction.value == "BUY":
                                        floating_pnl += (price - pos.entry_price) * shares
                                    else:
                                        floating_pnl += (pos.entry_price - price) * shares
                                except Exception:
                                    pass

                        # Проверка связи с T-Bank API (market data broker)
                        api_ok = False
                        try:
                            md_broker.get_account_id()
                            api_ok = True
                        except Exception:
                            logger.warning("[MAIN] Heartbeat: T-Bank API health check failed")

                        floating_str = f" | float: {floating_pnl:+.2f} ₽" if active_pos > 0 else ""
                        notifier.send_admin(
                            f"{'✅' if api_ok else '⚠️'} {now_msk.strftime('%H:%M')} МСК | "
                            f"клиентов: {len(execs)} | "
                            f"позиций: {active_pos} | лимиток: {pending_pos} | "
                            f"P&L: {pnl_today:+.2f} ₽{floating_str} | "
                            f"API: {'OK' if api_ok else 'НЕ ОТВЕЧАЕТ'}"
                        )
                    last_heartbeat = time.time()

                # Торговый цикл по тикерам
                # Клиенты, у которых хотя бы один тикер прошёл без исключения
                successful_clients: set[int] = set()

                # Клиенты, которых нужно поставить на паузу по дневному лимиту SL
                daily_sl_triggered: set[int] = set()

                for ticker_name, ticker_conf in config.tickers.items():
                    if ticker_name in disabled_tickers:
                        continue
                    figi = ticker_conf.figi
                    strategy = strategies[ticker_name]
                    timeframes = list(dict.fromkeys(["30m"] + strategy.required_timeframes))

                    # Свечи — ОДИН РАЗ через market-data брокер
                    # 15 дней нужны всем: EMA50 на 1H требует ~62 свечи, на 4H ~50 свечей (~200ч)
                    candle_days = 15
                    try:
                        candles = feed.get_candles(figi=figi, timeframes=timeframes, days=candle_days)
                    except Exception:
                        logger.exception("[MAIN] feed error for %s", ticker_name)
                        continue
                    if not candles:
                        continue

                    min_tf = strategy.required_timeframes[0]
                    current_price = candles[min_tf].iloc[-1]["close"]
                    last_candle_time = candles["30m"].index[-1].to_pydatetime()

                    # Сигнал — ОДИН РАЗ на тикер
                    shared_setup = strategy.find_setup(candles)

                    # Логируем найденный setup для сравнения с sim
                    if shared_setup is not None:
                        total_open = sum(len(em.positions) for em in execs.values())
                        _log_setup(
                            db, ticker_name, ticker_conf.strategy, shared_setup,
                            action="found", market_price=float(current_price),
                            open_positions=total_open, candle_time_30m=last_candle_time,
                        )

                    # Per-client: обновить/открыть позицию
                    ticker_events: list[dict] = []
                    setup_used = False
                    for client_id, em in list(execs.items()):
                        # Если revoked или достигнут дневной лимит SL — только сопровождаем
                        can_open = not em._revoked and not em._daily_sl_limit_reached
                        try:
                            if em.has_position(figi):
                                em.update(figi, current_price, last_candle_time)
                                # После закрытия последней позиции у revoked-клиента убираем его
                                if em._revoked and not em.positions:
                                    del execs[client_id]
                                    logger.info("[MAIN] Revoked client %d: all positions closed, removed",
                                                client_id)
                            elif can_open:
                                if shared_setup is not None:
                                    if em.is_ticker_blocked(ticker_name):
                                        em.notify_ticker_blocked(ticker_name)
                                    else:
                                        opened = em.open_position(ticker_name, figi, shared_setup)
                                        if opened:
                                            setup_used = True
                                            _log_setup(
                                                db, ticker_name, ticker_conf.strategy, shared_setup,
                                                action="opened", market_price=float(current_price),
                                                open_positions=total_open, candle_time_30m=last_candle_time,
                                            )
                        except Exception:
                            logger.exception("[MAIN] client %d tick error on %s", client_id, ticker_name)
                            handle_client_error(client_id, registry, execs, notifier)
                        else:
                            successful_clients.add(client_id)

                        # Собрать буферизированные события
                        ticker_events.extend(em.drain_trade_events())

                        # Запомнить для обработки после цикла тикеров (не вызываем повторно)
                        if em._daily_sl_limit_reached and client_id not in daily_sl_triggered:
                            daily_sl_triggered.add(client_id)

                    # Обновить state стратегии после успешного открытия
                    if setup_used:
                        strategy.on_trade_opened()

                    # Консолидировать и отправить уведомления по тикеру
                    _consolidate_and_send(ticker_events, registry, notifier)

                # Пауза по дневному лимиту SL — один раз на клиента после всех тикеров
                for client_id in daily_sl_triggered:
                    if client_id in execs:
                        handle_daily_sl_limit(client_id, registry, execs, notifier)

                # Сбросить счётчик ошибок для клиентов, у которых цикл прошёл без исключений
                for client_id in successful_clients:
                    if client_id in execs:
                        registry.reset_errors(client_id)

                # Ждём poll_interval или раньше если пришёл сигнал остановки
                _stop.wait(timeout=config.poll_interval_sec)

            except KeyboardInterrupt:
                logger.info("[MAIN] Stopped by user (Ctrl+C).")
                break
            except Exception as e:
                logger.exception("[MAIN] Main loop error: %s", e)
                if notifier:
                    notifier.send_admin(f"❌ Ошибка основного цикла: {e}")
                _stop.wait(timeout=config.poll_interval_sec)

    finally:
        logger.info("[MAIN] Shutting down.")
        if notifier:
            open_positions = sum(len(em.positions) for em in execs.values())
            notifier.send_admin(
                f"🛑 Бот остановлен. Открытых позиций: {open_positions} "
                f"(SL/TP остаются на бирже)."
            )
        db.close()


if __name__ == "__main__":
    main()
