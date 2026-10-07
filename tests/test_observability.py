import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from backend.config import ConfigurationError, get_stale_order_thresholds
from backend.observability import REASON_CODES, emit_event, reason_code_for_exception, safe_ref
from backend.order_admin import main as admin_main
from backend.order_service import OrderService, OrderServiceError
from backend.order_store import OrderStore
from backend.paypal_client import PayPalAmbiguousResultError


class SuccessfulPayPal:
    def __init__(self, sequence=None):
        self.sequence = sequence
        self.capture_calls = 0

    def create_order(self, amount_cents, currency, request_id, **kwargs):
        return {
            "order_id": "PAYPALORDER123",
            "status": "CREATED",
            "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=PAYPALORDER123",
        }

    def show_order(self, order_id):
        return {"order_id": order_id, "order_status": "APPROVED"}

    def capture_order(self, order_id, request_id):
        self.capture_calls += 1
        if self.sequence is not None:
            self.sequence.append("paypal_capture")
        return {
            "order_id": order_id,
            "order_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "capture_status": "COMPLETED",
            "amount": "199.00",
            "currency": "USD",
        }


class ObservabilityHelperTests(unittest.TestCase):
    def test_safe_refs_are_stable_type_separated_and_fixed_length(self):
        identifier = "SECRET_IDENTIFIER_123"
        expected = hashlib.sha256(f"local\0{identifier}".encode()).hexdigest()[:12]
        local = safe_ref("local", identifier)
        self.assertEqual(local, f"local_{expected}")
        self.assertEqual(local, safe_ref("local", identifier))
        self.assertNotEqual(local.split("_", 1)[1], safe_ref("paypal_order", identifier).split("_", 1)[1])
        self.assertEqual(len(local), len("local_") + 12)
        self.assertNotIn(identifier, local)

    def test_emitter_writes_one_allowlisted_json_line_and_omits_null(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        ok = emit_event(
            "order_created_local",
            stdout=stdout,
            stderr=stderr,
            local_order_ref=safe_ref("local", "LOCAL123"),
            paypal_order_ref=None,
            status_to="PENDING",
            operation="create_order",
            outcome="committed",
            source="api",
        )
        self.assertTrue(ok)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn("paypal_order_ref", payload)
        self.assertEqual(payload["schema_version"], 1)

    def test_emitter_rejects_arbitrary_fields_reason_codes_and_objects(self):
        stdout = io.StringIO()
        self.assertFalse(emit_event("operational_error", stdout=stdout, secret="SENTINEL"))
        self.assertFalse(emit_event("operational_error", stdout=stdout, reason_code="SENTINEL"))
        self.assertFalse(emit_event("operational_error", stdout=stdout, outcome=object()))
        self.assertEqual(stdout.getvalue(), "")

    def test_event_specific_levels_are_enforced(self):
        for event, invalid_level in (("capture_reconciled", "ERROR"), ("stale_order_detected", "INFO")):
            with self.subTest(event=event, invalid_level=invalid_level):
                stdout, stderr = io.StringIO(), io.StringIO()
                self.assertFalse(emit_event(event, level=invalid_level, stdout=stdout, stderr=stderr))
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

        for event, valid_levels in (
            ("capture_reconciled", ("INFO", "WARNING")),
            ("stale_order_detected", ("WARNING", "ERROR")),
        ):
            for level in valid_levels:
                with self.subTest(event=event, level=level):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    self.assertTrue(emit_event(event, level=level, stdout=stdout, stderr=stderr))
                    emitted = stdout.getvalue() + stderr.getvalue()
                    self.assertEqual(emitted.count("\n"), 1)
                    self.assertEqual(json.loads(emitted)["level"], level)

    def test_reason_code_allowlist_and_exception_classification_are_closed(self):
        required = {
            "timeout", "network", "http_408", "http_429", "paypal_5xx",
            "invalid_response", "create_ambiguous", "sqlite_error",
            "configuration_error", "show_failed", "stale_warning", "stale_critical",
        }
        self.assertTrue(required.issubset(REASON_CODES))
        self.assertEqual(reason_code_for_exception(TimeoutError("PRIVATE")), "timeout")
        self.assertEqual(reason_code_for_exception(URLError("PRIVATE")), "network")
        for status, expected in (
            (400, "invalid_response"),
            (401, "invalid_response"),
            (403, "invalid_response"),
            (408, "http_408"),
            (429, "http_429"),
            (503, "paypal_5xx"),
        ):
            with self.subTest(status=status):
                error = HTTPError("https://example.invalid/?token=PRIVATE", status, "PRIVATE", {}, None)
                self.assertEqual(reason_code_for_exception(error), expected)


class ServiceEventTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "orders.sqlite3"
        self.store = OrderStore(self.database_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def service(self, paypal):
        return OrderService(self.store, paypal, "https://example.com/return", "https://example.com/cancel")

    def create_attached(self):
        record = self.store.create_order_record(
            product="custom-song",
            solo="guitar-solo",
            amount_cents=19900,
            currency="USD",
            brief={"name": "PRIVATE_NAME_SENTINEL", "token": "PRIVATE_TOKEN_SENTINEL"},
            create_request_id="CREATE_REQUEST_SENTINEL",
        )
        return self.store.attach_paypal_order(record.local_order_id, "PAYPALORDER123")

    def test_create_events_follow_committed_states_and_hide_inputs(self):
        events = []
        with patch("backend.order_service.emit_event", side_effect=lambda event, **fields: events.append((event, fields)) or True):
            result = self.service(SuccessfulPayPal()).create_order({
                "product": "custom-song",
                "solo": "guitar-solo",
                "brief": {"name": "PRIVATE_NAME_SENTINEL", "email": "PRIVATE_EMAIL_SENTINEL"},
            })
        self.assertEqual([event for event, _ in events], ["order_created_local", "paypal_order_created"])
        self.assertEqual(self.store.get_by_local_order_id(result["local_order_id"]).status, "PAYPAL_CREATED")
        serialized = json.dumps(events)
        for sentinel in ("PRIVATE_NAME_SENTINEL", "PRIVATE_EMAIL_SENTINEL", result["local_order_id"], result["paypal_order_id"]):
            self.assertNotIn(sentinel, serialized)

    def test_capture_started_precedes_post_and_paid_emits_once(self):
        sequence = []
        paypal = SuccessfulPayPal(sequence)
        record = self.create_attached()

        def service_event(event, **fields):
            sequence.append(event)
            return True

        def store_event(event, **fields):
            sequence.append(event)
            return True

        with patch("backend.order_service.emit_event", side_effect=service_event), patch("backend.order_store.emit_event", side_effect=store_event):
            service = self.service(paypal)
            self.assertEqual(service.capture_order(record.local_order_id)["status"], "PAID")
            self.assertEqual(service.capture_order(record.local_order_id)["status"], "PAID")

        self.assertLess(sequence.index("capture_started"), sequence.index("paypal_capture"))
        self.assertEqual(sequence.count("paid"), 1)
        self.assertEqual(paypal.capture_calls, 1)

    def test_capture_ambiguous_warns_and_preserves_request_id(self):
        record = self.create_attached()
        paypal = SuccessfulPayPal()
        paypal.capture_order = lambda *args: (_ for _ in ()).throw(PayPalAmbiguousResultError("RAW_SECRET_SENTINEL"))
        events = []
        with patch("backend.order_service.emit_event", side_effect=lambda event, **fields: events.append((event, fields)) or True):
            with self.assertRaises(OrderServiceError):
                self.service(paypal).capture_order(record.local_order_id)
        updated = self.store.get_by_local_order_id(record.local_order_id)
        self.assertEqual(updated.status, "CAPTURING")
        self.assertTrue(updated.capture_request_id)
        ambiguous = next(fields for event, fields in events if event == "capture_ambiguous")
        self.assertEqual(ambiguous["reason_code"], "invalid_response")
        self.assertNotIn("RAW_SECRET_SENTINEL", json.dumps(events))

    def test_capture_started_is_not_repeated_after_ambiguous_retry(self):
        class AmbiguousThenSuccessful(SuccessfulPayPal):
            def __init__(self):
                super().__init__()
                self.request_ids = []

            def capture_order(self, order_id, request_id):
                self.capture_calls += 1
                self.request_ids.append(request_id)
                if self.capture_calls == 1:
                    raise PayPalAmbiguousResultError("uncertain")
                return super().capture_order(order_id, request_id)

        record = self.create_attached()
        paypal = AmbiguousThenSuccessful()
        events = []
        with patch("backend.order_service.emit_event", side_effect=lambda event, **fields: events.append(event) or True):
            service = self.service(paypal)
            with self.assertRaises(OrderServiceError):
                service.capture_order(record.local_order_id)
            after_ambiguous = self.store.get_by_local_order_id(record.local_order_id)
            self.assertEqual(after_ambiguous.status, "CAPTURING")
            self.assertEqual(service.capture_order(record.local_order_id)["status"], "PAID")

        self.assertEqual(events.count("capture_started"), 1)
        self.assertEqual(len(paypal.request_ids), 2)
        self.assertEqual(paypal.request_ids[0], paypal.request_ids[1])

    def test_sensitive_sentinels_never_reach_stdout_or_stderr(self):
        record = self.create_attached()
        sensitive = (
            "Authorization: Bearer AUTH_SENTINEL",
            "x-nf-sign: SIGN_SENTINEL",
            "Client Secret CLIENT_SECRET_SENTINEL",
            "access token ACCESS_TOKEN_SENTINEL",
            "approval URL https://www.paypal.com/checkoutnow?token=APPROVAL_SENTINEL",
            "query string ?token=QUERY_SENTINEL&PayerID=PAYER_SENTINEL",
            "body complete BODY_SENTINEL",
            "header complete HEADER_SENTINEL",
            "PayPal token PAYPAL_TOKEN_SENTINEL",
            "PayerID PAYER_ID_SENTINEL",
        )

        class SensitiveFailure(SuccessfulPayPal):
            def capture_order(self, *args):
                raise PayPalAmbiguousResultError(" | ".join(sensitive))

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(OrderServiceError):
            self.service(SensitiveFailure()).capture_order(record.local_order_id)
        updated = self.store.get_by_local_order_id(record.local_order_id)
        combined = stdout.getvalue() + stderr.getvalue()
        forbidden = sensitive + (
            record.local_order_id,
            record.paypal_order_id,
            updated.capture_request_id,
            "CREATE_REQUEST_SENTINEL",
            "PRIVATE_NAME_SENTINEL",
            "PRIVATE_TOKEN_SENTINEL",
        )
        for sentinel in forbidden:
            self.assertNotIn(sentinel, combined)

    def test_logger_failure_does_not_change_success_or_retry_capture(self):
        record = self.create_attached()
        paypal = SuccessfulPayPal()
        with patch("backend.order_service.emit_event", side_effect=RuntimeError("logger failed")), patch("backend.order_store.emit_event", side_effect=RuntimeError("logger failed")):
            result = self.service(paypal).capture_order(record.local_order_id)
        self.assertEqual(result["status"], "PAID")
        self.assertEqual(paypal.capture_calls, 1)

    def test_create_ambiguous_is_reduced_to_allowlisted_reason(self):
        class AmbiguousCreate(SuccessfulPayPal):
            def create_order(self, *args, **kwargs):
                raise PayPalAmbiguousResultError("TOKEN_SENTINEL?query=SECRET")

        events = []
        with patch("backend.order_service.emit_event", side_effect=lambda event, **fields: events.append((event, fields)) or True):
            with self.assertRaises(OrderServiceError):
                self.service(AmbiguousCreate()).create_order({"product": "custom-song", "solo": "none", "brief": {}})
        error_event = next(fields for event, fields in events if event == "operational_error")
        self.assertEqual(error_event["reason_code"], "create_ambiguous")
        self.assertNotIn("TOKEN_SENTINEL", json.dumps(events))


class AdminObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "orders.sqlite3"
        self.store = OrderStore(self.database_path)
        self.environment = patch.dict(os.environ, {"ORDER_DB_PATH": str(self.database_path)}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def create(self, suffix, status, updated_at):
        record = self.store.create_order_record(
            product="custom-song", solo="none", amount_cents=19900, currency="USD",
            brief={"name": "PRIVATE_SENTINEL"}, create_request_id=f"request-{suffix}",
        )
        if status in {"PAYPAL_CREATED", "CAPTURING"}:
            record = self.store.attach_paypal_order(record.local_order_id, f"PAYPAL{suffix}")
        if status == "CAPTURING":
            record = self.store.begin_capture(record.local_order_id, f"capture-{suffix}")
        with self.store._connection() as connection:
            connection.execute("UPDATE custom_song_orders SET updated_at = ? WHERE local_order_id = ?", (updated_at, record.local_order_id))
        return self.store.get_admin_order(record.local_order_id)

    def invoke(self, argv, now=None, paypal=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        factory_calls = []

        def factory():
            factory_calls.append(True)
            if paypal is None:
                raise AssertionError("PayPal must not be called")
            return paypal

        code = admin_main(argv, stdout=stdout, stderr=stderr, paypal_client_factory=factory, now=now)
        return code, stdout.getvalue(), stderr.getvalue(), factory_calls

    def test_audit_stale_classifies_all_states_without_mutating_database(self):
        capturing_warning = self.create("CAPWARN", "CAPTURING", "2026-10-06T11:53:00+00:00")
        capturing_critical = self.create("CAPCRIT", "CAPTURING", "2026-10-06T11:20:00+00:00")
        pending_attention = self.create("PENDWARN", "PENDING", "2026-10-06T11:53:00+00:00")
        pending_manual = self.create("PENDCRIT", "PENDING", "2026-10-06T11:20:00+00:00")
        abandoned = self.create("ABANDON", "PAYPAL_CREATED", "2026-10-05T10:00:00+00:00")
        before = self.database_path.read_bytes()
        events = []
        with patch("backend.order_admin.emit_event", side_effect=lambda event, **fields: events.append((event, fields)) or True):
            code, stdout, stderr, factory_calls = self.invoke(
                ["audit-stale"], now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
            )
        after = self.database_path.read_bytes()
        self.assertEqual(code, 3)
        self.assertEqual(stderr, "")
        self.assertEqual(factory_calls, [])
        self.assertEqual(before, after)
        payload = json.loads(stdout)
        self.assertEqual(payload["result"], "incidents")
        classifications = {item["local_order_ref"]: item["classification"] for item in payload["findings"]}
        self.assertEqual(classifications[safe_ref("local", capturing_warning.local_order_id)], "warning")
        self.assertEqual(classifications[safe_ref("local", capturing_critical.local_order_id)], "critical_manual_review")
        self.assertEqual(classifications[safe_ref("local", pending_attention.local_order_id)], "attention")
        self.assertEqual(classifications[safe_ref("local", pending_manual.local_order_id)], "manual_review")
        self.assertEqual(classifications[safe_ref("local", abandoned.local_order_id)], "probable_abandoned_checkout")
        self.assertTrue(all(event == "stale_order_detected" for event, _ in events))
        self.assertEqual(len(events), 4)
        self.assertNotIn(safe_ref("local", abandoned.local_order_id), json.dumps(events))
        self.assertNotIn("PRIVATE_SENTINEL", stdout + json.dumps(events))

    def test_audit_warning_exit_two_and_old_paypal_created_alone_exit_zero(self):
        warning = self.create("WARN", "CAPTURING", "2026-10-06T11:53:00+00:00")
        with patch("backend.order_admin.emit_event"):
            code, stdout, _, _ = self.invoke(["audit-stale"], now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["result"], "incidents")
        with self.store._connection() as connection:
            connection.execute("UPDATE custom_song_orders SET status='PAYPAL_CREATED', capture_request_id=NULL WHERE local_order_id=?", (warning.local_order_id,))
            connection.execute("UPDATE custom_song_orders SET updated_at='2026-10-05T10:00:00+00:00' WHERE local_order_id=?", (warning.local_order_id,))
        with patch("backend.order_admin.emit_event") as emit:
            code, stdout, _, factory_calls = self.invoke(["audit-stale"], now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(code, 0)
        self.assertEqual(factory_calls, [])
        payload = json.loads(stdout)
        self.assertEqual(payload["findings"][0]["classification"], "probable_abandoned_checkout")
        self.assertEqual(payload["result"], "ok")
        emit.assert_not_called()

    def test_safe_ref_resolves_exactly_and_rejects_partial_or_collision(self):
        record = self.create("REF", "PENDING", "2026-10-06T12:00:00+00:00")
        reference = safe_ref("local", record.local_order_id)
        code, stdout, stderr, _ = self.invoke(["inspect", "--ref", reference])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["local_order_ref"], reference)
        self.assertNotIn(record.local_order_id, stdout)
        code, _, stderr, _ = self.invoke(["inspect", "--ref", reference[:-1]])
        self.assertEqual(code, 4)
        self.assertEqual(stderr, "error: order database operation failed safely.\n")
        with patch.object(OrderStore, "find_admin_orders_by_local_ref", return_value=[record, record]):
            code, _, stderr, _ = self.invoke(["inspect", "--ref", reference])
        self.assertEqual(code, 4)
        self.assertEqual(stderr, "error: Order reference is not unique.\n")

    def test_reconcile_emits_evaluation_and_one_paid_event(self):
        record = self.create("RECON", "CAPTURING", "2026-10-06T12:00:00+00:00")

        class ShowOnly:
            def show_order(self, order_id):
                return {
                    "order_id": order_id, "order_status": "COMPLETED",
                    "capture_id": "CAPTURERECON", "capture_status": "COMPLETED",
                    "amount": "199.00", "currency": "USD",
                }

            def capture_order(self, *args):
                raise AssertionError("Capture must never be called")

        admin_events, store_events = [], []
        reference = safe_ref("local", record.local_order_id)
        with patch("backend.order_admin.emit_event", side_effect=lambda event, **fields: admin_events.append(event) or True), patch("backend.order_store.emit_event", side_effect=lambda event, **fields: store_events.append(event) or True):
            first = self.invoke(["reconcile", "--ref", reference, "--apply-paid"], paypal=ShowOnly())
            second = self.invoke(["reconcile", "--ref", reference, "--apply-paid"], paypal=ShowOnly())
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertEqual(admin_events.count("capture_reconciled"), 1)
        self.assertEqual(store_events.count("paid"), 1)


class ThresholdConfigurationTests(unittest.TestCase):
    def test_defaults_and_invalid_thresholds(self):
        with patch.dict(os.environ, {}, clear=True):
            thresholds = get_stale_order_thresholds()
            self.assertEqual((thresholds.capturing_warning_seconds, thresholds.capturing_critical_seconds), (300, 1800))
        invalid_cases = [
            {"ORDER_CAPTURING_WARNING_SECONDS": "0"},
            {"ORDER_CAPTURING_WARNING_SECONDS": "secret"},
            {"ORDER_CAPTURING_WARNING_SECONDS": "300", "ORDER_CAPTURING_CRITICAL_SECONDS": "300"},
        ]
        for environment in invalid_cases:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ConfigurationError) as raised:
                    get_stale_order_thresholds()
                self.assertNotIn("secret", str(raised.exception).lower())
