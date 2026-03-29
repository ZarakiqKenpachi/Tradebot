"""
Диагностика перед запуском.
Проверяет токен, аккаунт, баланс, рыночные данные, стратегии, Telegram.
Ничего не торгует, ордера не размещает.

Запуск: py -3.12 -m traderbot.check
"""
import sys

from traderbot.config import load_config
from traderbot.broker.tbank import TBankBroker
from traderbot.data.feed import DataFeed
from traderbot.strategies.registry import STRATEGY_REGISTRY
from traderbot.notifications.telegram import TelegramNotifier


def ok(msg: str):
    print(f"  [OK] {msg}")


def fail(msg: str):
    print(f"  [FAIL] {msg}")


def section(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")


def main():
    errors = 0

    # --- Конфиг ---
    section("1. Конфиг")
    try:
        import pathlib
        config_path = str(pathlib.Path(__file__).parent / "config.yaml")
        config = load_config(config_path)
        mode = "SANDBOX" if config.broker.sandbox else "LIVE"
        ok(f"config.yaml загружен, режим: {mode}")
    except Exception as e:
        fail(f"Не удалось загрузить конфиг: {e}")
        sys.exit(1)

    # --- Стратегии ---
    section("2. Стратегии")
    for ticker, tc in config.tickers.items():
        if tc.strategy in STRATEGY_REGISTRY:
            ok(f"{ticker} -> {tc.strategy}")
        else:
            fail(f"{ticker} -> {tc.strategy} (не найдена в реестре!)")
            errors += 1

    # --- Брокер и аккаунт ---
    section("3. Подключение к брокеру")
    broker = TBankBroker(config.broker.token, config.broker.sandbox, config.broker.app_name)

    account_id = None
    try:
        if config.broker.sandbox:
            account_id = broker.sandbox_create_account()
            broker.sandbox_fund(account_id, 100000.0)
            ok(f"Sandbox аккаунт создан: {account_id}")
        else:
            account_id = broker.get_account_id()
            ok(f"Live аккаунт: {account_id}")
    except Exception as e:
        fail(f"Не удалось получить аккаунт: {e}")
        errors += 1

    # --- Баланс ---
    section("4. Баланс")
    if account_id:
        try:
            balance = broker.get_portfolio_balance(account_id)
            if balance > 0:
                ok(f"Баланс: {balance:,.2f} RUB")
            else:
                fail(f"Баланс: {balance:,.2f} RUB (пустой счёт!)")
                errors += 1
        except Exception as e:
            fail(f"Не удалось получить баланс: {e}")
            errors += 1
    else:
        fail("Пропуск — нет аккаунта")
        errors += 1

    # --- Рыночные данные ---
    section("5. Рыночные данные")
    feed = DataFeed(broker)
    for ticker, tc in config.tickers.items():
        try:
            candles = feed.get_candles(figi=tc.figi, timeframes=["1h"], days=3)
            if candles and not candles["1h"].empty:
                n = len(candles["1h"])
                last = candles["1h"].iloc[-1]["close"]
                ok(f"{ticker} ({tc.figi}): {n} свечей 1H, последняя цена {last:.2f}")
            else:
                fail(f"{ticker} ({tc.figi}): нет данных (биржа закрыта?)")
                errors += 1
        except Exception as e:
            fail(f"{ticker} ({tc.figi}): {e}")
            errors += 1

    # --- Telegram ---
    section("6. Telegram")
    if config.telegram.enabled:
        try:
            notifier = TelegramNotifier(config.telegram.token, config.telegram.chat_id)
            notifier.send("TraderBot check: тестовое сообщение")
            ok("Сообщение отправлено — проверь Telegram")
        except Exception as e:
            fail(f"Ошибка отправки: {e}")
            errors += 1
    else:
        ok("Telegram отключён в конфиге (пропуск)")

    # --- Итог ---
    section("ИТОГ")
    if errors == 0:
        print("  Все проверки пройдены. Можно запускать бота.")
    else:
        print(f"  Ошибок: {errors}. Исправь перед запуском.")
    print()


if __name__ == "__main__":
    main()
