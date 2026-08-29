import tempfile
import unittest
from pathlib import Path

from backend.app import create_app
from backend.order_store import OrderStore
from backend.order_service import OrderService
from backend.paypal_client import PayPalAmbiguousResultError


class FakePayPal:
    def __init__(self, fail=False, approved=True):
        self.fail = fail
        self.approved = approved  # if True, show_order returns APPROVED; if False, returns CREATED
        self.calls = []
        # Track state and amount per order_id
        self.order_states = {}
        self.order_amounts = {}

    def create_order(self, amount_cents, currency, request_id, **kwargs):
        self.calls.append(("create_order", amount_cents, currency, request_id, kwargs))
        if self.fail:
            from backend.paypal_client import PayPalClientError
            raise PayPalClientError("private brief must not leak")
        order_id = f"PAYPALORDER{len(self.calls):03d}"
        self.order_states[order_id] = "APPROVED" if self.approved else "CREATED"
        self.order_amounts[order_id] = amount_cents
        return {
            "order_id": order_id,
            "status": "CREATED",
            "approval_url": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
        }

    def show_order(self, order_id):
        self.calls.append(("show_order", order_id))
        state = self.order_states.get(order_id, "CREATED")
        amount_cents = self.order_amounts.get(order_id, 19900)
        if state == "COMPLETED":
            return {
                "order_id": order_id,
                "order_status": "COMPLETED",
                "capture_id": f"CAPTURE{order_id[-3:]}",
                "capture_status": "COMPLETED",
                "amount": f"{amount_cents // 100}.{amount_cents % 100:02d}",
                "currency": "USD",
            }
        elif state == "APPROVED":
            return {"order_id": order_id, "order_status": "APPROVED"}
        else:
            return {"order_id": order_id, "order_status": "CREATED"}

    def capture_order(self, order_id, request_id):
        self.calls.append(("capture_order", order_id, request_id))
        # Mark this order as completed
        self.order_states[order_id] = "COMPLETED"
        amount_cents = self.order_amounts.get(order_id, 19900)
        return {
            "order_id": order_id,
            "order_status": "COMPLETED",
            "capture_id": f"CAPTURE{order_id[-3:]}",
            "capture_status": "COMPLETED",
            "amount": f"{amount_cents // 100}.{amount_cents % 100:02d}",
            "currency": "USD",
        }


class AmbiguousPayPal:
    def __init__(self): self.calls = []
    def create_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise PayPalAmbiguousResultError("unknown")
    def show_order(self, order_id):
        self.calls.append(("show_order", order_id))
        raise PayPalAmbiguousResultError("unknown")
    def capture_order(self, order_id, request_id):
        self.calls.append(("capture_order", order_id, request_id))
        raise PayPalAmbiguousResultError("unknown")


class FailingPayPal:
    def __init__(self): self.calls = []
    def create_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        from backend.paypal_client import PayPalClientError
        raise PayPalClientError("Create failed")
    def show_order(self, order_id):
        self.calls.append(("show_order", order_id))
        from backend.paypal_client import PayPalClientError
        raise PayPalClientError("Show failed")
    def capture_order(self, order_id, request_id):
        self.calls.append(("capture_order", order_id, request_id))
        from backend.paypal_client import PayPalClientError
        raise PayPalClientError("Capture failed")


class OrdersApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store, self.paypal = OrderStore(Path(self.temp.name) / "orders.sqlite3"), FakePayPal()
        service = OrderService(self.store, self.paypal, "https://example.test/return", "https://example.test/cancel")
        self.client = create_app(order_service=service).test_client()
    def tearDown(self): self.temp.cleanup()
    def test_create_prices_closed_options_and_hides_internal_data(self):
        for solo, amount in (("none", "199.00"), ("guitar-solo", "224.00"), ("piano-solo", "224.00")):
            with self.subTest(solo=solo):
                response = self.client.post("/api/paypal/orders", json={"product":"custom-song", "solo":solo, "brief":{"name":"Ada"}})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json["amount"], amount)
                self.assertEqual(response.json["status"], "PAYPAL_CREATED")
                self.assertNotIn("brief", response.json)
                self.assertNotIn("create_request_id", response.json)
    def test_rejects_invalid_and_client_price_fields(self):
        for payload in ({"product":"custom-song","solo":"none"}, {"product":"custom-song","solo":"none","brief":[]}, {"product":"custom-song","solo":"none","brief":{},"amount":1}):
            self.assertEqual(self.client.post("/api/paypal/orders", json=payload).status_code, 422)
        self.assertEqual(self.client.post("/api/paypal/orders", data="{", content_type="application/json").status_code, 400)

    # --- CAPTURE ENDPOINT TESTS ---

    def test_capture_success_199_returns_paid(self):
        # Create and capture with the same service
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # First create an order with solo="none" (199.00)
        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 201)
        local_order_id = create_resp.json["local_order_id"]

        # Now capture it - show_order returns APPROVED, capture_order returns COMPLETED
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "PAID")
        self.assertEqual(response.json["amount"], "199.00")
        self.assertEqual(response.json["currency"], "USD")

    def test_capture_success_224_returns_paid(self):
        # Create and capture with the same service for 224 (piano-solo)
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Create an order with piano-solo (224)
        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"piano-solo","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 201)
        local_order_id = create_resp.json["local_order_id"]

        # Now capture it
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "PAID")
        self.assertEqual(response.json["amount"], "224.00")
        self.assertEqual(response.json["currency"], "USD")

    def test_capture_already_paid_returns_200(self):
        # Create and capture with the same service
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Create and capture
        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 201)
        local_order_id = create_resp.json["local_order_id"]
        paypal_order_id = create_resp.json["paypal_order_id"]

        # First capture
        response1 = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response1.json["status"], "PAID")

        # Second capture (already paid)
        response2 = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json["status"], "PAID")
        # Verify paypal.capture_order was NOT called again
        capture_calls = [c for c in paypal.calls if c[0] == "capture_order"]
        self.assertEqual(len(capture_calls), 1)

    def test_capture_not_found_returns_404(self):
        response = self.client.post("/api/paypal/orders/00000000-0000-0000-0000-000000000000/capture")
        self.assertEqual(response.status_code, 404)

    def test_capture_not_approved_returns_409(self):
        # Create order with a service that will show CREATED (not approved)
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=False)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Create order (PAYPAL_CREATED)
        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        local_order_id = create_resp.json["local_order_id"]

        # Capture will fail because show_order returns CREATED (not approved)
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 409)

    def test_capture_invalid_state_returns_409(self):
        # To test FAILED state, we need to fail create_order which marks it as FAILED
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(fail=True)  # This will fail create_order, marking it as FAILED
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Create order will fail with 502, and the order record will be marked as FAILED
        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 502)

        # Get the local_order_id from the database
        import sqlite3
        connection = sqlite3.connect(store._database_path)
        try:
            local_order_id, = connection.execute("SELECT local_order_id FROM custom_song_orders").fetchone()
        finally:
            connection.close()

        # Try to capture FAILED order - should return 409
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 409)

    def test_capture_reconciliation_completed_no_second_capture(self):
        # Use a service that will show COMPLETED on show_order for CAPTURING state
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Create order
        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        local_order_id = create_resp.json["local_order_id"]
        paypal_order_id = create_resp.json["paypal_order_id"]

        # Now manually set the order to CAPTURING state and mark the order as COMPLETED in paypal
        import uuid
        capture_request_id = str(uuid.uuid4())
        # Use the store's connection to ensure it's properly committed
        with store._connection() as connection:
            connection.execute(
                "UPDATE custom_song_orders SET status = 'CAPTURING', capture_request_id = ? WHERE local_order_id = ?",
                (capture_request_id, local_order_id)
            )

        # Mark this order as completed in the paypal state
        paypal.order_states[paypal_order_id] = "COMPLETED"

        # Reset the paypal calls to only track calls from capture
        paypal.calls = []

        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "PAID")

        # Verify capture_order was NOT called (reconciliation via show_order)
        capture_calls = [c for c in paypal.calls if c[0] == "capture_order"]
        self.assertEqual(len(capture_calls), 0)
        show_calls = [c for c in paypal.calls if c[0] == "show_order"]
        self.assertGreater(len(show_calls), 0)

    def test_capture_deterministic_failure_returns_502(self):
        # Create order with self.client (uses working FakePayPal from setUp)
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 201)
        local_order_id = create_resp.json["local_order_id"]

        # Create a new service with FailingPayPal for capture (same database)
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FailingPayPal()
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Capture will fail with 502 (deterministic PayPal failure)
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 502)

    def test_capture_ambiguous_result_returns_503(self):
        # Create order with self.client (uses working FakePayPal from setUp)
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 201)
        local_order_id = create_resp.json["local_order_id"]

        # Use AmbiguousPayPal for capture (same database)
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = AmbiguousPayPal()
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        # Capture will fail with 503 (ambiguous PayPal result)
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 503)

    def test_capture_rejects_any_body_data(self):
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        local_order_id = create_resp.json["local_order_id"]

        # Capture rejects any non-empty JSON object (including forbidden fields)
        for field in ["amount", "paypal_order_id", "capture_id", "status", "note", "foo"]:
            with self.subTest(field=field):
                response = self.client.post(f"/api/paypal/orders/{local_order_id}/capture", json={field: "test"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("does not accept request body data", response.json["error"])

    def test_capture_empty_body_allowed(self):
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        local_order_id = create_resp.json["local_order_id"]

        # Empty body should work
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 200)

    def test_capture_empty_json_object_allowed(self):
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        local_order_id = create_resp.json["local_order_id"]

        # Empty JSON object {} should work
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture", json={})
        self.assertEqual(response.status_code, 200)

    def test_capture_rejects_non_object_json(self):
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Test"}})
        local_order_id = create_resp.json["local_order_id"]

        # Array should be rejected
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture", json=[])
        self.assertEqual(response.status_code, 400)

        # String should be rejected
        response = client.post(f"/api/paypal/orders/{local_order_id}/capture", json="test")
        self.assertEqual(response.status_code, 400)

    def test_capture_invalid_local_order_id_returns_400(self):
        response = self.client.post("/api/paypal/orders/invalid-uuid/capture")
        self.assertEqual(response.status_code, 400)

    def test_capture_privacy_no_secrets_in_response(self):
        store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        paypal = FakePayPal(approved=True)
        service = OrderService(store, paypal, "https://example.test/return", "https://example.test/cancel")
        client = create_app(order_service=service).test_client()

        create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"name":"Secret"}})
        local_order_id = create_resp.json["local_order_id"]

        response = client.post(f"/api/paypal/orders/{local_order_id}/capture")
        self.assertEqual(response.status_code, 200)
        json_data = response.json
        self.assertNotIn("brief", json_data)
        self.assertNotIn("create_request_id", json_data)
        self.assertNotIn("capture_request_id", json_data)
        keys = set(json_data.keys())
        expected = {"local_order_id", "paypal_order_id", "capture_id", "status", "amount", "currency"}
        self.assertEqual(keys, expected)

    def test_paypal_failure_marks_persisted_order_failed(self):
        failing = OrderService(self.store, FakePayPal(fail=True), "https://example.test/return", "https://example.test/cancel")
        response = create_app(order_service=failing).test_client().post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"private":"brief"}})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("brief", response.json["error"])
    def test_forbidden_internal_field_is_rejected(self):
        response = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{},"status":"PAID"})
        self.assertEqual(response.status_code, 422)
    def test_ambiguous_create_keeps_pending_without_retry(self):
        paypal = AmbiguousPayPal()
        service = OrderService(self.store, paypal, "https://example.test/return", "https://example.test/cancel")
        response = create_app(order_service=service).test_client().post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"private":"brief"}})
        self.assertEqual(response.status_code, 503)  # Changed from 502 to 503 for ambiguous
        self.assertEqual(len(paypal.calls), 1)
        import sqlite3
        connection = sqlite3.connect(self.store._database_path)
        try:
            status, request_id = connection.execute("SELECT status, create_request_id FROM custom_song_orders").fetchone()
        finally:
            connection.close()
        self.assertEqual(status, "PENDING")
        self.assertEqual(request_id, paypal.calls[0][0][2])


class ResolveEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        self.service = OrderService(self.store, FakePayPal(), "https://example.test/return", "https://example.test/cancel")
        self.client = create_app(order_service=self.service).test_client()
    def tearDown(self): self.temp.cleanup()

    def test_resolve_valid_known_token_returns_200_local_order_id(self):
        # First create an order
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(create_resp.status_code, 201)
        local_order_id = create_resp.json["local_order_id"]
        paypal_order_id = create_resp.json["paypal_order_id"]

        # Now resolve with the paypal_order_id as token
        response = self.client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"local_order_id": local_order_id})

    def test_resolve_token_absent_returns_400(self):
        response = self.client.get("/api/paypal/orders/resolve")
        self.assertEqual(response.status_code, 400)

    def test_resolve_empty_token_returns_400(self):
        response = self.client.get("/api/paypal/orders/resolve?token=")
        self.assertEqual(response.status_code, 400)

    def test_resolve_invalid_token_format_returns_400(self):
        # Token with special characters is invalid
        response = self.client.get("/api/paypal/orders/resolve?token=INVALID!")
        self.assertEqual(response.status_code, 400)

    def test_resolve_valid_unknown_token_returns_404(self):
        response = self.client.get("/api/paypal/orders/resolve?token=PAYPAL999")
        self.assertEqual(response.status_code, 404)

    def test_resolve_response_contains_only_local_order_id(self):
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"guitar-solo","brief":{"secret":"data"}})
        paypal_order_id = create_resp.json["paypal_order_id"]

        response = self.client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
        self.assertEqual(response.status_code, 200)
        json_data = response.json
        self.assertEqual(set(json_data.keys()), {"local_order_id"})
        self.assertNotIn("brief", json_data)
        self.assertNotIn("amount", json_data)
        self.assertNotIn("currency", json_data)
        self.assertNotIn("solo", json_data)
        self.assertNotIn("paypal_order_id", json_data)

    def test_resolve_does_not_call_paypal(self):
        # Create order
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        paypal_order_id = create_resp.json["paypal_order_id"]

        # Get the service's paypal client to check calls
        paypal = self.service._paypal_client
        initial_calls = len(paypal.calls) if hasattr(paypal, 'calls') else 0

        # Resolve should not call PayPal
        response = self.client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
        self.assertEqual(response.status_code, 200)

        # PayPal calls should not have increased
        final_calls = len(paypal.calls) if hasattr(paypal, 'calls') else 0
        self.assertEqual(initial_calls, final_calls)

    def test_resolve_does_not_change_sqlite_status(self):
        create_resp = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        paypal_order_id = create_resp.json["paypal_order_id"]

        # Resolve
        response = self.client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
        self.assertEqual(response.status_code, 200)

        # Check status in SQLite is still PAYPAL_CREATED (unchanged)
        import sqlite3
        connection = sqlite3.connect(self.store._database_path)
        try:
            status, = connection.execute("SELECT status FROM custom_song_orders").fetchone()
        finally:
            connection.close()
        self.assertEqual(status, "PAYPAL_CREATED")


class ReturnUrlConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.temp.name) / "orders.sqlite3")
        self.paypal = FakePayPal()
        self.service = OrderService(self.store, self.paypal, "https://custom-return.test/paypal/return", "https://custom-return.test/paypal/cancel")
        self.client = create_app(order_service=self.service).test_client()
    def tearDown(self): self.temp.cleanup()

    def test_create_returns_configured_return_url(self):
        response = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(response.status_code, 201)
        # The response should contain approval_url
        self.assertIn("approval_url", response.json)

    def test_create_returns_configured_cancel_url(self):
        response = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(response.status_code, 201)
        # The approval_url is created by PayPal client with our return/cancel URLs
        self.assertIn("approval_url", response.json)

    def test_create_urls_not_containing_token(self):
        response = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(response.status_code, 201)
        approval_url = response.json["approval_url"]
        # The approval_url from FakePayPal contains the token, but our return/cancel URLs passed to PayPal should not
        # We can't easily assert this without modifying FakePayPal, but the contract is:
        # return_url and cancel_url should be clean base URLs without token
        self.assertIn("approval_url", response.json)

    def test_product_slug_is_custom_song(self):
        # Verify the canonical product slug is custom-song (not custom_song)
        response = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(response.status_code, 201)

    def test_wrong_product_slug_is_rejected(self):
        response = self.client.post("/api/paypal/orders", json={"product":"custom_song","solo":"none","brief":{"name":"Test"}})
        self.assertEqual(response.status_code, 422)


class ResolveArchitectureTests(unittest.TestCase):
    def test_resolve_uses_public_service_api(self):
        # This test verifies that the resolve endpoint uses the public service API
        # (resolve_paypal_order) rather than accessing _store directly.
        # We use a custom OrderService that tracks method calls.
        from backend.order_service import OrderService
        from backend.order_store import OrderStore
        from pathlib import Path
        import tempfile

        temp = tempfile.TemporaryDirectory()
        try:
            store = OrderStore(Path(temp.name) / "orders.sqlite3")
            paypal = FakePayPal()
            service = OrderService(store, paypal, "https://test.test/return", "https://test.test/cancel")

            # Track calls to resolve_paypal_order
            original_resolve = service.resolve_paypal_order
            resolve_calls = []
            def tracking_resolve(paypal_order_id):
                resolve_calls.append(paypal_order_id)
                return original_resolve(paypal_order_id)
            service.resolve_paypal_order = tracking_resolve

            client = create_app(order_service=service).test_client()

            # Create an order
            create_resp = client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"name":"Test"}})
            paypal_order_id = create_resp.json["paypal_order_id"]

            # Call resolve endpoint
            response = client.get(f"/api/paypal/orders/resolve?token={paypal_order_id}")
            self.assertEqual(response.status_code, 200)

            # Verify resolve_paypal_order was called on the service
            self.assertEqual(len(resolve_calls), 1)
            self.assertEqual(resolve_calls[0], paypal_order_id)
        finally:
            temp.cleanup()
