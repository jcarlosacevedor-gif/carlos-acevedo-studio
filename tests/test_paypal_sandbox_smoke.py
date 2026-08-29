import io
import unittest
import uuid
from unittest.mock import patch

from backend.paypal_client import PayPalClientError
from backend.paypal_sandbox_smoke import load_sandbox_config, main, run_create


CLIENT_SECRET = "TEST_SECRET_DO_NOT_USE"
ACCESS_TOKEN = "TEST_ACCESS_TOKEN_DO_NOT_USE"
ENVIRONMENT = {"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET}


class FakePayPalClient:
    def __init__(self, config):
        self.config = config
        self.create_arguments = None
        self.capture_arguments = None
        self.capture_result = None

    def check_authentication(self):
        return {"authenticated": True}

    def create_order(self, amount_cents, currency, request_id, **kwargs):
        self.create_arguments = (amount_cents, currency, request_id, kwargs)
        return {"order_id": "SANDBOXORDER123", "status": "CREATED", "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=SANDBOXORDER123"}

    def capture_order(self, order_id, request_id):
        self.capture_arguments = (order_id, request_id)
        return self.capture_result or {"order_id": order_id, "order_status": "COMPLETED", "capture_status": "COMPLETED", "capture_id": "CAPTURE123", "amount": "199.00", "currency": "USD"}


class PayPalSandboxSmokeTests(unittest.TestCase):
    def invoke(self, args, client=None, input_fn=lambda prompt: "y"):
        client = client or FakePayPalClient(None)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(args, environ=ENVIRONMENT, input_fn=input_fn, client_factory=lambda config: client, request_id_factory=lambda: uuid.UUID("12345678-1234-5678-1234-567812345678"), stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue(), client

    def test_interactive_credentials_stay_in_memory_and_live_is_rejected(self):
        config = load_sandbox_config(environ={"PAYPAL_ENVIRONMENT": "sandbox"}, input_fn=lambda prompt: "test-client-id", secret_fn=lambda prompt: CLIENT_SECRET)
        self.assertEqual(config.client_secret, CLIENT_SECRET)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["auth"], environ={"PAYPAL_ENVIRONMENT": "live"}, client_factory=lambda config: self.fail("Live must not instantiate client"), stdout=stdout, stderr=stderr)
        self.assertEqual(code, 1)
        self.assertIn("only permits", stderr.getvalue())

    def test_auth_output_is_safe(self):
        code, output, error, _ = self.invoke(["auth"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "PayPal Sandbox authentication: OK\n")
        self.assertEqual(error, "")
        self.assertNotIn(CLIENT_SECRET, output)
        self.assertNotIn(ACCESS_TOKEN, output)

    def test_create_uses_pricing_for_each_closed_solo_option(self):
        expected = {"none": 19_900, "guitar-solo": 22_400, "piano-solo": 22_400}
        for solo, cents in expected.items():
            with self.subTest(solo=solo):
                code, output, error, client = self.invoke(["create", "--solo", solo])
                self.assertEqual(code, 0)
                self.assertEqual(client.create_arguments[:2], (cents, "USD"))
                self.assertIn(f"Configuration: {solo}", output)
                self.assertIn(f"Amount: ${cents / 100:.2f} USD", output)
                self.assertIn("Approval URL:", output)
                self.assertEqual(error, "")
                self.assertNotIn(CLIENT_SECRET, output)

    def test_create_uses_pricing_function_and_request_id(self):
        client = FakePayPalClient(None)
        with patch("backend.paypal_sandbox_smoke.calculate_custom_song_price", return_value={"amount_cents": 22_400, "currency": "USD"}) as pricing:
            run_create(client, "guitar-solo", request_id_factory=lambda: uuid.UUID("12345678-1234-5678-1234-567812345678"))
        pricing.assert_called_once_with({"product": "custom-song", "solo": "guitar-solo"})
        self.assertEqual(client.create_arguments[2], "12345678-1234-5678-1234-567812345678")

    def test_cli_rejects_unknown_solo_and_manual_price_arguments(self):
        with self.assertRaises(SystemExit):
            main(["create", "--solo", "unknown"], environ=ENVIRONMENT)
        with self.assertRaises(SystemExit):
            main(["create", "--solo", "none", "--amount", "1"], environ=ENVIRONMENT)

    def test_capture_requires_confirmation_and_uses_exact_configuration(self):
        code, output, error, client = self.invoke(["capture", "SANDBOXORDER123", "--solo", "guitar-solo"], input_fn=lambda prompt: "n")
        self.assertEqual(code, 0)
        self.assertIsNone(client.capture_arguments)
        self.assertIn("Expected configuration: guitar-solo", output)
        self.assertIn("Expected amount: $224.00 USD", output)
        self.assertIn("Capture cancelled.", output)

        code, output, error, client = self.invoke(["capture", "SANDBOXORDER123", "--solo", "guitar-solo"])
        self.assertEqual(code, 1)
        self.assertEqual(client.capture_arguments[0], "SANDBOXORDER123")
        self.assertNotIn("PAYMENT CONFIRMED", output)
        self.assertIn("Payment verification failed", error)

    def test_capture_confirms_matching_199_and_224(self):
        for solo, amount in (("none", "199.00"), ("guitar-solo", "224.00"), ("piano-solo", "224.00")):
            with self.subTest(solo=solo):
                client = FakePayPalClient(None)
                client.capture_result = {"order_id": "SANDBOXORDER123", "order_status": "COMPLETED", "capture_status": "COMPLETED", "capture_id": "CAPTURE123", "amount": amount, "currency": "USD"}
                code, output, error, client = self.invoke(["capture", "SANDBOXORDER123", "--solo", solo], client=client)
                self.assertEqual(code, 0)
                self.assertIn("PAYMENT CONFIRMED", output)
                self.assertNotIn(CLIENT_SECRET, output + error)

    def test_capture_rejects_wrong_amount_currency_or_status(self):
        for field, value in (("amount", "199.00"), ("currency", "EUR"), ("order_status", "CREATED"), ("capture_status", "PENDING")):
            with self.subTest(field=field):
                client = FakePayPalClient(None)
                client.capture_result = {"order_id": "SANDBOXORDER123", "order_status": "COMPLETED", "capture_status": "COMPLETED", "capture_id": "CAPTURE123", "amount": "224.00", "currency": "USD"}
                client.capture_result[field] = value
                code, output, error, _ = self.invoke(["capture", "SANDBOXORDER123", "--solo", "guitar-solo"], client=client)
                self.assertEqual(code, 1)
                self.assertNotIn("PAYMENT CONFIRMED", output)
                self.assertIn("Payment verification failed", error)

    def test_capture_invalid_solo_is_rejected_by_cli(self):
        with self.assertRaises(SystemExit):
            main(["capture", "SANDBOXORDER123", "--solo", "unknown"], environ=ENVIRONMENT)
