"""Small, durable SQLite store for future Custom Song payment orders.

This module deliberately does not call pricing or PayPal.  Callers must
validate the configuration and calculate the server price before persisting.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from .config import REQUEST_ID_MAX_LENGTH
from .paypal_client import validate_order_id


STATUSES = frozenset({"PENDING", "PAYPAL_CREATED", "CAPTURING", "PAID", "FAILED", "CANCELLED"})


class OrderStoreError(ValueError):
    """Raised for safe, caller-actionable order-store failures."""


@dataclass(frozen=True)
class OrderRecord:
    local_order_id: str
    created_at: str
    updated_at: str
    product: str
    solo: str
    amount_cents: int
    currency: str
    brief: Any
    paypal_order_id: str | None
    paypal_capture_id: str | None
    status: str
    create_request_id: str
    capture_request_id: str | None


class OrderStore:
    """SQLite-backed order store using a short-lived connection per operation.

    State transitions:
      PENDING -> PAYPAL_CREATED (via attach_paypal_order)
      PAYPAL_CREATED -> CAPTURING (via begin_capture)
      CAPTURING -> PAID (via mark_paid)
      CAPTURING -> PAYPAL_CREATED (via reset_capture_attempt with compare-and-set)

    Terminal states: PAID, FAILED, CANCELLED.
    FAILED can only be reached from PENDING or PAYPAL_CREATED.
    CAPTURING -> FAILED is NOT allowed; use reset_capture_attempt for deterministic
    failures where PayPal did not capture.
    """

    def __init__(self, database_path: str | Path):
        self._database_path = str(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            # Check if table exists
            table_info = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='custom_song_orders'"
            ).fetchone()

            if table_info is None:
                # Table doesn't exist, create with current schema
                connection.execute(
                    """
                    CREATE TABLE custom_song_orders (
                        id INTEGER PRIMARY KEY,
                        local_order_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        product TEXT NOT NULL,
                        solo TEXT NOT NULL,
                        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                        currency TEXT NOT NULL CHECK (length(currency) > 0),
                        brief_json TEXT NOT NULL,
                        paypal_order_id TEXT UNIQUE,
                        paypal_capture_id TEXT UNIQUE,
                        status TEXT NOT NULL CHECK (status IN ('PENDING', 'PAYPAL_CREATED', 'CAPTURING', 'PAID', 'FAILED', 'CANCELLED')),
                        create_request_id TEXT NOT NULL,
                        capture_request_id TEXT
                    )
                    """
                )
            else:
                # Table exists, check if migration is needed
                self._migrate_if_needed(connection)

    def _migrate_if_needed(self, connection: sqlite3.Connection) -> None:
        """Check schema and migrate if needed from legacy (without CAPTURING) to current."""
        # Get the current CHECK constraint for status column
        # SQLite doesn't have a direct way to query CHECK constraints, so we use PRAGMA
        # and check the sql create statement
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='custom_song_orders'"
        ).fetchone()

        if create_sql is None:
            return

        create_sql_str = create_sql[0].lower()

        # Check if CAPTURING is already in the CHECK constraint
        if "'capturing'" in create_sql_str or '"capturing"' in create_sql_str:
            # Schema already has CAPTURING, no migration needed
            return

        # Check if this is a known legacy schema (without CAPTURING but with other expected states)
        has_pending = "'pending'" in create_sql_str or '"pending"' in create_sql_str
        has_paypal_created = "'paypal_created'" in create_sql_str or '"paypal_created"' in create_sql_str
        has_paid = "'paid'" in create_sql_str or '"paid"' in create_sql_str
        has_failed = "'failed'" in create_sql_str or '"failed"' in create_sql_str
        has_cancelled = "'cancelled'" in create_sql_str or '"cancelled"' in create_sql_str
        has_capture_request_id = "capture_request_id" in create_sql_str

        if not (has_pending and has_paypal_created and has_paid and has_failed and has_cancelled):
            # Unknown schema - fail safely
            raise OrderStoreError(
                "Incompatible database schema detected. "
                "The custom_song_orders table has an unexpected structure. "
                "Manual recovery required."
            )

        if not has_capture_request_id:
            # Unknown schema without capture_request_id column
            raise OrderStoreError(
                "Incompatible database schema detected: missing capture_request_id column. "
                "Manual recovery required."
            )

        # This is a known legacy schema without CAPTURING - perform migration
        self._migrate_legacy_schema(connection, create_sql_str)

    def _migrate_legacy_schema(self, connection: sqlite3.Connection, create_sql_str: str) -> None:
        """Migrate from legacy schema (without CAPTURING) to current schema."""
        connection.execute("BEGIN EXCLUSIVE")

        try:
            # Create new table with updated schema
            connection.execute(
                """
                CREATE TABLE custom_song_orders_new (
                    id INTEGER PRIMARY KEY,
                    local_order_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    product TEXT NOT NULL,
                    solo TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                    currency TEXT NOT NULL CHECK (length(currency) > 0),
                    brief_json TEXT NOT NULL,
                    paypal_order_id TEXT UNIQUE,
                    paypal_capture_id TEXT UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'PAYPAL_CREATED', 'CAPTURING', 'PAID', 'FAILED', 'CANCELLED')),
                    create_request_id TEXT NOT NULL,
                    capture_request_id TEXT
                )
                """
            )

            # Copy all data explicitly
            connection.execute(
                """
                INSERT INTO custom_song_orders_new (
                    id, local_order_id, created_at, updated_at, product, solo,
                    amount_cents, currency, brief_json, paypal_order_id, paypal_capture_id,
                    status, create_request_id, capture_request_id
                ) SELECT
                    id, local_order_id, created_at, updated_at, product, solo,
                    amount_cents, currency, brief_json, paypal_order_id, paypal_capture_id,
                    status, create_request_id, capture_request_id
                FROM custom_song_orders
                """
            )

            # Verify copy
            old_count = connection.execute("SELECT COUNT(*) FROM custom_song_orders").fetchone()[0]
            new_count = connection.execute("SELECT COUNT(*) FROM custom_song_orders_new").fetchone()[0]

            if old_count != new_count:
                raise OrderStoreError(
                    f"Migration copy verification failed: {old_count} rows in old table, "
                    f"{new_count} rows in new table."
                )

            # Drop old table
            connection.execute("DROP TABLE custom_song_orders")

            # Rename new table
            connection.execute("ALTER TABLE custom_song_orders_new RENAME TO custom_song_orders")

            connection.execute("COMMIT")

        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _brief_json(brief: Any) -> str:
        if not isinstance(brief, (dict, list)):
            raise OrderStoreError("Brief must be a JSON object or array.")
        try:
            return json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise OrderStoreError("Brief must be JSON-compatible.") from error

    @staticmethod
    def _nonempty_text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise OrderStoreError(f"{field} is required.")
        return value

    @classmethod
    def _record_from_row(cls, row: sqlite3.Row | None) -> OrderRecord | None:
        if row is None:
            return None
        return OrderRecord(
            local_order_id=row["local_order_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            product=row["product"],
            solo=row["solo"],
            amount_cents=row["amount_cents"],
            currency=row["currency"],
            brief=json.loads(row["brief_json"]),
            paypal_order_id=row["paypal_order_id"],
            paypal_capture_id=row["paypal_capture_id"],
            status=row["status"],
            create_request_id=row["create_request_id"],
            capture_request_id=row["capture_request_id"],
        )

    def create_order_record(
        self,
        *,
        product: str,
        solo: str,
        amount_cents: int,
        currency: str,
        brief: Any,
        create_request_id: str,
    ) -> OrderRecord:
        product = self._nonempty_text(product, "Product")
        solo = self._nonempty_text(solo, "Solo")
        currency = self._nonempty_text(currency, "Currency")
        create_request_id = self._nonempty_text(create_request_id, "Create request ID")
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            raise OrderStoreError("Amount must be a positive integer number of cents.")
        brief_json = self._brief_json(brief)
        local_order_id = str(uuid.uuid4())
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO custom_song_orders (
                    local_order_id, created_at, updated_at, product, solo,
                    amount_cents, currency, brief_json, status, create_request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (local_order_id, now, now, product, solo, amount_cents, currency, brief_json, create_request_id),
            )
        record = self.get_by_local_order_id(local_order_id)
        assert record is not None
        return record

    def get_by_local_order_id(self, local_order_id: str) -> OrderRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
        return self._record_from_row(row)

    def get_by_paypal_order_id(self, paypal_order_id: str) -> OrderRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM custom_song_orders WHERE paypal_order_id = ?", (paypal_order_id,)).fetchone()
        return self._record_from_row(row)

    def attach_paypal_order(self, local_order_id: str, paypal_order_id: str) -> OrderRecord:
        paypal_order_id = validate_order_id(paypal_order_id)
        now = self._now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
                record = self._record_from_row(row)
                if record is None:
                    raise OrderStoreError("Local order was not found.")
                if record.paypal_order_id is not None:
                    if record.paypal_order_id == paypal_order_id:
                        return record
                    raise OrderStoreError("A different PayPal order is already attached.")
                if record.status != "PENDING":
                    raise OrderStoreError("Local order cannot receive a PayPal order in its current state.")
                connection.execute(
                    "UPDATE custom_song_orders SET paypal_order_id = ?, status = 'PAYPAL_CREATED', updated_at = ? WHERE local_order_id = ?",
                    (paypal_order_id, now, local_order_id),
                )
        except sqlite3.IntegrityError as error:
            raise OrderStoreError("PayPal order is already attached to another local order.") from error
        updated = self.get_by_local_order_id(local_order_id)
        assert updated is not None
        return updated

    def mark_paid(
        self,
        local_order_id: str,
        paypal_order_id: str,
        capture_id: str,
        amount_cents: int,
        currency: str,
        capture_request_id: str,
    ) -> OrderRecord:
        paypal_order_id = validate_order_id(paypal_order_id)
        capture_id = self._nonempty_text(capture_id, "Capture ID")
        currency = self._nonempty_text(currency, "Currency")
        capture_request_id = self._nonempty_text(capture_request_id, "Capture request ID")
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            raise OrderStoreError("Amount must be a positive integer number of cents.")
        now = self._now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
                record = self._record_from_row(row)
                if record is None:
                    raise OrderStoreError("Local order was not found.")
                if record.paypal_order_id != paypal_order_id:
                    raise OrderStoreError("PayPal order does not match the local order.")
                if record.amount_cents != amount_cents or record.currency != currency:
                    raise OrderStoreError("Captured payment does not match the stored price.")
                if record.status == "PAID":
                    if record.paypal_capture_id == capture_id:
                        return record
                    raise OrderStoreError("A different capture is already recorded for this order.")
                if record.status != "PAYPAL_CREATED":
                    raise OrderStoreError("Local order cannot be marked paid in its current state.")
                connection.execute(
                    """
                    UPDATE custom_song_orders
                    SET paypal_capture_id = ?, capture_request_id = ?, status = 'PAID', updated_at = ?
                    WHERE local_order_id = ?
                    """,
                    (capture_id, capture_request_id, now, local_order_id),
                )
        except sqlite3.IntegrityError as error:
            raise OrderStoreError("Capture is already attached to another local order.") from error
        updated = self.get_by_local_order_id(local_order_id)
        assert updated is not None
        return updated

    def begin_capture(self, local_order_id: str, capture_request_id: str) -> OrderRecord:
        """Transition PAYPAL_CREATED -> CAPTURING with idempotent capture request ID."""
        capture_request_id = self._nonempty_text(capture_request_id, "Capture request ID")
        if len(capture_request_id) > REQUEST_ID_MAX_LENGTH:
            raise OrderStoreError("Capture request ID is too long.")
        now = self._now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
                record = self._record_from_row(row)
                if record is None:
                    raise OrderStoreError("Local order was not found.")
                if record.paypal_order_id is None:
                    raise OrderStoreError("Local order has no PayPal order attached.")
                if record.status == "PAYPAL_CREATED":
                    connection.execute(
                        "UPDATE custom_song_orders SET status = 'CAPTURING', capture_request_id = ?, updated_at = ? WHERE local_order_id = ?",
                        (capture_request_id, now, local_order_id),
                    )
                elif record.status == "CAPTURING":
                    if record.capture_request_id == capture_request_id:
                        return record
                    raise OrderStoreError("Capture request ID cannot be changed once set.")
                else:
                    raise OrderStoreError("Local order cannot begin capture in its current state.")
        except sqlite3.IntegrityError:
            raise OrderStoreError("Capture request conflict.")
        updated = self.get_by_local_order_id(local_order_id)
        assert updated is not None
        return updated

    def reset_capture_attempt(self, local_order_id: str, expected_capture_request_id: str) -> OrderRecord:
        """Reset CAPTURING -> PAYPAL_CREATED only if request ID matches exactly (compare-and-set)."""
        expected_capture_request_id = self._nonempty_text(expected_capture_request_id, "Expected capture request ID")
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
            record = self._record_from_row(row)
            if record is None:
                raise OrderStoreError("Local order was not found.")
            if record.status != "CAPTURING":
                raise OrderStoreError("Local order cannot reset capture attempt in its current state.")
            if record.capture_request_id != expected_capture_request_id:
                raise OrderStoreError("Capture request ID does not match.")
            connection.execute(
                "UPDATE custom_song_orders SET status = 'PAYPAL_CREATED', capture_request_id = NULL, updated_at = ? WHERE local_order_id = ?",
                (now, local_order_id),
            )
        updated = self.get_by_local_order_id(local_order_id)
        assert updated is not None
        return updated

    def mark_paid(
        self,
        local_order_id: str,
        paypal_order_id: str,
        capture_id: str,
        amount_cents: int,
        currency: str,
        capture_request_id: str,
    ) -> OrderRecord:
        paypal_order_id = validate_order_id(paypal_order_id)
        capture_id = self._nonempty_text(capture_id, "Capture ID")
        currency = self._nonempty_text(currency, "Currency")
        capture_request_id = self._nonempty_text(capture_request_id, "Capture request ID")
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            raise OrderStoreError("Amount must be a positive integer number of cents.")
        now = self._now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
                record = self._record_from_row(row)
                if record is None:
                    raise OrderStoreError("Local order was not found.")
                if record.paypal_order_id != paypal_order_id:
                    raise OrderStoreError("PayPal order does not match the local order.")
                if record.amount_cents != amount_cents or record.currency != currency:
                    raise OrderStoreError("Captured payment does not match the stored price.")
                if record.status == "PAID":
                    if record.paypal_capture_id == capture_id:
                        return record
                    raise OrderStoreError("A different capture is already recorded for this order.")
                if record.status not in {"PAYPAL_CREATED", "CAPTURING"}:
                    raise OrderStoreError("Local order cannot be marked paid in its current state.")
                connection.execute(
                    """
                    UPDATE custom_song_orders
                    SET paypal_capture_id = ?, capture_request_id = ?, status = 'PAID', updated_at = ?
                    WHERE local_order_id = ?
                    """,
                    (capture_id, capture_request_id, now, local_order_id),
                )
        except sqlite3.IntegrityError as error:
            raise OrderStoreError("Capture is already attached to another local order.") from error
        updated = self.get_by_local_order_id(local_order_id)
        assert updated is not None
        return updated

    def mark_failed(self, local_order_id: str) -> OrderRecord:
        """Record a terminal failure without storing potentially sensitive details."""
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM custom_song_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
            record = self._record_from_row(row)
            if record is None:
                raise OrderStoreError("Local order was not found.")
            if record.status not in {"PENDING", "PAYPAL_CREATED", "FAILED"}:
                raise OrderStoreError("Local order cannot be marked failed in its current state.")
            if record.status != "FAILED":
                connection.execute("UPDATE custom_song_orders SET status = 'FAILED', updated_at = ? WHERE local_order_id = ?", (now, local_order_id))
        updated = self.get_by_local_order_id(local_order_id)
        assert updated is not None
        return updated
