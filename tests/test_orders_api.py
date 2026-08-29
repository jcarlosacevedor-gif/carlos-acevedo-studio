import os
import unittest
from unittest.mock import patch

from backend.app import create_app


class PlaceholderOrdersApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "sandbox"}, clear=True)
        self.environment.start()
        self.client = create_app().test_client()

    def tearDown(self):
        self.environment.stop()

    def test_valid_configurations_are_server_priced(self):
        for solo, amount_cents in (("none", 19_900), ("guitar-solo", 22_400), ("piano-solo", 22_400)):
            with self.subTest(solo=solo):
                response = self.client.post("/api/paypal/orders", json={"product": "custom-song", "solo": solo})
                self.assertEqual(response.status_code, 501)
                self.assertEqual(response.json["status"], "not_configured")
                self.assertEqual(response.json["pricing"]["amount_cents"], amount_cents)

    def test_client_price_and_invalid_options_are_rejected(self):
        for payload in (
            {"product": "custom-song", "solo": "none", "price": 1},
            {"product": "unknown", "solo": "none"},
            {"product": "custom-song", "solo": "unknown"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/paypal/orders", json=payload)
                self.assertEqual(response.status_code, 422)

    def test_invalid_json_is_sanitized(self):
        response = self.client.post("/api/paypal/orders", data="{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_missing_json_body_is_sanitized(self):
        response = self.client.post("/api/paypal/orders")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_capture_is_placeholder_only(self):
        invalid = self.client.post("/api/paypal/orders/invalid-id!/capture")
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post("/api/paypal/orders/ORDER123/capture")
        self.assertEqual(valid.status_code, 501)
        self.assertEqual(valid.json["status"], "not_implemented")
