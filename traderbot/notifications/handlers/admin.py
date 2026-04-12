"""Административные Telegram-команды.

Все команды доступны только пользователям с role='admin' в БД.

/admin          — сводка: клиенты по статусам, P&L за сегодня
/clients        — список всех клиентов
/client <id|@name|email> — детали клиента
/grant <chat_id> <days> — выдать/продлить подписку
/revoke <chat_id>       — отозвать доступ (интерактивный диалог)
/reset_client <chat_id> — сбросить профиль до pending_payment (онбординг заново)
/delete_client <chat_id>— полное удаление профиля (с подтверждением)
/pause_client <chat_id> — приостановить
/resume_client <chat_id>— возобновить
/broadcast <текст>      — разослать всем активным
/today          — итоги текущего торгового дня (P&L, сделки, разбивка по тикерам)
/positions      — все открытые позиции с нереализованным P&L
/pnl_all [день|неделя|месяц] — P&L по клиентам
/balance_all    — балансы всех активных клиентов
/export_trades <chat_id> — выгрузить сделки клиента в CSV
/reload_clients — форсировать sync_execs
"""
from __future__ import annotations

import csv
import io
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import telebot

from traderbot.clients.db import Database
from traderbot.clients.models import ClientRole, ClientStatus
from traderbot.clients.registry import ClientRegistry
from traderbot.notifications.fsm import FSM, RevokeDialogState

logger = logging.getLogger(__name__)


