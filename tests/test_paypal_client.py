import base64
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.config import PayPalConfig
from backend.paypal_client import (
    PayPalAmbiguousResultError,
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

    def test_show_order_uses_get_and_bearer(self):
        order_response = {"id": "ORDER123", "status": "APPROVED", "purchase_units": [{"payments": {}}]}
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]) as mocked_open:
            result = self.client.show_order("ORDER123")
        request = mocked_open.call_args_list[1].args[0]
        self.assertEqual(request.full_url, "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER123")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {ACCESS_TOKEN}")
        self.assertEqual(result, {"order_id": "ORDER123", "order_status": "APPROVED"})

    def test_show_order_returns_capture_fields_for_completed_order(self):
        order_response = {
            "id": "ORDER456",
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [{
                "id": "CAPTURE789",
                "status": "COMPLETED",
                "amount": {"value": "199.00", "currency_code": "USD"},
            }]}}],
        }
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            result = self.client.show_order("ORDER456")
        self.assertEqual(result["order_id"], "ORDER456")
        self.assertEqual(result["order_status"], "COMPLETED")
        self.assertEqual(result["capture_id"], "CAPTURE789")
        self.assertEqual(result["capture_status"], "COMPLETED")
        self.assertEqual(result["amount"], "199.00")
        self.assertEqual(result["currency"], "USD")

    def test_show_order_returns_only_order_fields_for_approved_without_capture(self):
        order_response = {"id": "ORDER789", "status": "APPROVED", "purchase_units": [{"payments": {}}]}
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            result = self.client.show_order("ORDER789")
        self.assertEqual(result, {"order_id": "ORDER789", "order_status": "APPROVED"})
        self.assertNotIn("capture_id", result)
        self.assertNotIn("capture_status", result)

    def test_show_order_returns_only_order_fields_for_created_without_capture(self):
        order_response = {"id": "ORDCREATED", "status": "CREATED", "purchase_units": [{"payments": {"captures": []}}]}
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            result = self.client.show_order("ORDCREATED")
        self.assertEqual(result, {"order_id": "ORDCREATED", "order_status": "CREATED"})

    def test_show_order_rejects_invalid_order_id(self):
        with self.assertRaises(PayPalClientError):
            self.client.show_order("invalid-id!")
        with self.assertRaises(PayPalClientError):
            self.client.show_order("")

    def test_show_order_rejects_invalid_json_response(self):
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(b"not json")]):
            with self.assertRaises(PayPalAmbiguousResultError):
                self.client.show_order("ORDER123")

    def test_show_order_rejects_incomplete_response_missing_purchase_units(self):
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse({"id": "ORDER123", "status": "APPROVED"})]):
            with self.assertRaises(PayPalResponseError):
                self.client.show_order("ORDER123")

    def test_show_order_rejects_invalid_capture_structure(self):
        order_response = {
            "id": "ORDER123",
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [{"status": 123}]}}],
        }
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            with self.assertRaises(PayPalResponseError):
                self.client.show_order("ORDER123")

    def test_show_order_rejects_http_error(self):
        error = HTTPError("https://example.invalid", 404, "Not Found", {}, io.BytesIO(b"{}"))
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), error]):
            with self.assertRaises(PayPalClientError):
                self.client.show_order("ORDER123")

    def test_show_order_rejects_ambiguous_network_error(self):
        from urllib.error import URLError
        error = URLError("Network error")
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), error]):
            with self.assertRaises(PayPalAmbiguousResultError):
                self.client.show_order("ORDER123")

    def test_show_order_does_not_expose_secrets_in_exceptions(self):
        error = HTTPError("https://example.invalid", 500, "Server Error", {}, io.BytesIO(b'{"error":"bad"}'))
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), error]):
            with self.assertRaises(PayPalClientError) as raised:
                self.client.show_order("ORDER123")
        self.assertNotIn(CLIENT_SECRET, str(raised.exception))
        self.assertNotIn(ACCESS_TOKEN, str(raised.exception))

    def test_show_order_result_does_not_contain_sensitive_fields(self):
        order_response = {
            "id": "ORDER123",
            "status": "COMPLETED",
            "payer": {"email": "user@example.com", "name": "Test User"},
            "payment_source": {"paypal": {}},
            "links": [{"href": "https://example.com", "rel": "self"}],
            "purchase_units": [{"payments": {"captures": [{
                "id": "CAPTURE123",
                "status": "COMPLETED",
                "amount": {"value": "199.00", "currency_code": "USD"},
            }]}}],
        }
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            result = self.client.show_order("ORDER123")
        self.assertNotIn("payer", result)
        self.assertNotIn("payment_source", result)
        self.assertNotIn("links", result)
        self.assertNotIn("email", str(result))
        self.assertNotIn("user@example.com", str(result))

    def test_show_order_rejects_mismatched_response_order_id(self):
        order_response = {
            "id": "DIFFERENTORDER456",
            "status": "APPROVED",
            "purchase_units": [{"payments": {}}],
        }
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            with self.assertRaises(PayPalResponseError) as raised:
                self.client.show_order("SANDBOXORDER123")
        self.assertNotIn("DIFFERENTORDER456", str(raised.exception))
        self.assertNotIn("SANDBOXORDER123", str(raised.exception))

    def test_show_order_rejects_missing_response_order_id(self):
        order_response = {
            "status": "APPROVED",
            "purchase_units": [{"payments": {}}],
        }
        with patch("backend.paypal_client.urlopen", side_effect=[self.oauth_response(), FakeResponse(order_response)]):
            with self.assertRaises(PayPalResponseError):
                self.client.show_order("ORDER123")

    # Tests for _validate_checkout_url with HTTP/HTTPS and sandbox/live environments
    def test_sandbox_allows_https_urls(self):
        """Sandbox environment must allow HTTPS URLs on any valid hostname."""
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        self.assertEqual(self.client._validate_checkout_url("https://example.com/return", "return URL"), "https://example.com/return")
        self.assertEqual(self.client._validate_checkout_url("https://paypal.example.com/cancel", "cancel URL"), "https://paypal.example.com/cancel")

    def test_sandbox_allows_http_for_loopback_hosts(self):
        """Sandbox environment must allow HTTP for loopback hosts."""
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        # 127.0.0.1
        self.assertEqual(self.client._validate_checkout_url("http://127.0.0.1:8000/paypal/return", "return URL"), "http://127.0.0.1:8000/paypal/return")
        # localhost
        self.assertEqual(self.client._validate_checkout_url("http://localhost:8000/paypal/return", "return URL"), "http://localhost:8000/paypal/return")
        # IPv6 loopback
        self.assertEqual(self.client._validate_checkout_url("http://[::1]:8000/paypal/return", "return URL"), "http://[::1]:8000/paypal/return")

    def test_sandbox_rejects_http_for_non_loopback_hosts(self):
        """Sandbox environment must reject HTTP for non-loopback hosts."""
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http://example.com/return", "return URL")
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http://paypal.example.com/cancel", "cancel URL")
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http://localhost.evil.com/return", "return URL")
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http://127.0.0.1.evil.com/return", "return URL")

    def test_live_requires_https_only(self):
        """Live environment must require HTTPS for all hosts."""
        self.client = PayPalClient(PayPalConfig("live", CLIENT_ID, CLIENT_SECRET))
        # HTTPS always allowed
        self.assertEqual(self.client._validate_checkout_url("https://example.com/return", "return URL"), "https://example.com/return")
        self.assertEqual(self.client._validate_checkout_url("https://127.0.0.1:8000/paypal/return", "return URL"), "https://127.0.0.1:8000/paypal/return")
        # HTTP always rejected for live
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http://example.com/return", "return URL")
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http://127.0.0.1:8000/return", "return URL")

    def test_validate_checkout_url_rejects_non_http_schemes(self):
        """Any non-http(s) scheme must be rejected in both environments."""
        for scheme in ["ftp", "file", "mailto", "javascript"]:
            with self.subTest(scheme=scheme):
                self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
                with self.assertRaises(PayPalClientError):
                    self.client._validate_checkout_url(f"{scheme}://example.com/return", "return URL")

    def test_validate_checkout_url_rejects_missing_hostname(self):
        """URLs without hostname must be rejected."""
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("http:///return", "return URL")
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url("https:///return", "return URL")

    def test_validate_checkout_url_rejects_embedded_credentials(self):
        """URLs with embedded username/password must be rejected."""
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        for url in (
            "http://@127.0.0.1:8000/paypal/return",
            "http://user@127.0.0.1:8000/paypal/return",
            "http://user:@127.0.0.1:8000/paypal/return",
            "http://:pass@127.0.0.1:8000/paypal/return",
            "http://:@127.0.0.1:8000/paypal/return",
            "https://@example.com/return",
            "https://user:pass@example.com/return",
        ):
            with self.subTest(url=url):
                with self.assertRaises(PayPalClientError):
                    self.client._validate_checkout_url(url, "return URL")

    def test_validate_checkout_url_rejects_invalid_port(self):
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        for url in ("http://127.0.0.1:not-a-port/paypal/return", "http://127.0.0.1:65536/paypal/return"):
            with self.subTest(url=url):
                with self.assertRaises(PayPalClientError):
                    self.client._validate_checkout_url(url, "return URL")

    def test_validate_checkout_url_rejects_non_string(self):
        """Non-string URLs must be rejected."""
        self.client = PayPalClient(PayPalConfig("sandbox", CLIENT_ID, CLIENT_SECRET))
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url(None, "return URL")
        with self.assertRaises(PayPalClientError):
            self.client._validate_checkout_url(123, "return URL")

    def test_create_order_validates_urls_before_oauth(self):
        """URL validation must happen before OAuth/access token is requested."""
        # Patch _get_access_token to track if it's called
        original_get_access_token = self.client._get_access_token
        access_token_called = []

        def track_get_access_token():
            access_token_called.append(True)
            return original_get_access_token()

        self.client._get_access_token = track_get_access_token

        # Test with invalid HTTP URL for non-loopback in sandbox
        # This should fail validation before OAuth is called
        access_token_called.clear()
        with self.assertRaises(PayPalClientError):
            self.client.create_order(19_900, "USD", "test-req",
                                       return_url="http://example.com/return",
                                       cancel_url="http://example.com/cancel")
        self.assertEqual(len(access_token_called), 0,
                        "OAuth should NOT be called when URL validation fails")

    def test_create_order_rejects_embedded_credentials_before_oauth_or_create(self):
        for url in ("http://@127.0.0.1:8000/paypal/return", "https://user@example.com/return"):
            with self.subTest(url=url):
                with patch("backend.paypal_client.urlopen") as mocked_open:
                    with self.assertRaises(PayPalClientError):
                        self.client.create_order(
                            19_900, "USD", "embedded-credentials", return_url=url,
                            cancel_url="http://127.0.0.1:8000/paypal/cancel",
                        )
                mocked_open.assert_not_called()
