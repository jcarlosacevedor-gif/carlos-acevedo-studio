import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.order_admin import main
from backend.observability import safe_ref
from backend.order_store import OrderStore
from backend.paypal_client import PayPalAmbiguousResultError


class FakeShowOnlyPayPal:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.show_calls = []
        self.capture_calls = 0

    def show_order(self, order_id):
        self.show_calls.append(order_id)
        if self.error is not None:
            raise self.error
        return self.result

    def capture_order(self, *args, **kwargs):
        self.capture_calls += 1
        raise AssertionError("The administrative CLI must never call Capture.")


class OrderAdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "orders.sqlite3"
        self.store = OrderStore(self.database_path)
        self.environment = patch.dict(
            os.environ,
            {
                "ORDER_DB_PATH": str(self.database_path),
                "PAYPAL_ENVIRONMENT": "sandbox",
                "PAYPAL_CLIENT_ID": "SYNTHETIC_CLIENT_ID",
                "PAYPAL_CLIENT_SECRET": "SYNTHETIC_CLIENT_SECRET",
            },
            clear=True,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def create_order(self, suffix="1"):
        return self.store.create_order_record(
            product="custom-song",
            solo="guitar-solo",
            amount_cents=19900,
            currency="USD",
            brief={
                "name": "PRIVATE_NAME_SENTINEL",
                "email": "PRIVATE_EMAIL_SENTINEL@example.test",
                "phone": "PRIVATE_PHONE_SENTINEL",
                "token": "PRIVATE_TOKEN_SENTINEL",
            },
            create_request_id=f"create-request-{suffix}",
        )

    def create_capturing(self, suffix="1"):
        record = self.create_order(suffix)
        paypal_order_id = f"SYNTHETICORDER{suffix}"
        self.store.attach_paypal_order(record.local_order_id, paypal_order_id)
        return self.store.begin_capture(record.local_order_id, f"capture-request-{suffix}")

    @staticmethod
    def completed_remote(order_id, *, amount="199.00", currency="USD"):
        return {
            "order_id": order_id,
            "order_status": "COMPLETED",
            "capture_id": "SYNTHETICCAPTURE",
            "capture_status": "COMPLETED",
            "amount": amount,
            "currency": currency,
            "approval_url": "https://example.invalid/?token=REMOTE_TOKEN_SENTINEL",
            "payload": {"PayerID": "REMOTE_PAYER_SENTINEL"},
        }

    def invoke(self, argv, paypal=None, *, now=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        factory_calls = []

        def factory():
            factory_calls.append(True)
            if paypal is None:
                self.fail("PayPal credentials/client were requested unnecessarily.")
            return paypal

        code = main(
            argv,
            stdout=stdout,
            stderr=stderr,
            paypal_client_factory=factory,
            now=now,
        )
        return code, stdout.getvalue(), stderr.getvalue(), factory_calls

    def test_list_capturing_is_read_only_oldest_first_and_filters_age(self):
        older = self.create_capturing("OLD")
        newer = self.create_capturing("NEW")
        self.create_order("PENDING")
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE custom_song_orders SET updated_at = ? WHERE local_order_id = ?",
                ("2026-10-04T10:00:00+00:00", older.local_order_id),
            )
            connection.execute(
                "UPDATE custom_song_orders SET updated_at = ? WHERE local_order_id = ?",
                ("2026-10-04T11:59:30+00:00", newer.local_order_id),
            )

        code, stdout, stderr, factory_calls = self.invoke(
            ["list", "--status", "CAPTURING", "--older-than", "2m", "--limit", "50"],
            now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(factory_calls, [])
        payload = json.loads(stdout)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["local_order_ref"], safe_ref("local", older.local_order_id))
        self.assertEqual(payload[0]["status"], "CAPTURING")
        self.assertNotIn(older.local_order_id, stdout)
        persisted = self.store.get_by_local_order_id(older.local_order_id)
        self.assertEqual(persisted.status, "CAPTURING")
        self.assertEqual(persisted.capture_request_id, "capture-request-OLD")

    def test_inspect_excludes_brief_and_full_identifiers(self):
        record = self.create_capturing("INSPECT")

        code, stdout, stderr, factory_calls = self.invoke(["inspect", record.local_order_id])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(factory_calls, [])
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "CAPTURING")
        self.assertTrue(payload["paypal_order_present"])
        self.assertTrue(payload["capture_request_present"])
        self.assertNotIn(record.local_order_id, stdout)
        self.assertNotIn("SYNTHETICORDERINSPECT", stdout)
        self.assertNotIn("capture-request-INSPECT", stdout)
        self.assertNotIn("PRIVATE_NAME_SENTINEL", stdout)
        self.assertNotIn("brief", stdout.lower())

    def test_reconcile_dry_run_completed_is_eligible_without_mutation(self):
        record = self.create_capturing("DRY")
        paypal = FakeShowOnlyPayPal(self.completed_remote(record.paypal_order_id))

        code, stdout, stderr, _ = self.invoke(["reconcile", record.local_order_id], paypal)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["action"], "eligible_for_apply_paid")
        self.assertFalse(payload["applied"])
        for check in (
            "order_id_matches", "capture_present", "capture_completed",
            "amount_matches", "currency_matches",
        ):
            self.assertTrue(payload[check])
        unchanged = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(unchanged.status, "CAPTURING")
        self.assertIsNone(unchanged.paypal_capture_id)
        self.assertEqual(paypal.capture_calls, 0)

    def test_apply_paid_is_strict_and_second_run_is_idempotent_without_show(self):
        record = self.create_capturing("APPLY")
        paypal = FakeShowOnlyPayPal(self.completed_remote(record.paypal_order_id))

        code, stdout, stderr, _ = self.invoke(
            ["reconcile", record.local_order_id, "--apply-paid"], paypal
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["action"], "paid")
        paid = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(paid.status, "PAID")
        self.assertEqual(paid.paypal_capture_id, "SYNTHETICCAPTURE")
        self.assertEqual(paid.capture_request_id, "capture-request-APPLY")

        code, stdout, stderr, _ = self.invoke(
            ["reconcile", record.local_order_id, "--apply-paid"], paypal
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["action"], "already_reconciled")
        self.assertEqual(len(paypal.show_calls), 1)
        self.assertEqual(paypal.capture_calls, 0)

    def test_remote_approved_requires_normal_retry_with_existing_request_id(self):
        record = self.create_capturing("APPROVED")
        paypal = FakeShowOnlyPayPal({
            "order_id": record.paypal_order_id,
            "order_status": "APPROVED",
        })

        code, stdout, stderr, _ = self.invoke(["reconcile", record.local_order_id], paypal)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["action"],
            "retry_requires_existing_capture_request_id",
        )
        unchanged = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(unchanged.status, "CAPTURING")
        self.assertEqual(unchanged.capture_request_id, "capture-request-APPROVED")
        self.assertEqual(paypal.capture_calls, 0)

    def test_apply_rejects_remote_mismatches_without_mutation(self):
        scenarios = (
            ("AMOUNT", {"amount": "200.00"}, "amount_matches"),
            ("CURRENCY", {"currency": "EUR"}, "currency_matches"),
            ("ORDER", {"order_id": "DIFFERENTORDER"}, "order_id_matches"),
        )
        for suffix, changes, failed_check in scenarios:
            with self.subTest(suffix=suffix):
                record = self.create_capturing(suffix)
                remote = self.completed_remote(record.paypal_order_id)
                remote.update(changes)
                paypal = FakeShowOnlyPayPal(remote)

                code, stdout, stderr, _ = self.invoke(
                    ["reconcile", record.local_order_id, "--apply-paid"], paypal
                )

                self.assertEqual(code, 3)
                self.assertEqual(stderr, "")
                payload = json.loads(stdout)
                self.assertEqual(payload["action"], "manual_review")
                self.assertFalse(payload[failed_check])
                unchanged = self.store.get_by_local_order_id(record.local_order_id)
                self.assertEqual(unchanged.status, "CAPTURING")
                self.assertIsNone(unchanged.paypal_capture_id)
                self.assertEqual(paypal.capture_calls, 0)

    def test_apply_rejects_missing_capture_request_id(self):
        record = self.create_capturing("NOREQUEST")
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE custom_song_orders SET capture_request_id = NULL WHERE local_order_id = ?",
                (record.local_order_id,),
            )
        paypal = FakeShowOnlyPayPal(self.completed_remote(record.paypal_order_id))

        code, stdout, stderr, _ = self.invoke(
            ["reconcile", record.local_order_id, "--apply-paid"], paypal
        )

        self.assertEqual(code, 3)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["action"], "manual_review")
        unchanged = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(unchanged.status, "CAPTURING")
        self.assertIsNone(unchanged.paypal_capture_id)
        self.assertEqual(paypal.capture_calls, 0)

    def test_show_failure_is_safe_and_does_not_mutate(self):
        record = self.create_capturing("TIMEOUT")
        paypal = FakeShowOnlyPayPal(error=PayPalAmbiguousResultError("timeout"))

        code, stdout, stderr, _ = self.invoke(
            ["reconcile", record.local_order_id, "--apply-paid"], paypal
        )

        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "error: PayPal Show Order failed; no local changes were made.\n")
        unchanged = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(unchanged.status, "CAPTURING")
        self.assertEqual(unchanged.capture_request_id, "capture-request-TIMEOUT")
        self.assertEqual(paypal.capture_calls, 0)

    def test_other_states_are_not_mutated_or_sent_to_paypal(self):
        pending = self.create_order("PENDINGSTATE")
        paypal_created_source = self.create_order("PAYPALCREATED")
        paypal_created = self.store.attach_paypal_order(
            paypal_created_source.local_order_id, "PAYPALCREATEDORDER"
        )
        failed_source = self.create_order("FAILEDSTATE")
        failed = self.store.mark_failed(failed_source.local_order_id)
        cancelled = self.create_order("CANCELLEDSTATE")
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE custom_song_orders SET status = 'CANCELLED' WHERE local_order_id = ?",
                (cancelled.local_order_id,),
            )

        for record, expected_status in (
            (pending, "PENDING"),
            (paypal_created, "PAYPAL_CREATED"),
            (failed, "FAILED"),
            (cancelled, "CANCELLED"),
        ):
            with self.subTest(status=expected_status):
                code, stdout, stderr, factory_calls = self.invoke(
                    ["reconcile", record.local_order_id, "--apply-paid"]
                )
                self.assertEqual(code, 3)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["action"], "not_supported")
                self.assertEqual(factory_calls, [])
                unchanged = self.store.get_by_local_order_id(record.local_order_id)
                self.assertEqual(unchanged.status, expected_status)

    def test_stdout_and_stderr_do_not_expose_sensitive_values(self):
        record = self.create_capturing("PRIVATE")
        paypal = FakeShowOnlyPayPal(self.completed_remote(record.paypal_order_id))

        _, inspect_stdout, inspect_stderr, _ = self.invoke(["inspect", record.local_order_id])
        _, reconcile_stdout, reconcile_stderr, _ = self.invoke(
            ["reconcile", record.local_order_id], paypal
        )
        combined = inspect_stdout + inspect_stderr + reconcile_stdout + reconcile_stderr
        forbidden = (
            "PRIVATE_NAME_SENTINEL",
            "PRIVATE_EMAIL_SENTINEL@example.test",
            "PRIVATE_PHONE_SENTINEL",
            "PRIVATE_TOKEN_SENTINEL",
            "SYNTHETIC_CLIENT_SECRET",
            "REMOTE_TOKEN_SENTINEL",
            "REMOTE_PAYER_SENTINEL",
            "Authorization",
            "x-nf-sign",
            "approval_url",
            "SYNTHETICORDERPRIVATE",
            "capture-request-PRIVATE",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, combined)
        self.assertNotIn("brief_json", combined)

    def test_missing_database_and_invalid_config_fail_without_creating_a_db(self):
        missing_path = Path(self.temporary_directory.name) / "missing.sqlite3"
        with patch.dict(os.environ, {"ORDER_DB_PATH": str(missing_path)}, clear=True):
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(["list", "--status", "CAPTURING"], stdout=stdout, stderr=stderr)
        self.assertEqual(code, 4)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: Order database does not exist.\n")
        self.assertFalse(missing_path.exists())

        with patch.dict(os.environ, {"ORDER_DB_PATH": "   "}, clear=True):
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(["inspect", "synthetic-id"], stdout=stdout, stderr=stderr)
        self.assertEqual(code, 4)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: invalid or incomplete configuration.\n")

    def test_reconcile_requires_paypal_configuration_but_read_only_commands_do_not(self):
        record = self.create_capturing("CONFIG")
        with patch.dict(
            os.environ,
            {"ORDER_DB_PATH": str(self.database_path), "PAYPAL_ENVIRONMENT": "sandbox"},
            clear=True,
        ):
            list_stdout = io.StringIO()
            list_stderr = io.StringIO()
            list_code = main(
                ["list", "--status", "CAPTURING"],
                stdout=list_stdout,
                stderr=list_stderr,
            )
            reconcile_stdout = io.StringIO()
            reconcile_stderr = io.StringIO()
            reconcile_code = main(
                ["reconcile", record.local_order_id],
                stdout=reconcile_stdout,
                stderr=reconcile_stderr,
            )

        self.assertEqual(list_code, 0)
        self.assertEqual(list_stderr.getvalue(), "")
        self.assertEqual(reconcile_code, 4)
        self.assertEqual(reconcile_stdout.getvalue(), "")
        self.assertEqual(
            reconcile_stderr.getvalue(),
            "error: PayPal credentials or environment are not configured.\n",
        )

    def test_cli_source_has_no_capture_invocation(self):
        source = (Path(__file__).parent.parent / "backend" / "order_admin.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".capture_order(", source)


if __name__ == "__main__":
    unittest.main()
