import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.app import create_app
from backend.config import ConfigurationError, get_stale_order_thresholds
from backend.order_admin import main as admin_main
from backend.order_store import OrderStore
from backend.stale_audit import StaleAuditReport, StaleFinding, audit_stale


OPS_TOKEN = "ops_" + "x" * 40
AUTHORIZATION = {"Authorization": f"Bearer {OPS_TOKEN}"}


class NoPayPalCalls:
    def __init__(self):
        self.show_calls = 0
        self.capture_calls = 0

    def show_order(self, *args, **kwargs):
        self.show_calls += 1
        raise AssertionError("The audit endpoint must not call PayPal Show.")

    def capture_order(self, *args, **kwargs):
        self.capture_calls += 1
        raise AssertionError("The audit endpoint must not call PayPal Capture.")


class InternalAuditEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "orders.sqlite3"
        self.store = OrderStore(self.database_path)
        self.paypal = NoPayPalCalls()
        self.environment = patch.dict(
            os.environ,
            {
                "PAYPAL_ENVIRONMENT": "sandbox",
                "OPS_AUDIT_TOKEN": OPS_TOKEN,
                "ORDER_DB_PATH": str(self.database_path),
            },
            clear=True,
        )
        self.environment.start()
        self.app = create_app(database_path=self.database_path, paypal_client=self.paypal)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def report(exit_code=0, findings=()):
        return StaleAuditReport(tuple(findings), exit_code)

    def assert_private_response(self, response, status_code, status):
        self.assertEqual(response.status_code, status_code)
        self.assertEqual(response.get_json(), {"status": status})
        self.assertEqual(set(response.get_json()), {"status"})
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_missing_and_wrong_auth_are_identical_and_do_not_open_store_or_scan(self):
        with patch("backend.app.OrderStore") as store_class, patch("backend.app.audit_stale") as scanner:
            missing = self.client.post("/internal/audit-stale")
            wrong = self.client.post(
                "/internal/audit-stale",
                headers={"Authorization": "Bearer PRIVATE_WRONG_TOKEN_SENTINEL"},
            )
        for response in (missing, wrong):
            self.assert_private_response(response, 403, "error")
            self.assertNotIn("WWW-Authenticate", response.headers)
        self.assertEqual(missing.data, wrong.data)
        store_class.assert_not_called()
        scanner.assert_not_called()

    def test_valid_auth_uses_compare_digest_and_scans_exactly_once(self):
        with patch("backend.app.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare, patch(
            "backend.app.audit_stale", return_value=self.report()
        ) as scanner:
            response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)
        self.assert_private_response(response, 200, "ok")
        compare.assert_called_once()
        scanner.assert_called_once()

    def test_query_or_any_body_is_400_and_never_scans(self):
        with patch("backend.app.audit_stale") as scanner:
            query = self.client.post("/internal/audit-stale?unexpected=1", headers=AUTHORIZATION)
            raw = self.client.post("/internal/audit-stale", headers=AUTHORIZATION, data=b"x")
            json_body = self.client.post("/internal/audit-stale", headers=AUTHORIZATION, json={})
        for response in (query, raw, json_body):
            self.assert_private_response(response, 400, "error")
        scanner.assert_not_called()

    def test_non_post_methods_are_405_and_never_scan(self):
        with patch("backend.app.audit_stale") as scanner:
            responses = (
                self.client.get("/internal/audit-stale", headers=AUTHORIZATION),
                self.client.put("/internal/audit-stale", headers=AUTHORIZATION),
                self.client.options("/internal/audit-stale", headers=AUTHORIZATION),
            )
        for response in responses:
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        scanner.assert_not_called()

    def test_result_mapping_including_informative_paypal_created(self):
        informative = StaleFinding(
            "safe-ref", "PAYPAL_CREATED", 86401, "probable_abandoned_checkout", 0
        )
        cases = (
            (self.report(), 200, "ok"),
            (self.report(findings=(informative,)), 200, "ok"),
            (self.report(2), 200, "warning"),
            (self.report(3), 409, "critical"),
            (self.report(99), 500, "error"),
        )
        for report, code, status in cases:
            with self.subTest(exit_code=report.highest_exit), patch(
                "backend.app.audit_stale", return_value=report
            ):
                response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)
                self.assert_private_response(response, code, status)

    def test_sqlite_error_is_private_500_even_if_logging_fails(self):
        exception_sentinel = "PRIVATE_EXCEPTION_SENTINEL"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "backend.app.audit_stale",
            side_effect=sqlite3.OperationalError(exception_sentinel),
        ), patch("backend.app.emit_event", side_effect=RuntimeError("logger failed")), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)
        self.assert_private_response(response, 500, "error")
        combined = response.get_data(as_text=True) + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(exception_sentinel, combined)
        self.assertNotIn(OPS_TOKEN, combined)

    def test_event_logging_failure_does_not_change_success_or_critical(self):
        warning = StaleFinding(
            "safe-warning", "PENDING", 301, "attention", 2,
            "WARNING", "stale_warning", "attention",
        )
        critical = StaleFinding(
            "safe-critical", "CAPTURING", 1801, "critical_manual_review", 3,
            "ERROR", "stale_critical", "critical",
        )
        for report, code, status in (
            (self.report(), 200, "ok"),
            (self.report(2, (warning,)), 200, "warning"),
            (self.report(3, (critical,)), 409, "critical"),
        ):
            with self.subTest(status=status), patch(
                "backend.app.audit_stale", return_value=report
            ) as scanner, patch(
                "backend.stale_audit.emit_event", side_effect=RuntimeError("logger failed")
            ):
                response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)
            self.assert_private_response(response, code, status)
            scanner.assert_called_once()

    def test_store_is_opened_read_only_without_initialization(self):
        real_store = OrderStore
        with patch("backend.app.OrderStore", wraps=real_store) as store_class, patch(
            "backend.app.audit_stale", return_value=self.report()
        ):
            response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)
        self.assertEqual(response.status_code, 200)
        store_class.assert_called_once_with(self.database_path, initialize=False, read_only=True)

    def test_missing_database_returns_500_without_creating_it(self):
        missing = Path(self.temporary_directory.name) / "missing.sqlite3"
        app = create_app(database_path=missing, paypal_client=self.paypal)
        app.testing = True
        response = app.test_client().post("/internal/audit-stale", headers=AUTHORIZATION)
        self.assert_private_response(response, 500, "error")
        self.assertFalse(missing.exists())

    def test_real_scan_preserves_database_and_never_calls_paypal(self):
        record = self.store.create_order_record(
            product="custom-song",
            solo="guitar-solo",
            amount_cents=19900,
            currency="USD",
            brief={"name": "PRIVATE_BRIEF_SENTINEL"},
            create_request_id="PRIVATE_CREATE_ID_SENTINEL",
        )
        self.store.attach_paypal_order(record.local_order_id, "PRIVATEPAYPALIDSENTINEL")
        self.store.begin_capture(record.local_order_id, "PRIVATE_CAPTURE_ID_SENTINEL")
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE custom_song_orders SET updated_at = ? WHERE local_order_id = ?",
                ("2000-01-01T00:00:00+00:00", record.local_order_id),
            )
        before = self.database_path.read_bytes()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)

        self.assert_private_response(response, 409, "critical")
        self.assertEqual(before, self.database_path.read_bytes())
        persisted = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(persisted.status, "CAPTURING")
        self.assertEqual(persisted.capture_request_id, "PRIVATE_CAPTURE_ID_SENTINEL")
        self.assertEqual(self.paypal.show_calls, 0)
        self.assertEqual(self.paypal.capture_calls, 0)
        response_text = response.get_data(as_text=True) + stdout.getvalue() + stderr.getvalue()
        for sentinel in (
            OPS_TOKEN,
            "PRIVATE_BRIEF_SENTINEL",
            record.local_order_id,
            "PRIVATEPAYPALIDSENTINEL",
            "PRIVATE_CAPTURE_ID_SENTINEL",
        ):
            self.assertNotIn(sentinel, response_text)

    def test_cli_and_endpoint_share_report_severity_and_cli_payload(self):
        record = self.store.create_order_record(
            product="custom-song", solo="guitar-solo", amount_cents=19900,
            currency="USD", brief={}, create_request_id="parity-request",
        )
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE custom_song_orders SET updated_at = ? WHERE local_order_id = ?",
                ("2026-10-06T11:50:00+00:00", record.local_order_id),
            )
        now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
        report = audit_stale(
            OrderStore(self.database_path, initialize=False, read_only=True),
            get_stale_order_thresholds(),
            now,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = admin_main(["audit-stale"], stdout=stdout, stderr=stderr, now=now)
        self.assertEqual(code, report.highest_exit)
        self.assertEqual(json.loads(stdout.getvalue()), report.as_payload())
        self.assertEqual(stderr.getvalue(), "")
        with patch("backend.app.audit_stale", return_value=report):
            response = self.client.post("/internal/audit-stale", headers=AUTHORIZATION)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "warning"})


class InternalAuditConfigurationTests(unittest.TestCase):
    def test_sandbox_without_token_starts_and_rejects_every_call(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PAYPAL_ENVIRONMENT": "sandbox"},
            clear=True,
        ):
            path = Path(directory) / "orders.sqlite3"
            OrderStore(path)
            app = create_app(database_path=path)
            app.testing = True
            with patch("backend.app.OrderStore") as store_class, patch("backend.app.audit_stale") as scanner:
                response = app.test_client().post("/internal/audit-stale")
            self.assertEqual(response.status_code, 403)
            store_class.assert_not_called()
            scanner.assert_not_called()

    def test_live_without_ops_token_fails_startup_safely(self):
        environment = {
            "PAYPAL_ENVIRONMENT": "live",
            "NETLIFY_PROXY_SIGNING_SECRET": "proxy-secret",
            "NETLIFY_PROXY_EXPECTED_SITE_ID": "site-id",
            "NETLIFY_PROXY_EXPECTED_SITE_URL": "https://example.test",
            "NETLIFY_PROXY_EXPECTED_DEPLOY_CONTEXT": "production",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                create_app()
        self.assertNotIn("proxy-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
