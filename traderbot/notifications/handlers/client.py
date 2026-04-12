"""Клиентские Telegram-команды: /start, /help, /status, /pause, /resume.

Этот модуль только регистрирует хендлеры — вся бизнес-логика идёт через
ClientRegistry, SqliteStateStore и execs dict из main loop.

Для /status читаем позиции из execs (актуальные) и последние 5 сделок из БД.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import telebot

from traderbot.clients.db import Database
from traderbot.clients.models import ClientRole, ClientStatus
from traderbot.clients.registry import ClientRegistry
from traderbot.notifications.fsm import FSM, OnboardingState
from traderbot.notifications.handlers.onboarding import TOKEN_HOWTO  # noqa: F401 (re-export)

logger = logging.getLogger(__name__)

# Статусы, при которых клиент уже прошёл онбординг (или в процессе)
_ONBOARDING_STATUSES = {
    ClientStatus.PENDING_EMAIL,
    ClientStatus.PENDING_TOKEN,
    ClientStatus.ACTIVE,
    ClientStatus.PAUSED,
}


def register(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    db: Database,
    execs: dict,         # dict[int, ExecutionManager] — mutable ref из main
    fsm: FSM,
    payment_provider=None,   # ManualProvider или другой провайдер
) -> None:
    """Зарегистрировать все клиентские хендлеры в боте."""

    # ------------------------------------------------------------------
    # /start
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["start"])
    def handle_start(message):
        chat_id = message.chat.id
        client, created = registry.get_or_create_subscriber(chat_id)
        if created:
            logger.info("[BOT] /start: new client chat_id=%d id=%d", chat_id, client.id)
            fsm.set_onboarding(chat_id, OnboardingState.PENDING_NICKNAME)
            bot.reply_to(
                message,
                "Добро пожаловать в TraderBot!\n\n"
                "Для начала введите ваш никнейм (2–32 символа).\n"
                "Он будет использоваться для вашей идентификации в системе:",
            )
        else:
            # Клиент существует, но никнейм ещё не задан
            if not client.account_name and client.status == ClientStatus.PENDING_PAYMENT:
                fsm.set_onboarding(chat_id, OnboardingState.PENDING_NICKNAME)
                bot.reply_to(
                    message,
                    "Для завершения регистрации введите ваш никнейм (2–32 символа):",
                )
            else:
                status_text = _status_hint(client.status)
                bot.reply_to(
                    message,
                    f"Вы уже зарегистрированы.\n{status_text}\n\n/help — список команд",
                )

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["help"])
    def handle_help(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Напишите /start для регистрации.")
            return

        if client.role == ClientRole.ADMIN:
            bot.reply_to(message, _admin_help_text())
        else:
            bot.reply_to(message, _client_help_text(client.status))

    # ------------------------------------------------------------------
    # /pay
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["pay"])
    def handle_pay(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return
        if client.status in (ClientStatus.ACTIVE, ClientStatus.PAUSED):
            paid_until = (
                client.paid_until.strftime("%d.%m.%Y") if client.paid_until else "бессрочно"
            )
            bot.reply_to(message, f"У вас уже активная подписка (до {paid_until}).")
            return

        if payment_provider is not None:
            from traderbot.config import SubscriptionConfig
            # Получаем параметры из провайдера напрямую
            text = payment_provider.create_invoice(
                client.id,
                payment_provider.price_rub,
                payment_provider.period_days,
            )
            bot.reply_to(message, text)
        else:
            bot.reply_to(
                message,
                "Для оформления подписки свяжитесь с администратором: @MakeRFGreatAgain\n"
                "После подтверждения оплаты вы получите доступ к настройке.",
            )

    # ------------------------------------------------------------------
    # /setup — запускается после /grant от админа (статус pending_email)
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["setup"])
    def handle_setup(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return
        if client.status == ClientStatus.ACTIVE:
            bot.reply_to(message, "Вы уже настроены и торгуете. Используйте /status.")
            return
        if client.status not in (ClientStatus.PENDING_EMAIL, ClientStatus.PENDING_TOKEN):
            bot.reply_to(
                message,
                "Команда /setup доступна после подтверждения оплаты администратором.\n"
                "Если вы уже оплатили — напишите /pay или свяжитесь с администратором.",
            )
            return

        # Запустить FSM онбординга
        fsm.set_onboarding(chat_id, OnboardingState.PENDING_EMAIL)
        bot.reply_to(
            message,
            "Начнём настройку.\n\n"
            "Шаг 1 из 2: введите вашу электронную почту:",
        )

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["status"])
    def handle_status(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return

        lines = [f"Статус аккаунта: {_status_label(client.status)}"]

        if client.paid_until:
            lines.append(f"Подписка до: {client.paid_until.strftime('%d.%m.%Y')}")
            if client.paid_until < datetime.now(timezone.utc):
                lines.append("⚠️ Подписка истекла")

        # Открытые позиции
        em = execs.get(client.id)
        if client.role == ClientRole.ADMIN:
            # Администратор видит полную информацию
            if em and em.positions:
                lines.append(f"\nОткрытых позиций: {len(em.positions)}")
                for p in em.positions.values():
                    icon = "🟢" if p.status == "active" else "📋"
                    lines.append(
                        f"{icon} {p.ticker} {p.direction.value}\n"
                        f"   Вход: {p.entry_price} | SL: {p.stop_price} | TP: {p.target_price}\n"
                        f"   Объём: {p.qty} лот | Свечей: {p.candles_held}"
                    )
            elif client.status == ClientStatus.ACTIVE:
                lines.append("\nОткрытых позиций нет.")

            recent = _get_recent_trades(db, client.id, limit=5)
            if recent:
                lines.append(f"\nПоследние {len(recent)} сделок:")
                for row in recent:
                    pnl = row["pnl"]
                    icon = "✅" if pnl >= 0 else "❌"
                    lines.append(
                        f"{icon} {row['ticker']} {row['direction']} "
                        f"P&L: {pnl:+.2f}₽ ({row['exit_reason']})"
                    )
        else:
            # Подписчик: количество позиций, тикеры (без направления), P&L за сегодня
            if client.status == ClientStatus.ACTIVE:
                pos_count = len(em.positions) if em else 0
                lines.append(f"\nОткрытых позиций: {pos_count}")
                if em and em.positions:
                    tickers = [p.ticker for p in em.positions.values()]
                    lines.append(f"Тикеры: {', '.join(tickers)}")

                # P&L за сегодня
                today_pnl = _get_today_pnl(db, client.id)
                if today_pnl is not None:
                    lines.append(f"\nP&L за сегодня: {today_pnl:+.2f} ₽")

        bot.reply_to(message, "\n".join(lines))

    # ------------------------------------------------------------------
    # /nickname — установить или изменить никнейм
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["nickname"])
    def handle_nickname(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return

        current = client.account_name or "(не задан)"
        fsm.set_onboarding(chat_id, OnboardingState.NICKNAME_UPDATE)
        bot.reply_to(
            message,
            f"Текущий никнейм: {current}\n\n"
            "Введите новый никнейм (2–32 символа):",
        )

    # ------------------------------------------------------------------
    # /mytoken — установить или обновить T-Bank токен
    # Для администраторов: добавить торговый токен (если нет) или заменить.
    # Для подписчиков: заменить токен без потери подписки (только active/paused).
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["mytoken"])
    def handle_mytoken(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return

        if client.role == ClientRole.ADMIN:
            action = "обновить" if client.tbank_token else "добавить"
            bot.reply_to(
                message,
                f"Введите T-Bank токен для торгового аккаунта ({action}).\n\n"
                f"{TOKEN_HOWTO}",
            )
            fsm.set_onboarding(chat_id, OnboardingState.TOKEN_UPDATE)
            return

        # Подписчик — только при активной или приостановленной подписке
        if client.status not in (ClientStatus.ACTIVE, ClientStatus.PAUSED):
            bot.reply_to(
                message,
                f"Смена токена доступна только активным подписчикам.\n"
                f"Текущий статус: {_status_label(client.status)}",
            )
            return

        bot.reply_to(
            message,
            "Введите новый T-Bank токен.\n"
            "Подписка и история сделок сохранятся.\n\n"
            f"{TOKEN_HOWTO}",
        )
        fsm.set_onboarding(chat_id, OnboardingState.TOKEN_UPDATE)

    # ------------------------------------------------------------------
    # /mybalance — текущий баланс портфеля
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["mybalance"])
    def handle_mybalance(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return
        if client.status != ClientStatus.ACTIVE:
            bot.reply_to(message, f"Команда доступна только при активной подписке.\nТекущий статус: {_status_label(client.status)}")
            return

        em = execs.get(client.id)
        if em is None:
            bot.reply_to(message, "Ваш аккаунт пока не подключён к торговому циклу.\nПопробуйте через минуту.")
            return

        try:
            balance = em.broker.get_portfolio_balance(em.account_id)
            bot.reply_to(message, f"💼 Баланс портфеля: {balance:,.2f} ₽")
        except Exception:
            bot.reply_to(message, "Не удалось получить баланс. Попробуйте позже.")

    # ------------------------------------------------------------------
    # /mystats — финансовая статистика (без деталей стратегии)
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["mystats"])
    def handle_mystats(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return
        if client.status not in (ClientStatus.ACTIVE, ClientStatus.PAUSED):
            bot.reply_to(message, f"Статистика доступна только активным подписчикам.\nТекущий статус: {_status_label(client.status)}")
            return

        stats = _get_client_financial_stats(db, client.id)
        if stats["total"] == 0:
            bot.reply_to(message, "📊 Сделок пока нет.")
            return

        lines = [
            "📊 Ваша статистика",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"Всего сделок: {stats['total']}",
            f"✅ Прибыльных: {stats['wins']} ({stats['win_rate']:.0f}%)",
            f"❌ Убыточных: {stats['losses']}",
            f"\nОбщий P&L: {stats['total_pnl']:+.2f} ₽",
            f"Средний P&L за сделку: {stats['avg_pnl']:+.2f} ₽",
            f"Лучшая сделка: {stats['best_pnl']:+.2f} ₽",
            f"Худшая сделка: {stats['worst_pnl']:+.2f} ₽",
        ]
        bot.reply_to(message, "\n".join(lines))

    # ------------------------------------------------------------------
    # /pause — клиент сам приостанавливает торговлю
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["pause"])
    def handle_pause(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return
        if client.status != ClientStatus.ACTIVE:
            bot.reply_to(message, f"Нельзя приостановить: статус {_status_label(client.status)}.")
            return

        registry.update_status(client.id, ClientStatus.PAUSED)
        # Убрать из цикла (sync_execs подхватит на следующей итерации,
        # но убираем сразу чтобы не открывались новые позиции)
        if client.id in execs:
            del execs[client.id]

        bot.reply_to(
            message,
            "⏸ Торговля приостановлена. Открытые позиции будут закрыты по SL/TP.\n"
            "Для возобновления — /resume",
        )
        logger.info("[BOT] /pause: client %d paused", client.id)

    # ------------------------------------------------------------------
    # /resume — возобновить после /pause
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["resume"])
    def handle_resume(message):
        chat_id = message.chat.id
        client = registry.get_by_chat_id(chat_id)
        if client is None:
            bot.reply_to(message, "Сначала отправьте /start.")
            return
        if client.status != ClientStatus.PAUSED:
            bot.reply_to(message, f"Нельзя возобновить: статус {_status_label(client.status)}.")
            return

        # Проверить срок подписки
        if client.paid_until and client.paid_until < datetime.now(timezone.utc):
            bot.reply_to(
                message,
                "❌ Подписка истекла. Продление через /pay или обратитесь к администратору.",
            )
            return

        registry.update_status(client.id, ClientStatus.ACTIVE)
        # sync_execs подхватит в следующий цикл (до 60 сек)
        bot.reply_to(
            message,
            "▶️ Торговля возобновлена. Бот начнёт открывать позиции в следующем цикле.",
        )
        logger.info("[BOT] /resume: client %d resumed", client.id)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _get_today_pnl(db: Database, client_id: int) -> float | None:
    """P&L клиента за сегодня (UTC day)."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with db.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total, COUNT(*) AS cnt "
            "FROM trades WHERE client_id = ? AND exit_time >= ?",
            (client_id, today_start.isoformat()),
        )
        row = cur.fetchone()
    if row and int(row["cnt"]) > 0:
        return float(row["total"])
    return None