def register(
    bot: telebot.TeleBot,
    registry: ClientRegistry,
    db: Database,
    execs: dict,
    fsm: FSM,
    notifier,           # TelegramNotifier
    reload_event: threading.Event,
) -> None:
    """Зарегистрировать все admin-хендлеры."""

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------

    def require_admin(message):
        """Вернуть клиента-админа или None (и отправить отказ)."""
        client = registry.get_by_chat_id(message.chat.id)
        if client is None or client.role != ClientRole.ADMIN:
            bot.reply_to(message, "⛔ Команда доступна только администраторам.")
            return None
        return client

    # ------------------------------------------------------------------
    # /admin — общая сводка
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["admin"])
    def handle_admin(message):
        if not require_admin(message):
            return

        all_clients = registry.list_all()
        admins = [c for c in all_clients if c.role.value == "admin"]
        subscribers = [c for c in all_clients if c.role.value != "admin"]

        by_status: dict[str, int] = {}
        for c in subscribers:
            by_status[c.status.value] = by_status.get(c.status.value, 0) + 1

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=6)
        pnl_today = _pnl_since(db, today_start)
        pnl_week = _pnl_since(db, week_start)

        # Позиции в цикле
        total_positions = sum(len(em.positions) for em in execs.values())

        labels = {
            "active": "активных", "paused": "на паузе",
            "pending_payment": "ожидают оплаты", "pending_email": "ожидают email",
            "pending_token": "ожидают токен", "expired": "истёк срок", "revoked": "отозваны",
        }

        lines = [
            "📊 Сводка TraderBot\n",
            f"Администраторы: {len(admins)}",
            f"Подписчики: {len(subscribers)}",
        ]
        if by_status:
            for status, count in sorted(by_status.items()):
                lines.append(f"  {labels.get(status, status)}: {count}")

        lines.append(f"\nВ торговом цикле: {len(execs)} аккаунтов")
        lines.append(f"Открытых позиций: {total_positions}")
        lines.append(f"\nP&L сегодня: {pnl_today:+.2f} ₽")
        lines.append(f"P&L за неделю: {pnl_week:+.2f} ₽")

        bot.reply_to(message, "\n".join(lines))

    # ------------------------------------------------------------------
    # /clients — список клиентов
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["clients"])
    def handle_clients(message):
        if not require_admin(message):
            return

        all_clients = registry.list_all()
        if not all_clients:
            bot.reply_to(message, "Клиентов нет.")
            return

        admins = [c for c in all_clients if c.role.value == "admin"]
        subscribers = [c for c in all_clients if c.role.value != "admin"]
        in_loop_ids = set(execs.keys())
        in_loop_count = sum(1 for c in all_clients if c.id in in_loop_ids)

        lines = [
            f"👥 Всего: {len(all_clients)} "
            f"(👑 {len(admins)} адм. / 👤 {len(subscribers)} подп.) "
            f"| ▶ в цикле: {in_loop_count}\n"
        ]

        # Сначала администраторы, потом подписчики
        for section_label, section_clients in [("— Администраторы —", admins), ("— Подписчики —", subscribers)]:
            if not section_clients:
                continue
            lines.append(section_label)
            for c in section_clients:
                role_icon = "👑" if c.role.value == "admin" else "👤"
                em = execs.get(c.id)
                name = c.account_name or c.email or "—"

                # Строка 1: роль, id, имя, статус
                status_icon = _status_icon(c.status)
                status_label = _status_label_ru(c.status)
                loop_mark = f" ▶{len(em.positions)}п" if em else ""
                has_token = "🔑" if c.tbank_token else "🚫"
                line1 = f"{role_icon}[{c.id}] {name} {status_icon}{status_label}{loop_mark} {has_token}"

                # Строка 2: chat_id, подписка, ошибки
                details = [f"💬{c.tg_chat_id}"]
                if c.paid_until:
                    details.append(f"📅до {c.paid_until.strftime('%d.%m.%y')}")
                if c.consecutive_errors > 0:
                    details.append(f"⚠️ошибок:{c.consecutive_errors}")
                line2 = "   " + " | ".join(details)

                lines.append(line1)
                lines.append(line2)

        _send_long(bot, message.chat.id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /client <id|@nickname|email> — детали конкретного клиента
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["client"])
    def handle_client_detail(message):
        if not require_admin(message):
            return

        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Использование: /client <id | chat_id | @никнейм | email>")
            return

        query = args[1]
        client = _resolve_client(query, registry)
        if client is None:
            bot.reply_to(
                message,
                f"Клиент «{query}» не найден.\n"
                "Допустимые форматы: числовой ID, chat_id, @никнейм, email.",
            )
            return

        lines = [
            f"Клиент #{client.id}",
            f"Никнейм: {client.account_name or '—'}",
            f"chat_id: {client.tg_chat_id}",
            f"Роль: {client.role.value}",
            f"Статус: {client.status.value}",
            f"Email: {client.email or '—'}",
            f"Аккаунт T-Bank: {client.tbank_account_id or '—'}",
            f"Токен: {'✅ есть' if client.tbank_token else '❌ нет'}",
            f"Подписка до: {client.paid_until.strftime('%d.%m.%Y') if client.paid_until else '∞'}",
            f"Ошибок подряд: {client.consecutive_errors}",
            f"В цикле: {'да ▶' if client.id in execs else 'нет'}",
        ]

        # Открытые позиции
        em = execs.get(client.id)
        if em and em.positions:
            lines.append(f"\nОткрытых позиций: {len(em.positions)}")
            for p in em.positions.values():
                lines.append(
                    f"  {'🟢' if p.status == 'active' else '📋'} {p.ticker} "
                    f"{p.direction.value} вход={p.entry_price} qty={p.qty}"
                )
        else:
            lines.append("\nОткрытых позиций: 0")

        # Баланс
        if em:
            try:
                balance = em.broker.get_portfolio_balance(em.account_id)
                lines.append(f"Баланс: {balance:,.2f} ₽")
            except Exception:
                lines.append("Баланс: ошибка запроса")

        # Статистика сделок
        stats = _get_client_stats(db, client.id)
        if stats["total"] > 0:
            lines.append(
                f"\nСтатистика (всего {stats['total']} сделок):\n"
                f"  Прибыльных: {stats['wins']} ({stats['win_rate']:.0f}%)\n"
                f"  Средний P&L: {stats['avg_pnl']:+.2f} ₽\n"
                f"  Суммарный P&L: {stats['total_pnl']:+.2f} ₽\n"
                f"  Лучшая: {stats['best_pnl']:+.2f} ₽ ({stats['best_ticker']})\n"
                f"  Худшая: {stats['worst_pnl']:+.2f} ₽ ({stats['worst_ticker']})\n"
                f"  Макс. просадка: -{stats['max_drawdown']:.2f} ₽"
            )

        # Последние 10 сделок
        recent = _get_trades(db, client.id, limit=10)
        if recent:
            lines.append(f"\nПоследние {len(recent)} сделок:")
            for row in recent:
                pnl = row["pnl"]
                icon = "✅" if pnl >= 0 else "❌"
                lines.append(
                    f"  {icon} {row['ticker']} {row['direction']} "
                    f"{pnl:+.2f}₽ ({row['exit_reason']})"
                )

        _send_long(bot, message.chat.id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /grant <chat_id> <days> — выдать/продлить подписку
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["grant"])
    def handle_grant(message):
        if not require_admin(message):
            return

        parts = message.text.split()
        if len(parts) < 3 or not parts[1].lstrip("-").isdigit() or not parts[2].isdigit():
            bot.reply_to(message, "Использование: /grant <chat_id> <дней>")
            return

        tg_chat_id = int(parts[1])
        days = int(parts[2])

        # Найти или создать клиента
        client = registry.get_by_chat_id(tg_chat_id)
        if client is None:
            client, _ = registry.get_or_create_subscriber(tg_chat_id, ClientStatus.PENDING_EMAIL)

        # Рассчитать новую дату
        now = datetime.now(timezone.utc)
        base = max(client.paid_until or now, now)
        new_paid_until = base + timedelta(days=days)

        registry.set_paid_until(client.id, new_paid_until)

        # Перевести в pending_email если ещё не прошёл онбординг
        if client.status in (ClientStatus.PENDING_PAYMENT,):
            registry.update_status(client.id, ClientStatus.PENDING_EMAIL)

        # Создать запись об оплате
        _record_payment(db, client.id, days)

        bot.reply_to(
            message,
            f"✅ Подписка выдана:\n"
            f"Клиент #{client.id} (chat_id={tg_chat_id})\n"
            f"До: {new_paid_until.strftime('%d.%m.%Y')}\n"
            f"Дней добавлено: {days}",
        )

        # Уведомить клиента
        try:
            updated = registry.get_by_id(client.id)
            if updated and updated.status == ClientStatus.PENDING_EMAIL:
                bot.send_message(
                    tg_chat_id,
                    f"✅ Ваша подписка активирована до {new_paid_until.strftime('%d.%m.%Y')}!\n\n"
                    "Для начала торговли необходимо указать email и T-Bank токен.\n"
                    "Отправьте /setup чтобы продолжить.",
                )
            else:
                bot.send_message(
                    tg_chat_id,
                    f"✅ Подписка продлена до {new_paid_until.strftime('%d.%m.%Y')}.",
                )
        except Exception:
            logger.exception("[ADMIN] Failed to notify client %d about grant", client.id)

        logger.info("[ADMIN] Grant: client %d (%d days, until %s)", client.id, days, new_paid_until)

    # ------------------------------------------------------------------
    # /revoke <chat_id> — интерактивный диалог
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["revoke"])
    def handle_revoke(message):
        if not require_admin(message):
            return

        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            bot.reply_to(message, "Использование: /revoke <chat_id>")
            return

        tg_chat_id = int(parts[1])
        client = registry.get_by_chat_id(tg_chat_id)
        if client is None:
            bot.reply_to(message, f"Клиент с chat_id={tg_chat_id} не найден.")
            return
        if client.status == ClientStatus.REVOKED:
            bot.reply_to(message, f"Клиент #{client.id} уже отозван.")
            return

        # Собрать сводку открытых позиций
        em = execs.get(client.id)
        pos_lines = []
        if em and em.positions:
            for p in em.positions.values():
                pos_lines.append(
                    f"  • {p.ticker} {p.direction.value} {p.qty} лот "
                    f"@ {p.entry_price} (SL {p.stop_price}, TP {p.target_price})"
                )
        pos_summary = "\n".join(pos_lines) if pos_lines else "  нет открытых позиций"

        # Сохранить состояние диалога в FSM
        fsm.set_revoke_dialog(
            message.chat.id,
            RevokeDialogState(
                target_client_id=client.id,
                positions_summary=pos_summary,
            ),
        )

        text = (
            f"Отзыв доступа:\n"
            f"Клиент #{client.id} (chat_id={tg_chat_id}, email={client.email or '—'})\n\n"
            f"Открытые позиции:\n{pos_summary}\n\n"
            "Что сделать с позициями?"
        )
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton(
                "🔴 Закрыть все по рынку", callback_data=f"revoke:close:{client.id}"
            ),
            telebot.types.InlineKeyboardButton(
                "⏳ Оставить до SL/TP", callback_data=f"revoke:wait:{client.id}"
            ),
            telebot.types.InlineKeyboardButton(
                "❌ Отменить", callback_data=f"revoke:cancel:{client.id}"
            ),
        )
        bot.reply_to(message, text, reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("revoke:"))
    def handle_revoke_callback(call):
        parts = call.data.split(":")
        if len(parts) != 3:
            return
        _, action, client_id_str = parts
        admin_chat_id = call.message.chat.id

        # Убрать inline-кнопки
        try:
            bot.edit_message_reply_markup(admin_chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        client = registry.get_by_id(int(client_id_str))
        if client is None:
            bot.answer_callback_query(call.id, "Клиент не найден.")
            return

        fsm.clear_revoke_dialog(admin_chat_id)

        if action == "cancel":
            bot.answer_callback_query(call.id)
            bot.send_message(admin_chat_id, "Отмена. Статус клиента не изменён.")
            return

        if action == "close":
            # Закрыть все позиции по рынку
            em = execs.get(client.id)
            closed = 0
            if em:
                for figi in list(em.positions.keys()):
                    try:
                        p = em.positions[figi]
                        # entry_price как приближение для P&L журнала;
                        # реальная цена выхода определяется биржей при исполнении рыночного ордера
                        em._close_position(figi, p.entry_price, "revoked")
                        closed += 1
                    except Exception:
                        logger.exception("[ADMIN] revoke: failed to close %s for client %d",
                                         figi, client.id)
                del execs[client.id]

            registry.update_status(client.id, ClientStatus.REVOKED)
            registry.delete_token(client.id)

            bot.answer_callback_query(call.id)
            bot.send_message(
                admin_chat_id,
                f"✅ Доступ клиента #{client.id} отозван.\n"
                f"Закрыто позиций: {closed}",
            )
            _notify_client_revoked(bot, client.tg_chat_id)
            logger.info("[ADMIN] Revoked client %d (close all)", client.id)

        elif action == "wait":
            # Оставить позиции до SL/TP, перестать открывать новые
            em = execs.get(client.id)
            if em:
                em._revoked = True  # main loop не будет открывать новые позиции

            registry.update_status(client.id, ClientStatus.REVOKED)

            bot.answer_callback_query(call.id)
            bot.send_message(
                admin_chat_id,
                f"✅ Доступ клиента #{client.id} отозван.\n"
                "Открытые позиции будут закрыты по SL/TP, новые не открываются.",
            )
            _notify_client_revoked(bot, client.tg_chat_id, with_positions=True)
            logger.info("[ADMIN] Revoked client %d (wait SL/TP)", client.id)

    # ------------------------------------------------------------------
    # /reset_client <chat_id> — сброс профиля до pending_payment
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["reset_client"])
    def handle_reset_client(message):
        if not require_admin(message):
            return

        client, error = _get_client_from_args(message, registry, bot)
        if client is None:
            return

        if client.role == ClientRole.ADMIN:
            bot.reply_to(message, "⛔ Нельзя сбросить аккаунт администратора.")
            return

        execs.pop(client.id, None)
        registry.reset_subscriber(client.id)

        bot.reply_to(
            message,
            f"🔄 Клиент #{client.id} сброшен.\n"
            "Статус: pending_payment. Email, токен и подписка очищены.\n"
            "После /grant клиент пройдёт онбординг заново.",
        )
        try:
            bot.send_message(
                client.tg_chat_id,
                "🔄 Ваш профиль сброшен администратором.\n"
                "Для возобновления торговли потребуется новая подписка.\n"
                "По вопросам: @MakeRFGreatAgain",
            )
        except Exception:
            pass
        logger.info("[ADMIN] reset_client: client %d reset", client.id)

    # ------------------------------------------------------------------
    # /delete_client <chat_id> — полное удаление профиля
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["delete_client"])
    def handle_delete_client(message):
        if not require_admin(message):
            return

        client, error = _get_client_from_args(message, registry, bot)
        if client is None:
            return

        if client.role == ClientRole.ADMIN:
            bot.reply_to(message, "⛔ Нельзя удалить аккаунт администратора.")
            return

        em = execs.get(client.id)
        pos_warning = ""
        if em and em.positions:
            pos_warning = f"\n⚠️ Открытых позиций: {len(em.positions)} — будут закрыты по рынку!"

        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton(
                "🗑 Да, удалить профиль", callback_data=f"delete_client:confirm:{client.id}"
            ),
            telebot.types.InlineKeyboardButton(
                "❌ Отменить", callback_data=f"delete_client:cancel:{client.id}"
            ),
        )
        bot.reply_to(
            message,
            f"Удаление профиля клиента #{client.id}:\n"
            f"chat_id: {client.tg_chat_id}\n"
            f"Никнейм: {client.account_name or '—'}\n"
            f"Email: {client.email or '—'}\n"
            f"Статус: {client.status.value}{pos_warning}\n\n"
            "Это действие необратимо. Подтвердите:",
            reply_markup=markup,
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("delete_client:"))
    def handle_delete_client_callback(call):
        parts = call.data.split(":")
        if len(parts) != 3:
            return
        _, action, client_id_str = parts
        admin_chat_id = call.message.chat.id

        try:
            bot.edit_message_reply_markup(admin_chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        if action == "cancel":
            bot.answer_callback_query(call.id)
            bot.send_message(admin_chat_id, "Отмена. Профиль не изменён.")
            return

        client = registry.get_by_id(int(client_id_str))
        if client is None:
            bot.answer_callback_query(call.id, "Клиент не найден.")
            return

        em = execs.pop(client.id, None)
        closed = 0
        if em:
            for figi in list(em.positions.keys()):
                try:
                    p = em.positions[figi]
                    em._close_position(figi, p.entry_price, "deleted")
                    closed += 1
                except Exception:
                    logger.exception("[ADMIN] delete_client: failed to close %s for client %d",
                                     figi, client.id)

        tg_chat_id = client.tg_chat_id
        client_id = client.id
        registry.delete(client_id)

        bot.answer_callback_query(call.id)
        result_msg = f"🗑 Профиль клиента #{client_id} полностью удалён."
        if closed:
            result_msg += f"\nЗакрыто позиций: {closed}"
        bot.send_message(admin_chat_id, result_msg)
        try:
            bot.send_message(
                tg_chat_id,
                "🚫 Ваш профиль в TraderBot удалён администратором.\n"
                "По вопросам: @MakeRFGreatAgain",
            )
        except Exception:
            pass
        logger.info("[ADMIN] delete_client: client %d deleted", client_id)

    # ------------------------------------------------------------------
    # /pause_client <chat_id>
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["pause_client"])
    def handle_pause_client(message):
        if not require_admin(message):
            return

        client, error = _get_client_from_args(message, registry, bot)
        if client is None:
            return

        if client.status not in (ClientStatus.ACTIVE,):
            bot.reply_to(message, f"Клиент #{client.id}: нельзя приостановить (статус {client.status.value}).")
            return

        registry.update_status(client.id, ClientStatus.PAUSED)
        execs.pop(client.id, None)

        bot.reply_to(message, f"⏸ Клиент #{client.id} приостановлен.")
        try:
            bot.send_message(client.tg_chat_id,
                             "⏸ Ваша торговля приостановлена администратором.")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # /resume_client <chat_id>
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["resume_client"])
    def handle_resume_client(message):
        if not require_admin(message):
            return

        client, error = _get_client_from_args(message, registry, bot)
        if client is None:
            return

        if client.status != ClientStatus.PAUSED:
            bot.reply_to(message, f"Клиент #{client.id}: нельзя возобновить (статус {client.status.value}).")
            return

        registry.update_status(client.id, ClientStatus.ACTIVE)
        bot.reply_to(message, f"▶️ Клиент #{client.id} возобновлён. Подхватится в следующем цикле.")
        try:
            bot.send_message(client.tg_chat_id,
                             "▶️ Торговля возобновлена администратором.")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # /broadcast <текст>
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["broadcast"])
    def handle_broadcast(message):
        if not require_admin(message):
            return

        text = message.text[len("/broadcast"):].strip()
        if not text:
            bot.reply_to(message, "Использование: /broadcast <текст>")
            return

        notifier.send_to_all_active(f"📢 {text}")
        active_count = len(registry.list_active())
        bot.reply_to(message, f"✅ Отправлено {active_count} активным клиентам.")

    # ------------------------------------------------------------------
    # /pnl_all [день|неделя|месяц]
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["pnl_all"])
    def handle_pnl_all(message):
        if not require_admin(message):
            return

        parts = message.text.split()
        period = parts[1].lower() if len(parts) > 1 else "день"
        period_map = {"день": 1, "неделя": 7, "месяц": 30,
                      "day": 1, "week": 7, "month": 30}
        days = period_map.get(period, 1)

        since = datetime.now(timezone.utc) - timedelta(days=days)
        period_label = {1: "сегодня", 7: "неделя", 30: "месяц"}.get(days, f"{days}д")

        rows = _pnl_by_client(db, since)
        total = sum(r["total_pnl"] for r in rows)

        if not rows:
            bot.reply_to(message, f"Сделок за период «{period_label}» нет.")
            return

        lines = [f"P&L за {period_label} (все клиенты): {total:+.2f} ₽\n"]
        for row in rows:
            client = registry.get_by_id(row["client_id"])
            name = client.email or f"#{row['client_id']}" if client else f"#{row['client_id']}"
            lines.append(
                f"  {name}: {row['total_pnl']:+.2f} ₽ "
                f"({row['trades']} сделок)"
            )

        _send_long(bot, message.chat.id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /today — итоги текущего торгового дня
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["today"])
    def handle_today(message):
        if not require_admin(message):
            return
        MSK = ZoneInfo("Europe/Moscow")
        now_msk = datetime.now(timezone.utc).astimezone(MSK)
        text = _build_daily_report(db, registry, now_msk)
        _send_long(bot, message.chat.id, text)

    # ------------------------------------------------------------------
    # /positions — открытые позиции с нереализованным P&L
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["positions"])
    def handle_positions(message):
        if not require_admin(message):
            return

        if not execs:
            bot.reply_to(message, "Нет активных клиентов в торговом цикле.")
            return

        has_any = any(em.positions for em in execs.values())
        if not has_any:
            bot.reply_to(message, "Открытых позиций нет.")
            return

        lines = ["📊 Открытые позиции\n"]
        total_unrealized = 0.0

        for client_id, em in execs.items():
            if not em.positions:
                continue
            client = registry.get_by_id(client_id)
            name = (client.account_name or client.email or f"#{client_id}") if client else f"#{client_id}"
            lines.append(f"— {name} —")

            for figi, pos in em.positions.items():
                # Получить текущую цену
                current_price = None
                try:
                    current_price = em.broker.get_last_price(figi)
                except Exception:
                    logger.debug("[ADMIN] positions: get_last_price failed for %s", figi)

                if current_price is not None:
                    shares = pos.qty * pos.lot_size
                    if pos.direction.value == "BUY":
                        unrealized = (current_price - pos.entry_price) * shares
                    else:
                        unrealized = (pos.entry_price - current_price) * shares
                    total_unrealized += unrealized
                    pnl_str = f"{unrealized:+.2f} ₽"
                    price_str = f"Тек: {current_price:.2f}"
                else:
                    pnl_str = "—"
                    price_str = "цена недоступна"

                status_icon = "🟢" if pos.status == "active" else "📋"
                lines.append(
                    f"  {status_icon} {pos.ticker} {pos.direction.value} | {pos.qty} лот. | {pnl_str}\n"
                    f"    Вход: {pos.entry_price:.2f} | {price_str}\n"
                    f"    SL: {pos.stop_price:.2f} | TP: {pos.target_price:.2f}\n"
                    f"    Свечей: {pos.candles_held} | Причина: {pos.entry_reason or '—'}"
                )

        lines.append(f"\nНереализованный P&L: {total_unrealized:+.2f} ₽")
        _send_long(bot, message.chat.id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /balance_all
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["balance_all"])
    def handle_balance_all(message):
        if not require_admin(message):
            return

        if not execs:
            bot.reply_to(message, "Нет активных клиентов в торговом цикле.")
            return

        lines = [f"Балансы активных клиентов ({len(execs)}):\n"]
        total = 0.0
        for client_id, em in execs.items():
            client = registry.get_by_id(client_id)
            name = (client.email or f"#{client_id}") if client else f"#{client_id}"
            try:
                balance = em.broker.get_portfolio_balance(em.account_id)
                total += balance
                lines.append(f"  {name}: {balance:,.2f} ₽")
            except Exception:
                lines.append(f"  {name}: ошибка запроса")

        lines.append(f"\nИтого: {total:,.2f} ₽")
        _send_long(bot, message.chat.id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /export_trades <chat_id> — CSV-выгрузка сделок клиента
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["export_trades"])
    def handle_export_trades(message):
        if not require_admin(message):
            return

        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Использование: /export_trades <chat_id | @никнейм | email>")
            return

        client = _resolve_client(args[1], registry)
        if client is None:
            bot.reply_to(message, f"Клиент «{args[1]}» не найден.")
            return

        all_trades = _get_trades(db, client.id, limit=0)
        if not all_trades:
            bot.reply_to(message, f"Сделок для клиента #{client.id} не найдено.")
            return

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "ticker", "figi", "direction",
            "entry_price", "exit_price", "stop_price", "target_price",
            "qty", "pnl", "commission",
            "entry_time", "exit_time", "entry_reason", "exit_reason", "candles_held",
        ])
        for row in all_trades:
            writer.writerow([
                row["id"], row["ticker"], row["figi"], row["direction"],
                row["entry_price"], row["exit_price"], row["stop_price"], row["target_price"],
                row["qty"], row["pnl"], row["commission"],
                row["entry_time"], row["exit_time"], row["entry_reason"], row["exit_reason"],
                row["candles_held"],
            ])

        csv_bytes = buf.getvalue().encode("utf-8-sig")  # utf-8-sig для корректного открытия в Excel
        name = client.account_name or client.email or str(client.id)
        filename = f"trades_{name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"

        bot.send_document(
            message.chat.id,
            telebot.types.InputFile(io.BytesIO(csv_bytes), file_name=filename),
            caption=f"Сделки клиента #{client.id} ({name}): {len(all_trades)} записей",
        )
        logger.info("[ADMIN] export_trades: client %d, %d rows", client.id, len(all_trades))

    # ------------------------------------------------------------------
    # /reload_clients
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["reload_clients"])
    def handle_reload_clients(message):
        if not require_admin(message):
            return

        reload_event.set()
        bot.reply_to(message, "✅ Сигнал обновления реестра отправлен.\nОбновление произойдёт в течение нескольких секунд.")

    # ------------------------------------------------------------------
    # /stats [тикер] — статистика по тикеру
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["stats"])
    def handle_stats(message):
        if not require_admin(message):
            return

        parts = message.text.split()
        if len(parts) < 2:
            # Общая разбивка по всем тикерам
            rows = _stats_all_tickers(db)
            if not rows:
                bot.reply_to(message, "Сделок нет.")
                return
            lines = ["📈 Статистика по тикерам\n"]
            for r in rows:
                wr = r["wins"] / r["total"] * 100 if r["total"] else 0
                icon = "✅" if float(r["total_pnl"]) >= 0 else "❌"
                lines.append(
                    f"{icon} {r['ticker']}: {float(r['total_pnl']):+.2f} ₽ | "
                    f"{r['total']} сд. | WR {wr:.0f}% | "
                    f"ср. {float(r['avg_pnl']):+.2f} ₽"
                )
            _send_long(bot, message.chat.id, "\n".join(lines))
        else:
            ticker = parts[1].upper()
            stats = _stats_for_ticker(db, ticker)
            if stats["total"] == 0:
                bot.reply_to(message, f"Сделок по {ticker} не найдено.")
                return
            wr = stats["wins"] / stats["total"] * 100
            lines = [
                f"📈 Статистика {ticker}",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"Всего сделок: {stats['total']}",
                f"✅ Прибыльных: {stats['wins']} ({wr:.0f}%)",
                f"❌ Убыточных: {stats['total'] - stats['wins']}",
                f"\nОбщий P&L: {stats['total_pnl']:+.2f} ₽",
                f"Средний P&L: {stats['avg_pnl']:+.2f} ₽",
                f"Лучшая: {stats['best_pnl']:+.2f} ₽",
                f"Худшая: {stats['worst_pnl']:+.2f} ₽",
                f"Средняя длительность: {stats['avg_candles']:.1f} свечей",
            ]
            bot.reply_to(message, "\n".join(lines))

    # ------------------------------------------------------------------
    # /equity [client_id] — кривая эквити (PNG)
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["equity"])
    def handle_equity(message):
        if not require_admin(message):
            return

        parts = message.text.split()
        client_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

        png = _build_equity_chart(db, client_id)
        if png is None:
            bot.reply_to(message, "Недостаточно данных для построения графика.")
            return

        label = f"клиента #{client_id}" if client_id else "всех клиентов"
        bot.send_photo(
            message.chat.id,
            telebot.types.InputFile(png, file_name="equity.png"),
            caption=f"📈 Кривая эквити {label}",
        )

    # ------------------------------------------------------------------
    # /config — текущая конфигурация
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["config"])
    def handle_config(message):
        if not require_admin(message):
            return

        import yaml
        from traderbot.config import load_config as _lc
        import pathlib

        try:
            config_path = str(pathlib.Path(__file__).parent.parent.parent / "config.yaml")
            with open(config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except Exception:
            bot.reply_to(message, "Ошибка чтения config.yaml.")
            return

        lines = ["⚙️ Конфигурация\n"]

        # Тикеры
        tickers = raw.get("tickers", {})
        lines.append(f"Тикеры ({len(tickers)}):")
        for name, conf in tickers.items():
            lines.append(f"  {name}: {conf.get('strategy', '—')}")

        # Риск
        risk = raw.get("risk", {})
        lines.append(f"\nРиск:")
        lines.append(f"  risk_pct: {risk.get('risk_pct', '—')}")
        lines.append(f"  max_position_pct: {risk.get('max_position_pct', '—')}")
        lines.append(f"  max_consecutive_sl: {risk.get('max_consecutive_sl', '—')}")

        # Trading
        trading = raw.get("trading", {})
        lines.append(f"\nТрейдинг:")
        lines.append(f"  poll_interval: {trading.get('poll_interval_sec', '—')}s")
        lines.append(f"  max_candles_timeout: {trading.get('max_candles_timeout', '—')}")
        lines.append(f"  commission_pct: {trading.get('commission_pct', '—')}")

        _send_long(bot, message.chat.id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /logs [N] — последние строки лога
    # ------------------------------------------------------------------

    @bot.message_handler(commands=["logs"])
    def handle_logs(message):
        if not require_admin(message):
            return

        parts = message.text.split()
        count = 30
        if len(parts) > 1 and parts[1].isdigit():
            count = min(int(parts[1]), 100)

        try:
            with open("logs/bot.log", "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            tail = all_lines[-count:]
            text = "".join(tail)
            if not text.strip():
                bot.reply_to(message, "Лог пуст.")
                return
            _send_long(bot, message.chat.id, f"📄 Последние {len(tail)} строк:\n\n{text}")
        except FileNotFoundError:
            bot.reply_to(message, "Файл лога не найден.")
        except Exception:
            bot.reply_to(message, "Ошибка чтения лога.")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _get_client_from_args(message, registry, bot):
    """Распарсить chat_id из аргументов команды и найти клиента."""
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, f"Использование: {parts[0]} <chat_id>")
        return None, "bad_args"
    tg_chat_id = int(parts[1])
    client = registry.get_by_chat_id(tg_chat_id)
    if client is None:
        bot.reply_to(message, f"Клиент с chat_id={tg_chat_id} не найден.")
        return None, "not_found"
    return client, None


def _notify_client_revoked(bot, tg_chat_id: int, with_positions: bool = False) -> None:
    try:
        if with_positions:
            msg = ("🚫 Ваш доступ к TraderBot отозван.\n"
                   "Открытые позиции будут автоматически закрыты по SL/TP.\n"
                   "По вопросам: @MakeRFGreatAgain")
        else:
            msg = ("🚫 Ваш доступ к TraderBot отозван.\n"
                   "По вопросам: @MakeRFGreatAgain")
        bot.send_message(tg_chat_id, msg)
    except Exception:
        logger.debug("[ADMIN] Could not notify revoked client chat_id=%d", tg_chat_id)


def _send_long(bot, chat_id: int, text: str, chunk: int = 4000) -> None:
    """Разбить длинное сообщение на части по ≤4000 символов."""
    for i in range(0, len(text), chunk):
        bot.send_message(chat_id, text[i:i + chunk])


def _pnl_since(db: Database, since: datetime) -> float:
    with db.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE exit_time >= ?",
            (since.isoformat(),),
        )
        row = cur.fetchone()
    return float(row["total"]) if row else 0.0


def _pnl_by_client(db: Database, since: datetime) -> list:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT client_id,
                   ROUND(SUM(pnl), 2) AS total_pnl,
                   COUNT(*) AS trades
            FROM trades
            WHERE exit_time >= ?
            GROUP BY client_id
            ORDER BY total_pnl DESC
            """,
            (since.isoformat(),),
        )
        return cur.fetchall()


def _get_trades(db: Database, client_id: int, limit: int = 10) -> list:
    """limit=0 — без ограничения (все сделки, полные колонки)."""
    with db.cursor() as cur:
        if limit > 0:
            cur.execute(
                """
                SELECT id, ticker, figi, direction, entry_price, exit_price,
                       stop_price, target_price, qty, pnl, commission,
                       entry_time, exit_time, entry_reason, exit_reason, candles_held
                FROM trades
                WHERE client_id = ?
                ORDER BY exit_time DESC
                LIMIT ?
                """,
                (client_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, ticker, figi, direction, entry_price, exit_price,
                       stop_price, target_price, qty, pnl, commission,
                       entry_time, exit_time, entry_reason, exit_reason, candles_held
                FROM trades
                WHERE client_id = ?
                ORDER BY exit_time DESC
                """,
                (client_id,),
            )
        return cur.fetchall()


