"""
Диагностика перед запуском.
Проверяет конфиг, рыночный токен, admin-токены, рыночные данные и Telegram.
Ничего не торгует, ордера не размещает.

Запуск: py -3.12 -m traderbot.check
"""
import pathlib
import sys

import telebot

from traderbot.broker.tbank import TBankBroker
from traderbot.config import load_config
from traderbot.data.feed import DataFeed
from traderbot.strategies.registry import STRATEGY_REGISTRY


def ok(msg: str):
    print(f"  [OK]   {msg}")


def warn(msg: str):
    print(f"  [WARN] {msg}")


def fail(msg: str):
    print(f"  [FAIL] {msg}")


def section(title: str):
    print(f"\n{'=' * 52}")
    print(f"  {title}")
    print(f"{'=' * 52}")


def main():
    errors = 0

    # ------------------------------------------------------------------ #
    # 1. Конфиг
    # ------------------------------------------------------------------ #
    section("1. Конфиг")
    try:
        config_path = str(pathlib.Path(__file__).parent / "config.yaml")
        config = load_config(config_path)
        ok(f"config.yaml загружен. Тикеров: {len(config.tickers)}")
    except Exception as e:
        fail(f"Не удалось загрузить конфиг: {e}")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. Стратегии
    # ------------------------------------------------------------------ #
    section("2. Стратегии")
    for ticker, tc in config.tickers.items():
        if tc.strategy in STRATEGY_REGISTRY:
            ok(f"{ticker} → {tc.strategy}")
        else:
            fail(f"{ticker} → {tc.strategy} (не найдена в реестре!)")
            errors += 1

    # ------------------------------------------------------------------ #
    # 3. Market data токен
    # ------------------------------------------------------------------ #
    section("3. Market data токен (TBANK_MARKET_TOKEN)")
    if not config.market_data.token_env:
        fail("TBANK_MARKET_TOKEN не задан в .env")
        errors += 1
        md_broker = None
    else:
        try:
            md_broker = TBankBroker(
                token=config.market_data.token_env,
                sandbox=False,
                app_name=config.market_data.app_name,
            )
            account_id = md_broker.get_account_id()
            ok(f"Подключено. Аккаунт: {account_id}")
        except Exception as e:
            fail(f"Не удалось подключиться: {e}")
            errors += 1
            md_broker = None

    # ------------------------------------------------------------------ #
    # 4. Admin-токены
    # ------------------------------------------------------------------ #
    section("4. Admin-токены (торговые)")
    if not config.admin.tokens:
        fail("Нет ни одного admin.tokens в config.yaml")
        errors += 1
    for token_cfg in config.admin.tokens:
        if not token_cfg.token_env:
            fail(f"Токен '{token_cfg.name}': переменная окружения не задана или пуста")
            errors += 1
            continue
        try:
            broker = TBankBroker(
                token=token_cfg.token_env,
                sandbox=False,
                app_name="TraderBot-check",
            )
            account_id = broker.get_account_id()
            balance = broker.get_portfolio_balance(account_id)
            if balance > 0:
                ok(f"'{token_cfg.name}': аккаунт {account_id}, баланс {balance:,.2f} RUB")
            else:
                warn(f"'{token_cfg.name}': аккаунт {account_id}, баланс {balance:,.2f} RUB (пустой!)")
        except Exception as e:
            fail(f"'{token_cfg.name}': {e}")
            errors += 1

    # ------------------------------------------------------------------ #
    # 5. Рыночные данные
    # ------------------------------------------------------------------ #
    section("5. Рыночные данные")
    if md_broker is None:
        warn("Пропуск — нет market-data брокера")
    else:
        feed = DataFeed(md_broker)
        for ticker, tc in config.tickers.items():
            try:
                candles = feed.get_candles(figi=tc.figi, timeframes=["1h"], days=3)
                if candles and not candles["1h"].empty:
                    n = len(candles["1h"])
                    last = candles["1h"].iloc[-1]["close"]
                    ok(f"{ticker} ({tc.figi}): {n} свечей 1H, последняя {last:.2f}")
                else:
                    warn(f"{ticker} ({tc.figi}): нет данных (биржа закрыта?)")
            except Exception as e:
                fail(f"{ticker} ({tc.figi}): {e}")
                errors += 1

    # ------------------------------------------------------------------ #
    # 6. Telegram
    # ------------------------------------------------------------------ #
    section("6. Telegram")
    if not config.telegram.enabled:
        warn("Telegram отключён (TELEGRAM_BOT_TOKEN не задан)")
    else:
        try:
            tb = telebot.TeleBot(config.telegram.token, parse_mode=None)
            targets = config.telegram.admin_chat_ids
            if not targets:
                fail("TELEGRAM_ADMIN_CHAT_ID не задан")
                errors += 1
            else:
                for chat_id in targets:
                    tb.send_message(chat_id, "✅ TraderBot check: диагностическое сообщение")
                ok(f"Сообщение отправлено ({len(targets)} адресат(ов)) — проверь Telegram")
        except Exception as e:
            fail(f"Ошибка Telegram: {e}")
            errors += 1

    # ------------------------------------------------------------------ #
    # Итог
    # ------------------------------------------------------------------ #
    section("ИТОГ")
    if errors == 0:
        print("  Все проверки пройдены. Можно запускать бота.\n")
    else:
        print(f"  Ошибок: {errors}. Исправь перед запуском.\n")
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
