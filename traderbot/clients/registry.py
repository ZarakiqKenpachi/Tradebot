"""CRUD-реестр клиентов поверх SQLite.

Единая точка доступа к таблице `clients`. Все write-операции идут через
`Database.write()` (под локом + транзакция). Методы возвращают готовые
dataclass `Client`, чтобы вызывающий код не знал про SQL/строки.

Проверка через __main__:
    py -3.12 -m traderbot.clients.registry
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from traderbot.clients.db import Database
from traderbot.clients.models import Client, ClientRole, ClientStatus

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_client(row: sqlite3.Row) -> Client:
    return Client(
        id=row["id"],
        tg_chat_id=row["tg_chat_id"],
        role=ClientRole(row["role"]),
        status=ClientStatus(row["status"]),
        email=row["email"],
        account_name=row["account_name"],
        tbank_token=row["tbank_token"],
        tbank_account_id=row["tbank_account_id"],
        paid_until=_parse_dt(row["paid_until"]),
        consecutive_errors=row["consecutive_errors"],
        created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
        updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
    )


class ClientRegistry:
    """CRUD-операции над таблицей `clients`."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # Create / Upsert
    # ------------------------------------------------------------------

    def create(
        self,
        tg_chat_id: int,
        role: ClientRole,
        status: ClientStatus,
        *,
        email: str | None = None,
        account_name: str | None = None,
        tbank_token: str | None = None,
        tbank_account_id: str | None = None,
        paid_until: datetime | None = None,
    ) -> Client:
        """Создать нового клиента. Бросает IntegrityError при дубликате tg_chat_id."""
        now = _utcnow_iso()
        paid_until_iso = paid_until.isoformat() if paid_until else None
        with self.db.write() as cur:
            cur.execute(
                """
                INSERT INTO clients (
                    tg_chat_id, role, status, email, account_name,
                    tbank_token, tbank_account_id, paid_until,
                    consecutive_errors, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    int(tg_chat_id),
                    role.value,
                    status.value,
                    email,
                    account_name,
                    tbank_token,
                    tbank_account_id,
                    paid_until_iso,
                    now,
                    now,
                ),
            )
            client_id = cur.lastrowid
            cur.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
            row = cur.fetchone()
        return _row_to_client(row)

    def get_or_create_subscriber(
        self, tg_chat_id: int, initial_status: ClientStatus = ClientStatus.PENDING_PAYMENT
    ) -> tuple[Client, bool]:
        """Атомарный upsert для /start: вернуть существующего или создать нового
        subscriber-клиента. Возвращает (client, created)."""
        existing = self.get_by_chat_id(tg_chat_id)
        if existing is not None:
            return existing, False
        try:
            client = self.create(
                tg_chat_id=tg_chat_id,
                role=ClientRole.SUBSCRIBER,
                status=initial_status,
            )
            return client, True
        except sqlite3.IntegrityError:
            # Параллельная гонка — кто-то уже создал запись. Перечитываем.
            existing = self.get_by_chat_id(tg_chat_id)
            assert existing is not None
            return existing, False

    def upsert_admin(
        self,
        tg_chat_id: int,
        account_name: str = "admin",
        tbank_token: str = "",
        tbank_account_id: str = "",
    ) -> Client:
        """Создать или обновить admin-клиента.

        Вызывается при старте из config.admin.tokens (с токеном) и для
        TG-only администраторов из TELEGRAM_ADMIN_CHAT_IDS (без токена).
        Admin всегда active, paid_until=NULL.

        Если токен не передан — существующий токен в БД не затирается.
        """
        existing = self.get_by_chat_id(tg_chat_id)
        now = _utcnow_iso()
        if existing is None:
            with self.db.write() as cur:
                cur.execute(
                    """
                    INSERT INTO clients (
                        tg_chat_id, role, status, email, account_name,
                        tbank_token, tbank_account_id, paid_until,
                        consecutive_errors, created_at, updated_at
                    ) VALUES (?, 'admin', 'active', NULL, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (int(tg_chat_id), account_name, tbank_token, tbank_account_id, now, now),
                )
                client_id = cur.lastrowid
                cur.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
                row = cur.fetchone()
            logger.info("[REGISTRY] Admin client created: id=%d chat_id=%d name=%s",
                        row["id"], tg_chat_id, account_name)
            return _row_to_client(row)

        # Обновить существующего.
        # Если новый токен не передан — оставить старый (не затирать пустой строкой).
        if tbank_token:
            with self.db.write() as cur:
                cur.execute(
                    """
                    UPDATE clients
                    SET role = 'admin',
                        status = CASE WHEN status IN ('revoked','expired') THEN status ELSE 'active' END,
                        account_name = ?,
                        tbank_token = ?,
                        tbank_account_id = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (account_name, tbank_token, tbank_account_id, now, existing.id),
                )
        else:
            with self.db.write() as cur:
                cur.execute(
                    """
                    UPDATE clients
                    SET role = 'admin',
                        status = CASE WHEN status IN ('revoked','expired') THEN status ELSE 'active' END,
                        account_name = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (account_name, now, existing.id),
                )
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM clients WHERE id = ?", (existing.id,))
            row = cur.fetchone()
        logger.info("[REGISTRY] Admin client updated: id=%d chat_id=%d", row["id"], tg_chat_id)
        return _row_to_client(row)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(self, client_id: int) -> Client | None:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
            row = cur.fetchone()
        return _row_to_client(row) if row else None

    def get_by_chat_id(self, tg_chat_id: int) -> Client | None:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM clients WHERE tg_chat_id = ?", (int(tg_chat_id),))
            row = cur.fetchone()
        return _row_to_client(row) if row else None

    def find_by_name(self, name: str) -> Client | None:
        """Поиск по account_name (точное совпадение, без учёта регистра)."""
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM clients WHERE LOWER(account_name) = LOWER(?) LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
        return _row_to_client(row) if row else None

    def find_by_email(self, email: str) -> Client | None:
        """Поиск по email (точное совпадение, без учёта регистра)."""
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM clients WHERE LOWER(email) = LOWER(?) LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
        return _row_to_client(row) if row else None

    def list_all(self) -> list[Client]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM clients ORDER BY id")
            rows = cur.fetchall()
        return [_row_to_client(r) for r in rows]

    def list_by_status(self, *statuses: ClientStatus) -> list[Client]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self.db.cursor() as cur:
            cur.execute(
                f"SELECT * FROM clients WHERE status IN ({placeholders}) ORDER BY id",
                tuple(s.value for s in statuses),
            )
            rows = cur.fetchall()
        return [_row_to_client(r) for r in rows]

    def list_active(self) -> list[Client]:
        """Все клиенты, которых нужно включить в торговый цикл."""
        return self.list_by_status(ClientStatus.ACTIVE)

    def list_tradable(self) -> list[Client]:
        """Клиенты, за чьими позициями нужно следить в цикле.

        Включает active (новые позиции открываются) + revoked с открытыми
        позициями обрабатываются отдельно через флаг ExecutionManager.
        """
        return self.list_by_status(ClientStatus.ACTIVE)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def _update_field(self, client_id: int, field: str, value) -> None:
        now = _utcnow_iso()
        with self.db.write() as cur:
            cur.execute(
                f"UPDATE clients SET {field} = ?, updated_at = ? WHERE id = ?",
                (value, now, client_id),
            )

    def update_status(self, client_id: int, status: ClientStatus) -> None:
        self._update_field(client_id, "status", status.value)
        logger.info("[REGISTRY] client %d status → %s", client_id, status.value)

    def set_account_name(self, client_id: int, account_name: str) -> None:
        self._update_field(client_id, "account_name", account_name)

    def set_email(self, client_id: int, email: str) -> None:
        self._update_field(client_id, "email", email)

    def set_token_and_account(
        self, client_id: int, tbank_token: str, tbank_account_id: str
    ) -> None:
        """Сохранить токен клиента после успешной валидации подключения."""
        now = _utcnow_iso()
        with self.db.write() as cur:
            cur.execute(
                """
                UPDATE clients
                SET tbank_token = ?,
                    tbank_account_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (tbank_token, tbank_account_id, now, client_id),
            )
        logger.info("[REGISTRY] client %d token saved (account=%s)",
                    client_id, tbank_account_id)

    def set_paid_until(self, client_id: int, paid_until: datetime | None) -> None:
        value = paid_until.isoformat() if paid_until else None
        self._update_field(client_id, "paid_until", value)

    def delete_token(self, client_id: int) -> None:
        """Обнулить токен (при revoke, чтобы не хранить больше чем нужно)."""
        now = _utcnow_iso()
        with self.db.write() as cur:
            cur.execute(
                "UPDATE clients SET tbank_token = NULL, updated_at = ? WHERE id = ?",
                (now, client_id),
            )
        logger.info("[REGISTRY] client %d token deleted", client_id)

    def increment_errors(self, client_id: int) -> int:
        """Увеличить счётчик ошибок подключения/торговли. Возвращает новое значение."""
        now = _utcnow_iso()
        with self.db.write() as cur:
            cur.execute(
                """
                UPDATE clients
                SET consecutive_errors = consecutive_errors + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, client_id),
            )
            cur.execute("SELECT consecutive_errors FROM clients WHERE id = ?", (client_id,))
            row = cur.fetchone()
        return int(row["consecutive_errors"]) if row else 0

    def reset_errors(self, client_id: int) -> None:
        now = _utcnow_iso()
        with self.db.write() as cur:
            cur.execute(
                "UPDATE clients SET consecutive_errors = 0, updated_at = ? WHERE id = ?",
                (now, client_id),
            )

    def reset_subscriber(self, client_id: int) -> None:
        """Сбросить подписчика до pending_payment: очистить email, токен, аккаунт, подписку."""
        now = _utcnow_iso()
        with self.db.write() as cur:
            cur.execute(
                """
                UPDATE clients
                SET status = 'pending_payment',
                    email = NULL,
                    tbank_token = NULL,
                    tbank_account_id = NULL,
                    paid_until = NULL,
                    consecutive_errors = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, client_id),
            )
        logger.info("[REGISTRY] client %d reset to pending_payment", client_id)

    def delete(self, client_id: int) -> None:
        """Полное удаление (в админ-flow не используется — предпочитаем revoked-статус)."""
        with self.db.write() as cur:
            cur.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        logger.info("[REGISTRY] client %d deleted", client_id)


# ---------------------------------------------------------------------------
# Self-test: py -3.12 -m traderbot.clients.registry
# ---------------------------------------------------------------------------

def _selftest() -> None:
    import os
    import tempfile

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        db = Database(db_path)
        db.init_schema()
        reg = ClientRegistry(db)

        # 1. Создать subscriber через get_or_create
        c1, created1 = reg.get_or_create_subscriber(tg_chat_id=111111)
        assert created1, "expected new client"
        assert c1.role == ClientRole.SUBSCRIBER
        assert c1.status == ClientStatus.PENDING_PAYMENT
        print(f"OK create subscriber: {c1}")

        # 2. Повторный get_or_create возвращает существующего
        c1b, created1b = reg.get_or_create_subscriber(tg_chat_id=111111)
        assert not created1b
        assert c1b.id == c1.id
        print("OK idempotent get_or_create")

        # 3. Переходы статуса
        reg.update_status(c1.id, ClientStatus.PENDING_EMAIL)
        reg.set_email(c1.id, "user@example.com")
        reg.update_status(c1.id, ClientStatus.PENDING_TOKEN)
        reg.set_token_and_account(c1.id, "t.secret_token_12345", "acct-1")
        reg.update_status(c1.id, ClientStatus.ACTIVE)
        updated = reg.get_by_id(c1.id)
        assert updated is not None
        assert updated.email == "user@example.com"
        assert updated.tbank_token == "t.secret_token_12345"
        assert updated.tbank_account_id == "acct-1"
        assert updated.status == ClientStatus.ACTIVE
        print(f"OK onboarded subscriber: {updated}")
        assert "2345" in repr(updated) and "secret" not in repr(updated), \
            f"token leak in repr: {updated!r}"
        print("OK repr masks token")

        # 4. Admin upsert
        admin = reg.upsert_admin(
            tg_chat_id=222222,
            tbank_token="t.admin_token_abcd",
            tbank_account_id="acct-admin",
            account_name="primary",
        )
        assert admin.role == ClientRole.ADMIN
        assert admin.status == ClientStatus.ACTIVE
        print(f"OK admin upsert: {admin}")

        admin2 = reg.upsert_admin(
            tg_chat_id=222222,
            tbank_token="t.admin_token_NEW",
            tbank_account_id="acct-admin",
            account_name="primary-updated",
        )
        assert admin2.id == admin.id
        assert admin2.tbank_token == "t.admin_token_NEW"
        assert admin2.account_name == "primary-updated"
        print("OK admin upsert is idempotent (updates existing)")

        # 5. Списки
        all_clients = reg.list_all()
        assert len(all_clients) == 2
        actives = reg.list_active()
        assert len(actives) == 2  # subscriber + admin

        # 6. Счётчик ошибок
        n = reg.increment_errors(c1.id)
        assert n == 1
        n = reg.increment_errors(c1.id)
        assert n == 2
        reg.reset_errors(c1.id)
        assert reg.get_by_id(c1.id).consecutive_errors == 0
        print("OK error counter")

        # 7. Удаление токена
        reg.delete_token(c1.id)
        assert reg.get_by_id(c1.id).tbank_token is None
        print("OK delete_token")

        # 8. Полное удаление
        reg.delete(c1.id)
        assert reg.get_by_id(c1.id) is None
        assert len(reg.list_all()) == 1
        print("OK delete")

        db.close()
    print("\nAll registry self-tests passed.")


if __name__ == "__main__":
    _selftest()