def _resolve_client(query: str, registry: ClientRegistry):
    """Найти клиента по числовому ID, chat_id, @никнейму или email."""
    # Числовой запрос: сначала внутренний id, потом chat_id
    stripped = query.lstrip("@")
    if query.lstrip("-").isdigit():
        num = int(query)
        client = registry.get_by_id(num)
        if client is None:
            client = registry.get_by_chat_id(num)
        return client
    # @nickname или просто nickname → поиск по account_name
    if query.startswith("@") or not "@" in query:
        return registry.find_by_name(stripped)
    # email (содержит @, но не начинается с @)
    return registry.find_by_email(query)


def _get_client_stats(db: Database, client_id: int) -> dict:
    """Агрегированная торговая статистика клиента за всё время."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      AS wins,
                ROUND(AVG(pnl), 2)                             AS avg_pnl,
                ROUND(SUM(pnl), 2)                             AS total_pnl,
                ROUND(MAX(pnl), 2)                             AS best_pnl,
                ROUND(MIN(pnl), 2)                             AS worst_pnl,
                ticker AS best_ticker
            FROM trades WHERE client_id = ?
            """,
            (client_id,),
        )
        agg = cur.fetchone()

        # Тикеры лучшей и худшей сделки
        cur.execute(
            "SELECT ticker FROM trades WHERE client_id = ? ORDER BY pnl DESC LIMIT 1",
            (client_id,),
        )
        row = cur.fetchone()
        best_ticker = row["ticker"] if row else "—"

        cur.execute(
            "SELECT ticker FROM trades WHERE client_id = ? ORDER BY pnl ASC LIMIT 1",
            (client_id,),
        )
        row = cur.fetchone()
        worst_ticker = row["ticker"] if row else "—"

        # Для макс. просадки нужны сделки в хронологическом порядке
        cur.execute(
            "SELECT pnl FROM trades WHERE client_id = ? ORDER BY exit_time ASC",
            (client_id,),
        )
        pnl_rows = cur.fetchall()

    total = int(agg["total"]) if agg and agg["total"] else 0
    if total == 0:
        return {
            "total": 0, "wins": 0, "win_rate": 0.0,
            "avg_pnl": 0.0, "total_pnl": 0.0,
            "best_pnl": 0.0, "best_ticker": "—",
            "worst_pnl": 0.0, "worst_ticker": "—",
            "max_drawdown": 0.0,
        }

    wins = int(agg["wins"] or 0)
    return {
        "total": total,
        "wins": wins,
        "win_rate": wins / total * 100,
        "avg_pnl": float(agg["avg_pnl"] or 0),
        "total_pnl": float(agg["total_pnl"] or 0),
        "best_pnl": float(agg["best_pnl"] or 0),
        "best_ticker": best_ticker,
        "worst_pnl": float(agg["worst_pnl"] or 0),
        "worst_ticker": worst_ticker,
        "max_drawdown": _max_drawdown([r["pnl"] for r in pnl_rows]),
    }


