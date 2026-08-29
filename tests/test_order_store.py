import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

from backend.order_store import OrderStore, OrderStoreError


class OrderStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "orders.sqlite3"
        self.store = OrderStore(self.database_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create(self, **overrides):
        values = {
            "product": "custom-song",
            "solo": "guitar-solo",
            "amount_cents": 22_400,
            "currency": "USD",
            "brief": {"name": "Ada", "story": "Private story", "moods": ["warm", "hopeful"]},
            "create_request_id": "create-request-1",
        }
        values.update(overrides)
        return self.store.create_order_record(**values)

    def attach(self, record, paypal_order_id="PAYPALORDER123"):
        return self.store.attach_paypal_order(record.local_order_id, paypal_order_id)

    def paid(self, record, paypal_order_id="PAYPALORDER123", capture_id="CAPTURE123", **overrides):
        values = {
            "local_order_id": record.local_order_id,
            "paypal_order_id": paypal_order_id,
            "capture_id": capture_id,
            "amount_cents": 22_400,
            "currency": "USD",
            "capture_request_id": "capture-request-1",
        }
        values.update(overrides)
        return self.store.mark_paid(**values)

    def test_create_generates_uuid_and_preserves_snapshot_without_pricing(self):
        with patch("backend.order_store.uuid.uuid4", return_value=uuid.UUID("12345678-1234-5678-1234-567812345678")):
            record = self.create()
        self.assertEqual(record.local_order_id, "12345678-1234-5678-1234-567812345678")
        self.assertEqual(record.status, "PENDING")
        self.assertEqual(record.amount_cents, 22_400)
        self.assertEqual(record.currency, "USD")
        self.assertEqual(record.brief["story"], "Private story")
        self.assertIsNone(record.paypal_order_id)
        self.assertEqual(record.created_at, record.updated_at)

    def test_create_rejects_non_json_compatible_brief_without_leaking_it(self):
        secret_brief = {"story": "do not expose this private text", "bad": object()}
        with self.assertRaises(OrderStoreError) as raised:
            self.create(brief=secret_brief)
        self.assertNotIn("do not expose this private text", str(raised.exception))

    def test_attach_moves_to_paypal_created_and_is_idempotent_for_same_id(self):
        record = self.create()
        attached = self.attach(record)
        repeated = self.attach(record)
        self.assertEqual(attached.status, "PAYPAL_CREATED")
        self.assertEqual(attached.paypal_order_id, "PAYPALORDER123")
        self.assertEqual(repeated, attached)

    def test_attach_rejects_different_or_duplicate_paypal_order_ids(self):
        first = self.create(create_request_id="one")
        second = self.create(create_request_id="two")
        self.attach(first)
        with self.assertRaises(OrderStoreError):
            self.attach(first, "ANOTHERORDER123")
        with self.assertRaises(OrderStoreError):
            self.attach(second, "PAYPALORDER123")

    def test_mark_paid_persists_verified_capture_and_is_idempotent(self):
        record = self.create()
        self.attach(record)
        paid = self.paid(record)
        repeated = self.paid(record)
        self.assertEqual(paid.status, "PAID")
        self.assertEqual(paid.paypal_capture_id, "CAPTURE123")
        self.assertEqual(paid.capture_request_id, "capture-request-1")
        self.assertEqual(repeated, paid)

    def test_mark_paid_rejects_mismatched_payment_fields(self):
        record = self.create()
        self.attach(record)
        for field, value in (
            ("paypal_order_id", "ANOTHERORDER123"),
            ("amount_cents", 19_900),
            ("currency", "EUR"),
        ):
            with self.subTest(field=field), self.assertRaises(OrderStoreError):
                self.paid(record, **{field: value})

    def test_mark_paid_rejects_different_capture_after_paid(self):
        record = self.create()
        self.attach(record)
        self.paid(record)
        with self.assertRaises(OrderStoreError):
            self.paid(record, capture_id="OTHER_CAPTURE123")

    def test_capture_id_is_unique_between_orders(self):
        first = self.create(create_request_id="one")
        second = self.create(create_request_id="two")
        self.attach(first, "PAYPALORDER123")
        self.attach(second, "PAYPALORDER456")
        self.paid(first, "PAYPALORDER123", "CAPTURE123")
        with self.assertRaises(OrderStoreError):
            self.paid(second, "PAYPALORDER456", "CAPTURE123")

    def test_lookups_and_reopen_preserve_the_record(self):
        record = self.create()
        self.attach(record)
        self.assertIsNone(self.store.get_by_local_order_id("missing"))
        self.assertIsNone(self.store.get_by_paypal_order_id("missing"))
        reopened = OrderStore(self.database_path)
        loaded_by_local = reopened.get_by_local_order_id(record.local_order_id)
        loaded_by_paypal = reopened.get_by_paypal_order_id("PAYPALORDER123")
        self.assertEqual(loaded_by_local, loaded_by_paypal)
        self.assertEqual(loaded_by_local.brief["moods"], ["warm", "hopeful"])

    def test_failed_is_a_safe_terminal_status(self):
        record = self.create()
        failed = self.store.mark_failed(record.local_order_id)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(self.store.mark_failed(record.local_order_id), failed)
        with self.assertRaises(OrderStoreError):
            self.store.mark_paid(
                record.local_order_id,
                "PAYPALORDER123",
                "CAPTURE123",
                22_400,
                "USD",
                "capture-request-1",
            )
