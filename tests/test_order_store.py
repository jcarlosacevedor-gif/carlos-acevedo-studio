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

    # --- CAPTURING state tests ---

    def test_begin_capture_transitions_paypal_created_to_capturing(self):
        record = self.create()
        self.attach(record)
        capturing = self.store.begin_capture(record.local_order_id, "capture-req-1")
        self.assertEqual(capturing.status, "CAPTURING")
        self.assertEqual(capturing.capture_request_id, "capture-req-1")
        self.assertEqual(capturing.paypal_order_id, "PAYPALORDER123")

    def test_begin_capture_rejects_missing_local_order(self):
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture("nonexistent", "capture-req-1")

    def test_begin_capture_rejects_missing_paypal_order_id(self):
        record = self.create()
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "capture-req-1")

    def test_begin_capture_rejects_non_paypal_created_state(self):
        record = self.create()
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "capture-req-1")
        self.attach(record)
        self.store.mark_failed(record.local_order_id)
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "capture-req-1")

    def test_begin_capture_is_idempotent_same_request_id(self):
        record = self.create()
        self.attach(record)
        first = self.store.begin_capture(record.local_order_id, "capture-req-1")
        second = self.store.begin_capture(record.local_order_id, "capture-req-1")
        self.assertEqual(first, second)
        self.assertEqual(second.status, "CAPTURING")
        self.assertEqual(second.capture_request_id, "capture-req-1")

    def test_begin_capture_rejects_different_request_id_on_capturing(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "capture-req-2")

    def test_begin_capture_rejects_paid_state(self):
        record = self.create()
        self.attach(record)
        self.paid(record)
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "capture-req-1")

    def test_reset_capture_attempt_transitions_capturing_to_paypal_created(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        reset = self.store.reset_capture_attempt(record.local_order_id, "capture-req-1")
        self.assertEqual(reset.status, "PAYPAL_CREATED")
        self.assertIsNone(reset.capture_request_id)

    def test_reset_capture_attempt_clears_capture_request_id(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        reset = self.store.reset_capture_attempt(record.local_order_id, "capture-req-1")
        self.assertIsNone(reset.capture_request_id)

    def test_reset_capture_attempt_rejects_wrong_request_id(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        with self.assertRaises(OrderStoreError):
            self.store.reset_capture_attempt(record.local_order_id, "wrong-req-id")

    def test_reset_capture_attempt_rejects_paid_state(self):
        record = self.create()
        self.attach(record)
        self.paid(record)
        with self.assertRaises(OrderStoreError):
            self.store.reset_capture_attempt(record.local_order_id, "any-req-id")

    def test_reset_capture_attempt_rejects_non_capturing_state(self):
        record = self.create()
        with self.assertRaises(OrderStoreError):
            self.store.reset_capture_attempt(record.local_order_id, "any-req-id")

    def test_mark_paid_accepts_capturing_state(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        paid = self.store.mark_paid(
            record.local_order_id,
            "PAYPALORDER123",
            "CAPTURE123",
            22_400,
            "USD",
            "capture-req-1",
        )
        self.assertEqual(paid.status, "PAID")

    def test_reopen_preserves_capturing_and_request_id(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        reopened = OrderStore(self.database_path)
        loaded = reopened.get_by_local_order_id(record.local_order_id)
        self.assertEqual(loaded.status, "CAPTURING")
        self.assertEqual(loaded.capture_request_id, "capture-req-1")

    def test_mark_failed_rejects_capturing_state(self):
        record = self.create()
        self.attach(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")
        with self.assertRaises(OrderStoreError):
            self.store.mark_failed(record.local_order_id)

    def test_begin_capture_rejects_excessively_long_request_id(self):
        record = self.create()
        self.attach(record)
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "x" * 109)
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "")
        with self.assertRaises(OrderStoreError):
            self.store.begin_capture(record.local_order_id, "   ")