def _max_drawdown(pnl_series: list[float]) -> float:
    """Максимальная просадка от пика по накопленной кривой P&L."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_series:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _status_icon(status: ClientStatus) -> str:
    return {
        ClientStatus.ACTIVE: "✅",
        ClientStatus.PAUSED: "⏸",
        ClientStatus.PENDING_PAYMENT: "⏳",
        ClientStatus.PENDING_EMAIL: "📧",
        ClientStatus.PENDING_TOKEN: "🔑",
        ClientStatus.EXPIRED: "❌",
        ClientStatus.REVOKED: "🚫",
    }.get(status, "•")


def _status_short(status: ClientStatus) -> str:
    return {
        ClientStatus.ACTIVE: "active",
        ClientStatus.PAUSED: "paused",
        ClientStatus.PENDING_PAYMENT: "pending_payment",
        ClientStatus.PENDING_EMAIL: "pending_email",
        ClientStatus.PENDING_TOKEN: "pending_token",
        ClientStatus.EXPIRED: "expired",
        ClientStatus.REVOKED: "revoked",
    }.get(status, status.value)


def _status_label_ru(status: ClientStatus) -> str:
    return {
        ClientStatus.ACTIVE: "активен",
        ClientStatus.PAUSED: "пауза",
        ClientStatus.PENDING_PAYMENT: "ждёт оплаты",
        ClientStatus.PENDING_EMAIL: "ждёт email",
        ClientStatus.PENDING_TOKEN: "ждёт токен",
        ClientStatus.EXPIRED: "истёк",
        ClientStatus.REVOKED: "отозван",
    }.get(status, status.value)


def _stats_all_tickers(db: Database) -> list:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT ticker,
                   COUNT(*) AS total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(pnl), 2) AS total_pnl,
                   ROUND(AVG(pnl), 2) AS avg_pnl
            FROM trades
            GROUP BY ticker
            ORDER BY total_pnl DESC
            """
        )
        return cur.fetchall()