def _get_client_financial_stats(db: Database, client_id: int) -> dict:
    """Финансовая статистика клиента (без деталей стратегии)."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(pnl), 2) AS avg_pnl,
                   ROUND(SUM(pnl), 2) AS total_pnl,
                   ROUND(MAX(pnl), 2) AS best_pnl,
                   ROUND(MIN(pnl), 2) AS worst_pnl
            FROM trades WHERE client_id = ?
            """,
            (client_id,),
        )
        row = cur.fetchone()
    total = int(row["total"]) if row and row["total"] else 0
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_pnl": 0.0, "total_pnl": 0.0, "best_pnl": 0.0, "worst_pnl": 0.0}
    wins = int(row["wins"] or 0)
    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": wins / total * 100,
        "avg_pnl": float(row["avg_pnl"] or 0),
        "total_pnl": float(row["total_pnl"] or 0),
        "best_pnl": float(row["best_pnl"] or 0),
        "worst_pnl": float(row["worst_pnl"] or 0),
    }


def _get_recent_trades(db: Database, client_id: int, limit: int = 5) -> list:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, direction, pnl, exit_reason, exit_time
            FROM trades
            WHERE client_id = ?
            ORDER BY exit_time DESC
            LIMIT ?
            """,
            (client_id, limit),
        )
        return cur.fetchall()


def _status_label(status: ClientStatus) -> str:
    return {
        ClientStatus.PENDING_PAYMENT: "ожидает оплаты",
        ClientStatus.PENDING_EMAIL: "настройка: ожидает email",
        ClientStatus.PENDING_TOKEN: "настройка: ожидает токен",
        ClientStatus.ACTIVE: "активен ✅",
        ClientStatus.PAUSED: "приостановлен ⏸",
        ClientStatus.EXPIRED: "подписка истекла ❌",
        ClientStatus.REVOKED: "доступ отозван 🚫",
    }.get(status, status.value)


def _status_hint(status: ClientStatus) -> str:
    return {
        ClientStatus.PENDING_PAYMENT: "Для доступа оформите подписку: /pay",
        ClientStatus.PENDING_EMAIL: "Продолжите настройку: /setup",
        ClientStatus.PENDING_TOKEN: "Продолжите настройку: /setup",
        ClientStatus.ACTIVE: "Торговля активна. /status — текущие позиции",
        ClientStatus.PAUSED: "Торговля приостановлена. /resume — возобновить",
        ClientStatus.EXPIRED: "Подписка истекла. /pay — продлить",
        ClientStatus.REVOKED: "Доступ отозван. По вопросам: @MakeRFGreatAgain",
    }.get(status, "")


def _client_help_text(status: ClientStatus) -> str:
    base = (
        "Доступные команды:\n"
        "  /start — регистрация\n"
        "  /pay — оформить/продлить подписку\n"
        "  /setup — настроить торговый токен\n"
        "  /nickname — изменить никнейм\n"
        "  /mytoken — заменить T-Bank токен\n"
        "  /status — текущие позиции и P&L\n"
        "  /mybalance — баланс портфеля\n"
        "  /mystats — ваша статистика\n"
        "  /pause — приостановить торговлю\n"
        "  /resume — возобновить торговлю\n"
        "  /help — эта справка"
    )
    hint = _status_hint(status)
    return f"{base}\n\nТекущий статус: {hint}" if hint else base


def _admin_help_text() -> str:
    return (
        "Команды администратора:\n\n"
        "Клиенты:\n"
        "  /clients — список всех клиентов\n"
        "  /client <id> — детали клиента\n"
        "  /grant <chat_id> <days> — подписка\n"
        "  /revoke <chat_id> — отозвать доступ\n"
        "  /reset_client <chat_id> — сброс профиля\n"
        "  /delete_client <chat_id> — удалить профиль\n"
        "  /pause_client / /resume_client <chat_id>\n\n"
        "Аналитика:\n"
        "  /admin — общая сводка\n"
        "  /today — итоги текущего дня\n"
        "  /positions — открытые позиции с P&L\n"
        "  /pnl_all [день|неделя|месяц] — P&L по клиентам\n"
        "  /stats [тикер] — статистика по тикеру\n"
        "  /equity [client_id] — кривая эквити\n"
        "  /balance_all — балансы клиентов\n\n"
        "Управление:\n"
        "  /config — текущая конфигурация\n"
        "  /logs [N] — последние строки лога\n"
        "  /nickname — изменить никнейм\n"
        "  /mytoken — T-Bank токен\n"
        "  /broadcast <текст> — рассылка\n"
        "  /export_trades <chat_id> — CSV сделок\n"
        "  /reload_clients — обновить реестр\n"
        "  /status — ваши позиции\n"
        "  /help — эта справка"
    )
