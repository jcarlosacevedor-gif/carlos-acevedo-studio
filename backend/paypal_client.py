"""Small, server-only PayPal Orders v2 client using the Python standard library."""

import base64
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import ConfigurationError, PayPalConfig


REQUEST_ID_MAX_LENGTH = 108
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,36}$")


class PayPalClientError(RuntimeError):
    """Safe error suitable for an application layer, without secret details."""


class PayPalConfigurationError(PayPalClientError):
    """Raised before a request when required credentials are absent."""


class PayPalResponseError(PayPalClientError):
    """Raised when PayPal's response is invalid or incomplete."""


class PayPalClient:
    """Encapsulates OAuth and Orders v2 requests without pricing knowledge."""

    def __init__(self, config: PayPalConfig, timeout_seconds: float = 15.0):
        if not config.client_id or not config.client_secret:
            raise PayPalConfigurationError("PayPal credentials are not configured.")
        self._config = config
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "PayPalClient":
        try:
            return cls(PayPalConfig.from_environment(require_credentials=True))
        except ConfigurationError as error:
            raise PayPalConfigurationError("PayPal credentials are not configured.") from error

    @staticmethod
    def _validate_request_id(request_id: str) -> str:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > REQUEST_ID_MAX_LENGTH:
            raise PayPalClientError("Invalid PayPal request ID.")
        return request_id

    @staticmethod
    def _validate_order_id(order_id: str) -> str:
        if not isinstance(order_id, str) or not ORDER_ID_PATTERN.fullmatch(order_id):
            raise PayPalClientError("Invalid PayPal order ID.")
        return order_id

    def _request_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw_body = response.read()
        except HTTPError as error:
            raise PayPalClientError(f"PayPal request failed (HTTP {error.code}).") from error
        except URLError as error:
            raise PayPalClientError("PayPal request could not be completed.") from error

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PayPalResponseError("PayPal returned an invalid response.") from error
        if not isinstance(payload, dict):
            raise PayPalResponseError("PayPal returned an invalid response.")
        return payload

    def _get_access_token(self) -> str:
        credentials = f"{self._config.client_id}:{self._config.client_secret}".encode("utf-8")
        basic_authorization = base64.b64encode(credentials).decode("ascii")
        request = Request(
            f"{self._config.api_base_url}/v1/oauth2/token",
            data=urlencode({"grant_type": "client_credentials"}).encode("ascii"),
            headers={
                "Authorization": f"Basic {basic_authorization}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        payload = self._request_json(request)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise PayPalResponseError("PayPal returned an incomplete OAuth response.")
        return access_token

    @staticmethod
    def _amount_value(amount_cents: int) -> str:
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
            raise PayPalClientError("Invalid payment amount.")
        return f"{amount_cents // 100}.{amount_cents % 100:02d}"

    @staticmethod
    def _validate_currency(currency: str) -> str:
        if currency != "USD":
            raise PayPalClientError("Unsupported currency.")
        return currency

    def create_order(self, amount_cents: int, currency: str, request_id: str) -> dict[str, str]:
        """Create one CAPTURE order from a server-calculated amount."""
        amount_value = self._amount_value(amount_cents)
        currency = self._validate_currency(currency)
        request_id = self._validate_request_id(request_id)
        access_token = self._get_access_token()
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": currency, "value": amount_value},
            }],
        }
        request = Request(
            f"{self._config.api_base_url}/v2/checkout/orders",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": request_id,
            },
            method="POST",
        )
        response = self._request_json(request)
        order_id = response.get("id")
        status = response.get("status")
        if not isinstance(order_id, str) or not order_id or not isinstance(status, str) or not status:
            raise PayPalResponseError("PayPal returned an incomplete order response.")
        return {"order_id": order_id, "status": status}

    def capture_order(self, order_id: str, request_id: str) -> dict[str, str | None]:
        """Capture a payer-approved order and return only verification fields."""
        order_id = self._validate_order_id(order_id)
        request_id = self._validate_request_id(request_id)
        access_token = self._get_access_token()
        request = Request(
            f"{self._config.api_base_url}/v2/checkout/orders/{order_id}/capture",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": request_id,
            },
            method="POST",
        )
        response = self._request_json(request)
        capture = self._extract_capture(response)
        captured_order_id = response.get("id")
        order_status = response.get("status")
        if not isinstance(captured_order_id, str) or not isinstance(order_status, str):
            raise PayPalResponseError("PayPal returned an incomplete capture response.")
        return {
            "order_id": captured_order_id,
            "order_status": order_status,
            "capture_status": capture["status"],
            "amount": capture["amount"]["value"],
            "currency": capture["amount"]["currency_code"],
            "capture_id": capture.get("id"),
        }

    @staticmethod
    def _extract_capture(response: dict[str, Any]) -> dict[str, Any]:
        try:
            capture = response["purchase_units"][0]["payments"]["captures"][0]
            amount = capture["amount"]
        except (KeyError, IndexError, TypeError) as error:
            raise PayPalResponseError("PayPal returned an incomplete capture response.") from error
        if (
            not isinstance(capture.get("status"), str)
            or not isinstance(amount.get("value"), str)
            or not isinstance(amount.get("currency_code"), str)
            or (capture.get("id") is not None and not isinstance(capture.get("id"), str))
        ):
            raise PayPalResponseError("PayPal returned an incomplete capture response.")
        return capture
