import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.order_service import OrderService, OrderServiceError, _format_amount_cents
from backend.order_store import OrderStore, OrderStoreError


class TestFormatAmountCents(unittest.TestCase):
    def test_formats_positive_cents(self):
        self.assertEqual(_format_amount_cents(19900), "199.00")
        self.assertEqual(_format_amount_cents(22400), "224.00")
        self.assertEqual(_format_amount_cents(100), "1.00")
        self.assertEqual(_format_amount_cents(5), "0.05")
        self.assertEqual(_format_amount_cents(1), "0.01")


class OrderServiceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "orders.sqlite3"
        self.store = OrderStore(self.database_path)
        self.paypal_client = MagicMock()
        self.service = OrderService(
            store=self.store,
            paypal_client=self.paypal_client,
            return_url="https://example.com/return",
            cancel_url="https://example.com/cancel",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_local_order(self):
        return self.store.create_order_record(
            product="custom-song",
            solo="guitar-solo",
            amount_cents=19900,
            currency="USD",
            brief={"name": "Test"},
            create_request_id="create-1",
        )

    def attach_paypal_order(self, record):
        return self.store.attach_paypal_order(record.local_order_id, "PAYPALORDER123")

    # --- ALREADY PAID ---

    def test_capture_already_paid_returns_paid_without_calls(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        self.store.mark_paid(
            record.local_order_id,
            "PAYPALORDER123",
            "CAPTURE123",
            19900,
            "USD",
            "capture-req-1",
        )
        result = self.service.capture_order(record.local_order_id)
        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["local_order_id"], record.local_order_id)
        self.assertEqual(result["paypal_order_id"], "PAYPALORDER123")
        self.assertEqual(result["capture_id"], "CAPTURE123")
        self.assertEqual(result["amount"], "199.00")
        self.assertEqual(result["currency"], "USD")
        # Verify no PayPal calls were made
        self.paypal_client.show_order.assert_not_called()
        self.paypal_client.capture_order.assert_not_called()

    # --- INVALID STATES ---

    def test_capture_pending_rejected(self):
        record = self.create_local_order()
        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PENDING", str(ctx.exception))

    def test_capture_failed_rejected(self):
        record = self.create_local_order()
        self.store.mark_failed(record.local_order_id)
        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("FAILED", str(ctx.exception))

    def test_capture_cancelled_rejected(self):
        record = self.create_local_order()
        self.store.mark_failed(record.local_order_id)
        # Mark as CANCELLED via direct SQL for test
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE custom_song_orders SET status = 'CANCELLED' WHERE local_order_id = ?",
                (record.local_order_id,),
            )
        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("CANCELLED", str(ctx.exception))

    def test_capture_no_paypal_order_id_rejected(self):
        # States without paypal_order_id (PENDING) are rejected by state first with 409.
        record = self.create_local_order()
        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PENDING", str(ctx.exception))

    def test_capture_nonexistent_order_rejected(self):
        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order("nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not found", str(ctx.exception))

    # --- PAYPAL_CREATED -> CAPTURING -> CAPTURE -> PAID (Success 199) ---

    def test_success_199_paypal_created_to_paid(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        # Mock show_order returns APPROVED
        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        # Mock capture_order returns COMPLETED
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        result = self.service.capture_order(record.local_order_id)

        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["local_order_id"], record.local_order_id)
        self.assertEqual(result["paypal_order_id"], "PAYPALORDER123")
        self.assertEqual(result["capture_id"], "CAPTURE123")
        self.assertEqual(result["amount"], "199.00")
        self.assertEqual(result["currency"], "USD")

        # Verify show_order was called
        self.paypal_client.show_order.assert_called_once_with("PAYPALORDER123")
        # Verify begin_capture was called before capture_order
        # Verify capture_order was called with the persisted request_id
        self.paypal_client.capture_order.assert_called_once()
        call_args = self.paypal_client.capture_order.call_args
        self.assertEqual(call_args[0][0], "PAYPALORDER123")
        # The request_id should be a UUID string
        self.assertTrue(len(call_args[0][1]) > 0)

    # --- SUCCESS 224 ---

    def test_success_224_with_different_amount(self):
        record = self.store.create_order_record(
            product="custom-song",
            solo="piano-solo",
            amount_cents=22400,
            currency="USD",
            brief={"name": "Test"},
            create_request_id="create-224",
        )
        self.attach_paypal_order(record)  # paypal_order_id = PAYPALORDER123

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE224",
            "capture_status": "COMPLETED",
            "amount": "224.00",
            "currency": "USD",
        }

        result = self.service.capture_order(record.local_order_id)

        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["amount"], "224.00")
        self.assertEqual(result["currency"], "USD")

    # --- PAYPAL_CREATED + SHOW CREATED ---

    def test_not_approved_show_created_no_capture(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "CREATED",
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("not yet approved", str(ctx.exception))
        self.paypal_client.capture_order.assert_not_called()

    # --- CAPTURING RECONCILIATION: Show COMPLETED ---

    def test_capturing_reconciliation_show_completed(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        # Begin capture first
        self.store.begin_capture(record.local_order_id, "capture-req-1")

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        result = self.service.capture_order(record.local_order_id)

        self.assertEqual(result["status"], "PAID")
        self.assertEqual(result["capture_id"], "CAPTURE123")
        # capture_order should NOT be called for reconciliation
        self.paypal_client.capture_order.assert_not_called()

    # --- CAPTURING RETRY: Show APPROVED ---

    def test_capturing_retry_show_approved_same_request_id(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        result = self.service.capture_order(record.local_order_id)

        self.assertEqual(result["status"], "PAID")
        # capture_order should be called with the SAME request_id
        self.paypal_client.capture_order.assert_called_once()
        call_args = self.paypal_client.capture_order.call_args
        self.assertEqual(call_args[0][0], "PAYPALORDER123")
        self.assertEqual(call_args[0][1], "capture-req-1")

    # --- AMBIGUOUS CAPTURE ---

    def test_ambiguous_show_order_persists_capturing(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")

        from backend.paypal_client import PayPalAmbiguousResultError
        self.paypal_client.show_order.side_effect = PayPalAmbiguousResultError(
            "Ambiguous"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("recovery", str(ctx.exception))
        self.paypal_client.capture_order.assert_not_called()

    def test_ambiguous_capture_persists_capturing(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        from backend.paypal_client import PayPalAmbiguousResultError
        self.paypal_client.capture_order.side_effect = PayPalAmbiguousResultError(
            "Ambiguous"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("uncertain", str(ctx.exception))

    # --- DETERMINISTIC CAPTURE FAILURE ---

    def test_deterministic_capture_failure_resets(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        from backend.paypal_client import PayPalClientError
        self.paypal_client.capture_order.side_effect = PayPalClientError(
            "Deterministic failure"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("deterministically", str(ctx.exception))

        # Verify reset_capture_attempt was called
        # Check that the order went back to PAYPAL_CREATED
        reloaded = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(reloaded.status, "PAYPAL_CREATED")

    # --- MISMATCHES ---

    def test_mismatch_paypal_order_id(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)  # paypal_order_id = PAYPALORDER123

        self.paypal_client.show_order.return_value = {
            "order_id": "DIFFERENT_ORDER",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "DIFFERENT_ORDER",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("mismatch", str(ctx.exception).lower())

    def test_mismatch_amount(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)  # amount_cents = 19900

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "200.00",  # Mismatch: 200.00 vs 199.00
            "currency": "USD",
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("mismatch", str(ctx.exception).lower())

    def test_mismatch_currency(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)  # currency = USD

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "EUR",  # Mismatch
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_mismatch_capture_status(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "PENDING",  # Mismatch: should be COMPLETED
            "amount": "199.00",
            "currency": "USD",
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 400)

    # --- CAPTURING + SHOW COMPLETED MISMATCH ---

    def test_capturing_show_completed_mismatch_amount(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "200.00",  # Mismatch
            "currency": "USD",
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("does not match", str(ctx.exception))
        self.paypal_client.capture_order.assert_not_called()

    # --- PRIVACY: No sensitive fields in result ---

    def test_result_does_not_contain_sensitive_fields(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        result = self.service.capture_order(record.local_order_id)

        self.assertNotIn("brief", result)
        self.assertNotIn("create_request_id", result)
        self.assertNotIn("capture_request_id", result)
        self.assertNotIn("product", result)
        self.assertNotIn("solo", result)
        keys = set(result.keys())
        expected_keys = {"local_order_id", "paypal_order_id", "capture_id", "status", "amount", "currency"}
        self.assertEqual(keys, expected_keys)

    # --- PRIVACY: No sensitive fields in errors ---

    def test_errors_do_not_contain_sensitive_fields(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        self.store.begin_capture(record.local_order_id, "capture-req-1")

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "200.00",
            "currency": "USD",
        }

        try:
            self.service.capture_order(record.local_order_id)
        except OrderServiceError as error:
            self.assertNotIn("brief", str(error))
            self.assertNotIn("capture_request_id", str(error))

    # --- HTTP 500 / 429 Capture -> Ambiguous -> CAPTURING persists ---

    def test_http_500_capture_ambiguous_persists_capturing(self):
        from backend.paypal_client import PayPalAmbiguousResultError
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.side_effect = PayPalAmbiguousResultError(
            "HTTP 500"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 503)
        # Verify CAPTURING persists
        reloaded = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(reloaded.status, "CAPTURING")
        # Verify request_id persists (it was generated by begin_capture)
        self.assertIsNotNone(reloaded.capture_request_id)
        self.assertTrue(len(reloaded.capture_request_id) > 0)

    def test_http_429_capture_ambiguous_persists_capturing(self):
        from backend.paypal_client import PayPalAmbiguousResultError
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.side_effect = PayPalAmbiguousResultError(
            "HTTP 429"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 503)
        reloaded = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(reloaded.status, "CAPTURING")

    def test_http_408_capture_ambiguous_persists_capturing(self):
        from backend.paypal_client import PayPalAmbiguousResultError
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.side_effect = PayPalAmbiguousResultError(
            "HTTP 408"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 503)
        reloaded = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(reloaded.status, "CAPTURING")

    # --- 4xx determinista -> reset PAYPAL_CREATED ---

    def test_deterministic_4xx_capture_failure_resets(self):
        from backend.paypal_client import PayPalClientError
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.side_effect = PayPalClientError(
            "HTTP 400 Bad Request"
        )

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 502)
        reloaded = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(reloaded.status, "PAYPAL_CREATED")
        self.assertIsNone(reloaded.capture_request_id)

    # --- Capture remoto COMPLETED + mark_paid falla -> CAPTURING ---

    def test_capture_completed_but_mark_paid_fails_preserves_capturing(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "APPROVED",
        }
        self.paypal_client.capture_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        # Mock mark_paid to fail
        original_mark_paid = self.store.mark_paid
        def failing_mark_paid(*args, **kwargs):
            raise OrderStoreError("Database error")
        self.store.mark_paid = failing_mark_paid

        try:
            with self.assertRaises(OrderServiceError) as ctx:
                self.service.capture_order(record.local_order_id)
            self.assertEqual(ctx.exception.status_code, 500)
            # Verify CAPTURING persists
            reloaded = self.store.get_by_local_order_id(record.local_order_id)
            self.assertEqual(reloaded.status, "CAPTURING")
            # The request_id was generated by begin_capture, not hardcoded
            self.assertIsNotNone(reloaded.capture_request_id)
            self.assertTrue(len(reloaded.capture_request_id) > 0)

            # Second call: show_order returns COMPLETED -> reconciliation
            self.paypal_client.show_order.return_value = {
                "order_id": "PAYPALORDER123",
                "order_status": "COMPLETED",
                "capture_id": "CAPTURE123",
                "capture_status": "COMPLETED",
                "amount": "199.00",
                "currency": "USD",
            }
            # Restore mark_paid
            self.store.mark_paid = original_mark_paid

            result = self.service.capture_order(record.local_order_id)
            self.assertEqual(result["status"], "PAID")
            # capture_order should NOT be called again in reconciliation
            self.assertEqual(self.paypal_client.capture_order.call_count, 1)
        finally:
            self.store.mark_paid = original_mark_paid

    # --- PAYPAL_CREATED + show COMPLETED -> RECONCILIATION_REQUIRED ---

    def test_paypal_created_show_completed_requires_reconciliation(self):
        record = self.create_local_order()
        self.attach_paypal_order(record)
        # record.status = PAYPAL_CREATED, no capture_request_id yet

        self.paypal_client.show_order.return_value = {
            "order_id": "PAYPALORDER123",
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }

        with self.assertRaises(OrderServiceError) as ctx:
            self.service.capture_order(record.local_order_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Reconciliation required", str(ctx.exception))
        # Verify NO capture was called
        self.paypal_client.capture_order.assert_not_called()
        # Verify local status remains PAYPAL_CREATED
        reloaded = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(reloaded.status, "PAYPAL_CREATED")
