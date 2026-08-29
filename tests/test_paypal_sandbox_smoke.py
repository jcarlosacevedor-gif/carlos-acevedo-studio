import io
import unittest
import uuid
from unittest.mock import patch

from backend.paypal_client import PayPalClientError
from backend.paypal_sandbox_smoke import load_sandbox_config, main, run_create_199


CLIENT_SECRET = "TEST_SECRET_DO_NOT_USE"
ACCESS_TOKEN = "TEST_ACCESS_TOKEN_DO_NOT_USE"


class FakePayPalClient:
    def __init__(self, config):
        self.config = config
        self.auth_checked = False
        self.create_arguments = None
        self.create_options = None
        self.capture_arguments = None
        self.capture_result = None

    def check_authentication(self):
        self.auth_checked = True
        return {"authenticated": True}

    def create_order(self, amount_cents, currency, request_id, **kwargs):
        self.create_arguments = (amount_cents, currency, request_id)
        self.create_options = kwargs
        return {"order_id": "SANDBOXORDER123", "status": "CREATED", "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=SANDBOXORDER123"}

    def capture_order(self, order_id, request_id):
        self.capture_arguments = (order_id, request_id)
        if self.capture_result is not None:
            return self.capture_result
        return {
            "order_id": order_id,
            "order_status": "COMPLETED",
            "capture_status": "COMPLETED",
            "capture_id": "CAPTURE123",
            "amount": "199.00",
            "currency": "USD",
        }


class PayPalSandboxSmokeTests(unittest.TestCase):
    def test_interactive_credentials_stay_in_memory(self):
        prompts = []
        config = load_sandbox_config(
            environ={"PAYPAL_ENVIRONMENT": "sandbox"},
            input_fn=lambda prompt: prompts.append(prompt) or "test-client-id",
            secret_fn=lambda prompt: prompts.append(prompt) or CLIENT_SECRET,
        )
        self.assertEqual(config.client_id, "test-client-id")
        self.assertEqual(config.client_secret, CLIENT_SECRET)
        self.assertEqual(len(prompts), 2)

    def test_live_environment_is_rejected_before_client_creation(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = main(
            ["auth"],
            environ={"PAYPAL_ENVIRONMENT": "live"},
            client_factory=lambda config: self.fail("Client must not be created"),
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("only permits", stderr.getvalue())

    def test_auth_success_is_sanitized(self):
        created = []
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = main(
            ["auth"],
            environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
            client_factory=lambda config: created.append(FakePayPalClient(config)) or created[-1],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(created[0].auth_checked)
        self.assertEqual(stdout.getvalue(), "PayPal Sandbox authentication: OK\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(CLIENT_SECRET, stdout.getvalue())
        self.assertNotIn(ACCESS_TOKEN, stdout.getvalue())

    def test_auth_failure_is_sanitized(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = main(
            ["auth"],
            environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
            client_factory=lambda config: (_ for _ in ()).throw(PayPalClientError("Authentication failed.")),
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("Authentication failed.", stderr.getvalue())
        self.assertNotIn(CLIENT_SECRET, stderr.getvalue())
        self.assertNotIn(ACCESS_TOKEN, stderr.getvalue())

    def test_create_199_uses_authoritative_pricing_and_new_request_id(self):
        client = FakePayPalClient(None)
        request_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        with patch("backend.paypal_sandbox_smoke.calculate_custom_song_price", return_value={"amount_cents": 19_900, "currency": "USD"}) as pricing:
            result = run_create_199(client, request_id_factory=lambda: request_id)
        self.assertEqual(result["order_id"], "SANDBOXORDER123")
        pricing.assert_called_once_with({"product": "custom-song", "solo": "none"})
        self.assertEqual(client.create_arguments, (19_900, "USD", str(request_id)))

    def test_create_199_output_is_safe(self):
        created = []
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = main(
            ["create-199"],
            environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
            client_factory=lambda config: created.append(FakePayPalClient(config)) or created[-1],
            request_id_factory=lambda: uuid.UUID("12345678-1234-5678-1234-567812345678"),
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(created[0].create_arguments, (19_900, "USD", "12345678-1234-5678-1234-567812345678"))
        self.assertIn("Order ID: SANDBOXORDER123", stdout.getvalue())
        self.assertIn("Amount: $199.00 USD", stdout.getvalue())
        self.assertIn("Approval URL: https://www.sandbox.paypal.com/checkoutnow?token=SANDBOXORDER123", stdout.getvalue())
        self.assertNotIn(CLIENT_SECRET, stdout.getvalue())
        self.assertNotIn(ACCESS_TOKEN, stdout.getvalue())
        self.assertNotIn("Basic ", stdout.getvalue())
        self.assertNotIn("Bearer ", stdout.getvalue())

    def test_capture_cancelled_or_invalid_never_calls_client(self):
        created = []
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = main(
            ["capture", "SANDBOXORDER123"],
            environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
            input_fn=lambda prompt: "n",
            client_factory=lambda config: created.append(FakePayPalClient(config)) or created[-1],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 0)
        self.assertIsNone(created[0].capture_arguments)
        self.assertIn("Capture cancelled.", stdout.getvalue())

        invalid = main(
            ["capture", "invalid-id!"],
            environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
            client_factory=lambda config: self.fail("Invalid ID must not create a client"),
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(invalid, 1)

    def test_capture_confirms_only_completed_base_order(self):
        created = []
        order_id = "SANDBOXORDER123"
        stdout, stderr = io.StringIO(), io.StringIO()
        request_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        exit_code = main(
            ["capture", order_id],
            environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
            input_fn=lambda prompt: "y",
            client_factory=lambda config: created.append(FakePayPalClient(config)) or created[-1],
            request_id_factory=lambda: request_id,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(created[0].capture_arguments, (order_id, str(request_id)))
        self.assertIn("PAYMENT CONFIRMED", stdout.getvalue())
        self.assertNotIn(CLIENT_SECRET, stdout.getvalue())
        self.assertNotIn(ACCESS_TOKEN, stdout.getvalue())

    def test_capture_rejects_wrong_status_amount_or_currency(self):
        for field, value in (("order_status", "CREATED"), ("capture_status", "PENDING"), ("amount", "224.00"), ("currency", "EUR")):
            with self.subTest(field=field):
                client = FakePayPalClient(None)
                client.capture_result = client.capture_order("SANDBOXORDER123", "unused")
                client.capture_result[field] = value
                stdout, stderr = io.StringIO(), io.StringIO()
                exit_code = main(
                    ["capture", "SANDBOXORDER123"],
                    environ={"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "test-client-id", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
                    input_fn=lambda prompt: "y",
                    client_factory=lambda config, client=client: client,
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(exit_code, 1)
                self.assertNotIn("PAYMENT CONFIRMED", stdout.getvalue())
                self.assertIn("Payment verification failed", stderr.getvalue())
                self.assertNotIn(CLIENT_SECRET, stderr.getvalue())
