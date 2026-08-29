import tempfile
import unittest
from pathlib import Path

from backend.app import create_app
from backend.order_store import OrderStore
from backend.order_service import OrderService
from backend.paypal_client import PayPalAmbiguousResultError


class FakePayPal:
    def __init__(self, fail=False): self.fail, self.calls = fail, []
    def create_order(self, amount_cents, currency, request_id, **kwargs):
        self.calls.append((amount_cents, currency, request_id, kwargs))
        if self.fail: raise RuntimeError("private brief must not leak")
        order_id = f"PAYPALORDER{len(self.calls):03d}"
        return {"order_id": order_id, "status": "CREATED", "approval_url": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}


class AmbiguousPayPal:
    def __init__(self): self.calls = []
    def create_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise PayPalAmbiguousResultError("unknown")


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
    def test_capture_is_still_placeholder(self):
        self.assertEqual(self.client.post("/api/paypal/orders/ORDER123/capture").status_code, 501)
    def test_paypal_failure_marks_persisted_order_failed(self):
        failing = OrderService(self.store, FakePayPal(fail=True), "https://example.test/return", "https://example.test/cancel")
        response = create_app(order_service=failing).test_client().post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"private":"brief"}})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("brief", response.json["error"])
        self.assertEqual(self.store.get_by_local_order_id(next(iter([r.local_order_id for r in [self.store.get_by_paypal_order_id("missing")] if r])) if False else "missing"), None)
    def test_forbidden_internal_field_is_rejected(self):
        response = self.client.post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{},"status":"PAID"})
        self.assertEqual(response.status_code, 422)
    def test_ambiguous_create_keeps_pending_without_retry(self):
        paypal = AmbiguousPayPal()
        service = OrderService(self.store, paypal, "https://example.test/return", "https://example.test/cancel")
        response = create_app(order_service=service).test_client().post("/api/paypal/orders", json={"product":"custom-song","solo":"none","brief":{"private":"brief"}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(paypal.calls), 1)
        import sqlite3
        connection = sqlite3.connect(self.store._database_path)
        try:
            status, request_id = connection.execute("SELECT status, create_request_id FROM custom_song_orders").fetchone()
        finally:
            connection.close()
        self.assertEqual(status, "PENDING")
        self.assertEqual(request_id, paypal.calls[0][0][2])
