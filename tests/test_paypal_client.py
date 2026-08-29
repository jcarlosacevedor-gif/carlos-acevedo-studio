import base64
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.config import PayPalConfig
from backend.paypal_client import (
    PayPalClient,
    PayPalClientError,
    PayPalResponseError,
)


CLIENT_ID = "test-client-id"
CLIENT_SECRET = "TEST_SECRET_DO_NOT_USE"
ACCESS_TOKEN = "TEST_ACCESS_TOKEN_DO_NOT_USE"
APPROVAL_URL = "https://www.sandbox.paypal.com/checkoutnow?token=ORDER199"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class PayPalClientTests(unittest.TestCase):
    def setUp(self):
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))

    @staticmethod
    def oauth_response():
        return FakeResponse({"access_token": ACCESS_TOKEN, "token_type": "Bearer"})

    def test_oauth_uses_sandbox_basic_auth_and_grant_type(self):
        with patch("backend.paypal_client.urlopen", return_value=self.oauth_response()) as mocked_open:
            self.assertEqual(self.client._get_access_token(), ACCESS_TOKEN)

        request = mocked_open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api-m.sandbox.paypal.com/v1/oauth2/token")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"grant_type=client_credentials")
        expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        self.assertEqual(request.get_header("Authorization"), f"Basic {expected_basic}")

    def test_authentication_check_discards_the_access_token(self):
        with patch("backend.paypal_client.urlopen", return_value=self.oauth_response()):
            result = self.client.check_authentication()
        self.assertEqual(result, {"authenticated": True})
        self.assertNotIn(ACCESS_TOKEN, result.values())

    def test_oauth_rejects_missing_token_invalid_json_and_http_error_without_secrets(self):
        scenarios = [
            FakeResponse({}),
            FakeResponse(b"not json"),
            HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"{}")),
        ]
        for response in scenarios:
            with self.subTest(response=type(response).__name__):
                with patch("backend.paypal_client.urlopen", side_effect=response if isinstance(response, HTTPError) else None, return_value=None if isinstance(response, HTTPError) else response):
                    with self.assertRaises((PayPalClientError, PayPalResponseError)) as raised:
                        self.client._get_access_token()
                self.assertNotIn(CLIENT_SECRET, str(raised.exception))
                self.assertNotIn(ACCESS_TOKEN, str(raised.exception))

    def test_create_order_builds_one_capture_purchase_unit(self):
        with patch(
            "backend.paypal_client.urlopen",
            side_effect=[self.oauth_response(), FakeResponse({"id": "ORDER199", "status": "CREATED", "links": [{"rel": "approve", "href": APPROVAL_URL}]})],
        ) as mocked_open:
            result = self.client.create_order(19_900, "USD", "create-199")

        self.assertEqual(result, {"order_id": "ORDER199", "status": "CREATED", "approval_url": APPROVAL_URL})
        request = mocked_open.call_args_list[1].args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api-m.sandbox.paypal.com/v2/checkout/orders")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {ACCESS_TOKEN}")
        self.assertEqual(dict((key.lower(), value) for key, value in request.header_items())["paypal-request-id"], "create-199")
        self.assertEqual(payload["intent"], "CAPTURE")
        self.assertEqual(len(payload["purchase_units"]), 1)
        self.assertEqual(payload["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "199.00"})

    def test_create_order_formats_22400_cents(self):
        with patch(
            "backend.paypal_client.urlopen",
            side_effect=[self.oauth_response(), FakeResponse({"id": "ORDER224", "status": "CREATED", "links": [{"rel": "approve", "href": APPROVAL_URL}]})],
        ) as mocked_open:
            self.client.create_order(22_400, "USD", "create-224")
        payload = json.loads(mocked_open.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(payload["purchase_units"][0]["amount"]["value"], "224.00")

    def test_create_order_rejects_invalid_amount_currency_request_id_and_response(self):
        for amount, currency, request_id in ((0, "USD", "id"), (-1, "USD", "id"), (True, "USD", "id"), (19_900, "EUR", "id"), (19_900, "USD", ""), (19_900, "USD", "x" * 109)):
            with self.subTest(amount=amount, currency=currency, request_id=request_id):
                with self.assertRaises(PayPalClientError):
                    self.client.create_order(amount, currency, request_id)

        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse({"status": "CREATED"})]):
            with self.assertRaises(PayPalResponseError):
                self.client.create_order(19_900, "USD", "incomplete-order")

    def test_create_order_uses_only_validated_return_and_cancel_urls(self):
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse({"id": "ORDER199", "status": "CREATED", "links": [{"rel": "approve", "href": APPROVAL_URL}]})]) as mocked_open:
            self.client.create_order(
                19_900, "USD", "urls", return_url="https://example.com/return", cancel_url="https://example.com/cancel",
            )
        payload = json.loads(mocked_open.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(payload["application_context"], {"return_url": "https://example.com/return", "cancel_url": "https://example.com/cancel"})

        for kwargs in (
            {"return_url": "http://example.com/return", "cancel_url": "https://example.com/cancel"},
            {"return_url": "https://user:pass@example.com/return", "cancel_url": "https://example.com/cancel"},
            {"return_url": "https://example.com/return"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PayPalClientError):
                    self.client.create_order(19_900, "USD", "invalid-urls", **kwargs)

    def test_create_order_requires_a_valid_sandbox_approval_url(self):
        invalid_urls = [
            None,
            "http://www.sandbox.paypal.com/checkoutnow?token=ORDER199",
            "https://sandbox.paypal.com.attacker.example/checkoutnow?token=ORDER199",
            "https://user:pass@www.sandbox.paypal.com/checkoutnow?token=ORDER199",
        ]
        for approval_url in invalid_urls:
            with self.subTest(approval_url=approval_url):
                links = [] if approval_url is None else [{"rel": "approve", "href": approval_url}]
                response = {"id": "ORDER199", "status": "CREATED", "links": links}
                with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(response)]):
                    with self.assertRaises(PayPalResponseError):
                        self.client.create_order(19_900, "USD", "invalid-approval")

    def test_capture_order_extracts_only_safe_verification_fields(self):
        capture_response = {
            "id": "ORDER123",
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [{
                "id": "CAPTURE123", "status": "COMPLETED",
                "amount": {"value": "224.00", "currency_code": "USD"},
            }]}}],
        }
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(capture_response)]) as mocked_open:
            result = self.client.capture_order("ORDER123", "capture-123")

        request = mocked_open.call_args_list[1].args[0]
        self.assertEqual(request.full_url, "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER123/capture")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {ACCESS_TOKEN}")
        self.assertEqual(dict((key.lower(), value) for key, value in request.header_items())["paypal-request-id"], "capture-123")
        self.assertEqual(result, {
            "order_id": "ORDER123", "order_status": "COMPLETED", "capture_status": "COMPLETED",
            "amount": "224.00", "currency": "USD", "capture_id": "CAPTURE123",
        })
        self.assertNotIn(ACCESS_TOKEN, result.values())

    def test_capture_rejects_invalid_id_incomplete_response_and_http_error(self):
        with self.assertRaises(PayPalClientError):
            self.client.capture_order("invalid-id!", "capture")

        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse({"id": "ORDER", "status": "COMPLETED"})]):
            with self.assertRaises(PayPalResponseError):
                self.client.capture_order("ORDER", "capture")

        error = HTTPError("https://example.invalid", 500, "Server Error", {}, io.BytesIO(b"{}"))
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), error]):
            with self.assertRaises(PayPalClientError) as raised:
                self.client.capture_order("ORDER", "capture")
        self.assertNotIn(CLIENT_SECRET, str(raised.exception))
        self.assertNotIn(ACCESS_TOKEN, str(raised.exception))