def _stats_for_ticker(db: Database, ticker: str) -> dict:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(pnl), 2) AS total_pnl,
                   ROUND(AVG(pnl), 2) AS avg_pnl,
                   ROUND(MAX(pnl), 2) AS best_pnl,
                   ROUND(MIN(pnl), 2) AS worst_pnl,
                   ROUND(AVG(candles_held), 1) AS avg_candles
            FROM trades WHERE ticker = ?
            """,
            (ticker,),
        )
        row = cur.fetchone()
    total = int(row["total"]) if row and row["total"] else 0
    if total == 0:
        return {"total": 0, "wins": 0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "best_pnl": 0.0, "worst_pnl": 0.0, "avg_candles": 0.0}
    return {
        "total": total,
        "wins": int(row["wins"] or 0),
        "total_pnl": float(row["total_pnl"] or 0),
        "avg_pnl": float(row["avg_pnl"] or 0),
        "best_pnl": float(row["best_pnl"] or 0),
        "worst_pnl": float(row["worst_pnl"] or 0),
        "avg_candles": float(row["avg_candles"] or 0),
    }


def _build_equity_chart(db: Database, client_id: int | None = None) -> io.BytesIO | None:
    """Построить кривую эквити и вернуть PNG как BytesIO (или None если данных нет)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    with db.cursor() as cur:
        if client_id:
            cur.execute(
                "SELECT pnl, exit_time FROM trades WHERE client_id = ? ORDER BY exit_time ASC",
                (client_id,),
            )
        else:
            cur.execute("SELECT pnl, exit_time FROM trades ORDER BY exit_time ASC")
        rows = cur.fetchall()

    if len(rows) < 2:
        return None

    # Накопленный P&L
    equity = []
    dates = []
    cumulative = 0.0
    for r in rows:
        cumulative += float(r["pnl"])
        equity.append(cumulative)
        try:
            dt = datetime.fromisoformat(r["exit_time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dates.append(dt)
        except Exception:
            dates.append(None)

    fig, ax = plt.subplots(figsize=(10, 5))
    if all(d is not None for d in dates):
        ax.plot(dates, equity, linewidth=1.5, color="#2196F3")
        ax.fill_between(dates, equity, alpha=0.1, color="#2196F3")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        fig.autofmt_xdate()
    else:
        ax.plot(range(1, len(equity) + 1), equity, linewidth=1.5, color="#2196F3")
        ax.set_xlabel("Сделки")

    ax.set_ylabel("P&L, ₽")
    ax.set_title("Кривая эквити")
    ax.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


def _build_daily_report(db: Database, registry, now_msk: datetime) -> str:
    """Сформировать текст итогов торгового дня (используется /today и авто-отчёт)."""
    MSK = ZoneInfo("Europe/Moscow")
    today_start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_msk.astimezone(timezone.utc)
    date_str = now_msk.strftime("%d.%m.%Y")

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, direction, entry_price, exit_price, stop_price, target_price,
                   qty, pnl, commission, entry_time, exit_time,
                   entry_reason, exit_reason, candles_held, client_id
            FROM trades
            WHERE exit_time >= ?
            ORDER BY exit_time ASC
            """,
            (today_start_utc.isoformat(),),
        )
        trades = cur.fetchall()

    if not trades:
        return f"📊 Итоги дня {date_str}: сделок не было."

    total_pnl = sum(float(t["pnl"]) for t in trades)
    wins = sum(1 for t in trades if float(t["pnl"]) > 0)
    losses = len(trades) - wins
    win_rate = wins / len(trades) * 100

    _exit_labels = {
        "stop_loss": "стоп-лосс",
        "take_profit": "тейк-профит",
        "timeout": "таймаут",
        "revoked": "принудительно",
    }

    # Разбивка по тикерам
    ticker_stats: dict[str, dict] = {}
    for t in trades:
        tk = t["ticker"]
        if tk not in ticker_stats:
            ticker_stats[tk] = {"pnl": 0.0, "total": 0, "wins": 0}
        ticker_stats[tk]["pnl"] += float(t["pnl"])
        ticker_stats[tk]["total"] += 1
        if float(t["pnl"]) > 0:
            ticker_stats[tk]["wins"] += 1

    lines = [
        f"📊 Итоги торгового дня {date_str}",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Сделок: {len(trades)} | P&L: {total_pnl:+.2f} ₽",
        f"✅ Прибыльных: {wins} ({win_rate:.0f}%) | ❌ Убыточных: {losses}",
    ]

    if len(ticker_stats) > 1:
        lines.append("\n📈 По тикерам:")
        for tk, s in sorted(ticker_stats.items(), key=lambda x: -x[1]["pnl"]):
            tk_wr = s["wins"] / s["total"] * 100
            icon = "✅" if s["pnl"] >= 0 else "❌"
            lines.append(
                f"  {icon} {tk}: {s['pnl']:+.2f} ₽ | {s['total']} сд. | WR {tk_wr:.0f}%"
            )

    lines.append("\n📋 Сделки:")

    for i, t in enumerate(trades, 1):
        pnl = float(t["pnl"])
        icon = "✅" if pnl >= 0 else "❌"
        try:
            entry_dt = datetime.fromisoformat(t["entry_time"])
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            entry_msk = entry_dt.astimezone(MSK).strftime("%H:%M")
        except Exception:
            entry_msk = "??"
        try:
            exit_dt = datetime.fromisoformat(t["exit_time"])
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
            exit_msk = exit_dt.astimezone(MSK).strftime("%H:%M")
        except Exception:
            exit_msk = "??"

        # Имя клиента
        client = registry.get_by_id(t["client_id"])
        client_name = (client.account_name or client.email or f"#{t['client_id']}") if client else f"#{t['client_id']}"

        exit_label = _exit_labels.get(t["exit_reason"], t["exit_reason"] or "—")
        lines.append(
            f"\n{i}. {icon} {t['ticker']} {t['direction']} | P&L: {pnl:+.2f} ₽ | {client_name}\n"
            f"   Вход {entry_msk} @ {t['entry_price']:.2f} → Выход {exit_msk} @ {t['exit_price']:.2f}\n"
            f"   SL: {t['stop_price']:.2f} | TP: {t['target_price']:.2f}\n"
            f"   Причина входа: {t['entry_reason'] or '—'}\n"
            f"   Выход по: {exit_label} | Длительность: {t['candles_held']} свечей"
        )

    return "\n".join(lines)


def _record_payment(db: Database, client_id: int, period_days: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db.write() as cur:
        cur.execute(
            """
            INSERT INTO payments (client_id, provider, amount_rub, status,
                                  external_id, period_days, created_at, paid_at)
            VALUES (?, 'manual', 0, 'paid', NULL, ?, ?, ?)
            """,
            (client_id, period_days, now, now),
        )
