"""Small, server-only PayPal Orders v2 client using the Python standard library."""

import base64
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import ConfigurationError, PayPalConfig, REQUEST_ID_MAX_LENGTH
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,36}$")


def validate_order_id(order_id: str) -> str:
    """Validate the bounded PayPal order ID used by client and smoke tooling."""
    if not isinstance(order_id, str) or not ORDER_ID_PATTERN.fullmatch(order_id):
        raise PayPalClientError("Invalid PayPal order ID.")
    return order_id


class PayPalClientError(RuntimeError):
    """Safe error suitable for an application layer, without secret details."""


class PayPalConfigurationError(PayPalClientError):
    """Raised before a request when required credentials are absent."""


class PayPalResponseError(PayPalClientError):
    """Raised when PayPal's response is invalid or incomplete."""


class PayPalAmbiguousResultError(PayPalClientError):
    """The request may have reached PayPal; reconcile with the same request ID."""


class PayPalClient:
    """Encapsulates OAuth and Orders v2 requests without pricing knowledge."""

    # Loopback hostnames permitted for HTTP in sandbox environment
    _LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

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
        return validate_order_id(order_id)

    def _request_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw_body = response.read()
        except HTTPError as error:
            # HTTP 408/429/5xx are ambiguous for state-mutating operations:
            # PayPal may have processed the request even if we did not get a reliable response.
            if error.code in (408, 429) or (500 <= error.code <= 599):
                raise PayPalAmbiguousResultError(f"PayPal request failed (HTTP {error.code}).") from error
            raise PayPalClientError(f"PayPal request failed (HTTP {error.code}).") from error
        except URLError as error:
            raise PayPalAmbiguousResultError("PayPal request outcome is unknown.") from error

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PayPalAmbiguousResultError("PayPal request outcome is unknown.") from error
        if not isinstance(payload, dict):
            raise PayPalAmbiguousResultError("PayPal request outcome is unknown.")
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

    def check_authentication(self) -> dict[str, bool]:
        """Verify OAuth works without exposing the access token to callers."""
        self._get_access_token()
        return {"authenticated": True}

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

    def _validate_checkout_url(self, url: str, field_name: str) -> str:
        if not isinstance(url, str):
            raise PayPalClientError(f"Invalid {field_name}.")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
            # Accessing port forces urllib to reject non-numeric and out-of-range ports.
            parsed.port

            # Always require: hostname present, no embedded credentials
            if not hostname or parsed.username is not None or parsed.password is not None:
                raise PayPalClientError(f"Invalid {field_name}.")

            # For live: HTTPS is mandatory
            if self._config.environment == "live":
                if parsed.scheme != "https":
                    raise PayPalClientError(f"Invalid {field_name}.")

            # For sandbox: HTTPS always allowed; HTTP allowed only for loopback hosts
            else:  # sandbox
                if parsed.scheme == "https":
                    # HTTPS is always valid
                    pass
                elif parsed.scheme == "http":
                    # HTTP only allowed for loopback hosts
                    if hostname not in self._LOOPBACK_HOSTS:
                        raise PayPalClientError(f"Invalid {field_name}.")
                else:
                    # Any other scheme (ftp, file, etc.) is invalid
                    raise PayPalClientError(f"Invalid {field_name}.")

            return url
        except ValueError:
            raise PayPalClientError(f"Invalid {field_name}.")

    def _extract_approval_url(self, response: dict[str, Any]) -> str:
        links = response.get("links")
        if not isinstance(links, list):
            raise PayPalResponseError("PayPal returned an incomplete order response.")
        for link in links:
            if isinstance(link, dict) and link.get("rel") == "approve":
                href = link.get("href")
                if not isinstance(href, str):
                    break
                try:
                    parsed = urlsplit(href)
                    is_allowed = (
                        parsed.scheme == "https"
                        and parsed.hostname in self._config.approval_hosts
                        and not parsed.username
                        and not parsed.password
                    )
                except ValueError:
                    is_allowed = False
                if is_allowed:
                    return href
                raise PayPalResponseError("PayPal returned an invalid approval URL.")
        raise PayPalResponseError("PayPal returned an incomplete order response.")

    def create_order(
        self,
        amount_cents: int,
        currency: str,
        request_id: str,
        *,
        return_url: str | None = None,
        cancel_url: str | None = None,
    ) -> dict[str, str]:
        """Create one CAPTURE order from a server-calculated amount."""
        amount_value = self._amount_value(amount_cents)
        currency = self._validate_currency(currency)
        request_id = self._validate_request_id(request_id)
        if (return_url is None) != (cancel_url is None):
            raise PayPalClientError("Return and cancel URLs must be provided together.")
        if return_url is not None:
            return_url = self._validate_checkout_url(return_url, "return URL")
            cancel_url = self._validate_checkout_url(cancel_url, "cancel URL")
        access_token = self._get_access_token()
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": currency, "value": amount_value},
            }],
        }
        if return_url is not None:
            payload["application_context"] = {"return_url": return_url, "cancel_url": cancel_url}
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
        return {"order_id": order_id, "status": status, "approval_url": self._extract_approval_url(response)}

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

    def show_order(self, order_id: str) -> dict[str, str | None]:
        """Fetch a PayPal order and return only reconciliation-safe fields."""
        order_id = self._validate_order_id(order_id)
        access_token = self._get_access_token()
        request = Request(
            f"{self._config.api_base_url}/v2/checkout/orders/{order_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        response = self._request_json(request)
        response_order_id = response.get("id")
        if not isinstance(response_order_id, str) or response_order_id != order_id:
            raise PayPalResponseError("PayPal returned an order ID that does not match the requested one.")
        result: dict[str, str | None] = {
            "order_id": order_id,
            "order_status": response.get("status"),
        }
        capture = self._extract_show_capture(response)
        if capture is not None:
            result["capture_id"] = capture.get("id")
            result["capture_status"] = capture.get("status")
            amount = capture.get("amount")
            if isinstance(amount, dict):
                result["amount"] = amount.get("value")
                result["currency"] = amount.get("currency_code")
        return result

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

    @staticmethod
    def _extract_show_capture(response: dict[str, Any]) -> dict[str, Any] | None:
        """Extract capture from order response for show_order; return None if no capture."""
        purchase_units = response.get("purchase_units")
        if not isinstance(purchase_units, list) or len(purchase_units) == 0:
            raise PayPalResponseError("PayPal returned an incomplete order response.")
        payments = purchase_units[0].get("payments")
        if not isinstance(payments, dict):
            return None
        captures = payments.get("captures")
        if not isinstance(captures, list) or len(captures) == 0:
            return None
        capture = captures[0]
        if not isinstance(capture, dict):
            raise PayPalResponseError("PayPal returned an incompatible capture structure.")
        amount = capture.get("amount")
        if not isinstance(amount, dict):
            raise PayPalResponseError("PayPal returned an incompatible capture structure.")
        if (
            not isinstance(capture.get("status"), str)
            or not isinstance(amount.get("value"), str)
            or not isinstance(amount.get("currency_code"), str)
            or (capture.get("id") is not None and not isinstance(capture.get("id"), str))
        ):
            raise PayPalResponseError("PayPal returned an incomplete capture response.")
        return capture
