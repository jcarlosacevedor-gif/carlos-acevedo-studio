"""Application service for the future web PayPal Create flow and Capture."""

from __future__ import annotations

import json
from typing import Any
import uuid

from .order_store import OrderStore, OrderStoreError
from .paypal_client import PayPalAmbiguousResultError, PayPalClientError
from .pricing import PricingError, calculate_custom_song_price


FORBIDDEN_FIELDS = frozenset({"amount", "amount_cents", "price", "total", "currency", "quantity", "paypal_order_id", "capture_id", "status", "create_request_id", "capture_request_id"})


def _format_amount_cents(amount_cents: int) -> str:
    """Format positive integer cents as a decimal string without floating point."""
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"


class OrderServiceError(ValueError):
    def __init__(self, message: str, status_code: int):
        self.status_code = status_code
        super().__init__(message)


class OrderService:
    def __init__(self, store: OrderStore, paypal_client: Any, return_url: str, cancel_url: str):
        self._store, self._paypal_client = store, paypal_client
        self._return_url, self._cancel_url = return_url, cancel_url

    def create_order(self, payload: object) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise OrderServiceError("A JSON object is required.", 400)
        if FORBIDDEN_FIELDS.intersection(payload):
            raise OrderServiceError("Client payment and internal fields are not accepted.", 422)
        brief = payload.get("brief")
        if not isinstance(brief, dict):
            raise OrderServiceError("Brief must be a JSON object.", 422)
        try:
            json.dumps(brief, allow_nan=False)
            pricing = calculate_custom_song_price({"product": payload.get("product"), "solo": payload.get("solo")})
        except (TypeError, ValueError, PricingError) as error:
            raise OrderServiceError("Invalid Custom Song configuration or brief.", 422) from error
        request_id = str(uuid.uuid4())
        try:
            record = self._store.create_order_record(product=pricing["product"], solo=pricing["solo"], amount_cents=pricing["amount_cents"], currency=pricing["currency"], brief=brief, create_request_id=request_id)
        except OrderStoreError as error:
            raise OrderServiceError("Could not persist the local order.", 500) from error
        try:
            remote = self._paypal_client.create_order(record.amount_cents, record.currency, record.create_request_id, return_url=self._return_url, cancel_url=self._cancel_url)
            order_id, approval_url = remote["order_id"], remote["approval_url"]
        except PayPalAmbiguousResultError as error:
            raise OrderServiceError("PayPal order creation outcome requires recovery.", 502) from error
        except (KeyError, TypeError, PayPalClientError, RuntimeError) as error:
            self._store.mark_failed(record.local_order_id)
            raise OrderServiceError("PayPal order creation failed.", 502) from error
        try:
            attached = self._store.attach_paypal_order(record.local_order_id, order_id)
        except OrderStoreError as error:
            raise OrderServiceError("PayPal order requires recovery before it can be used.", 500) from error
        return {"local_order_id": attached.local_order_id, "paypal_order_id": attached.paypal_order_id, "status": attached.status, "approval_url": approval_url, "amount": f"{attached.amount_cents // 100}.{attached.amount_cents % 100:02d}", "currency": attached.currency}

    def capture_order(self, local_order_id: str) -> dict[str, str]:
        """Orchestrate capture: lookup, PayPal show/capture, verification, and mark_paid.

        Returns a sanitized result with: local_order_id, paypal_order_id, capture_id,
        status, amount, currency. Never exposes brief, request IDs, tokens, or secrets.
        """
        # Step 2: Lookup local
        try:
            record = self._store.get_by_local_order_id(local_order_id)
        except Exception:
            raise OrderServiceError("Local order lookup failed.", 500)
        if record is None:
            raise OrderServiceError("Local order not found.", 404)

        # Step 3: Already PAID
        if record.status == "PAID":
            return {
                "local_order_id": record.local_order_id,
                "paypal_order_id": record.paypal_order_id,
                "capture_id": record.paypal_capture_id,
                "status": "PAID",
                "amount": _format_amount_cents(record.amount_cents),
                "currency": record.currency,
            }

        # Steps 4-18: Orquestation based on local state
        # Invalid states: PENDING, FAILED, CANCELLED cannot capture (state conflict)
        if record.status == "PENDING":
            raise OrderServiceError("Order cannot be captured in PENDING state.", 409)
        if record.status == "FAILED":
            raise OrderServiceError("Order cannot be captured in FAILED state.", 409)
        if record.status == "CANCELLED":
            raise OrderServiceError("Order cannot be captured in CANCELLED state.", 409)

        # Valid states: PAYPAL_CREATED or CAPTURING
        # Both require paypal_order_id
        if record.paypal_order_id is None:
            raise OrderServiceError("Order has no PayPal order ID attached.", 400)

        # Step 4: Precheck with show_order
        try:
            paypal_order = self._paypal_client.show_order(record.paypal_order_id)
        except PayPalAmbiguousResultError as error:
            raise OrderServiceError("PayPal order lookup outcome requires recovery.", 502) from error
        except PayPalClientError as error:
            raise OrderServiceError("PayPal order lookup failed.", 502) from error

        paypal_order_id = paypal_order["order_id"]
        order_status = paypal_order["order_status"]
        capture_id = paypal_order.get("capture_id")
        capture_status = paypal_order.get("capture_status")
        capture_amount = paypal_order.get("amount")
        capture_currency = paypal_order.get("currency")

        # Handle state based on local state and PayPal response
        if record.status == "PAYPAL_CREATED":
            # Steps 5-6: PAYPAL_CREATED precheck
            if order_status == "CREATED":
                # Step 5: PAYPAL CREATED / NO APROBADA
                raise OrderServiceError("Order not yet approved by payer.", 409)

            # Step 16: PAYPAL_CREATED + SHOW COMPLETED
            # Local has no capture_request_id, but PayPal shows COMPLETED with capture
            if order_status == "COMPLETED" and capture_status == "COMPLETED":
                # We cannot mark PAID without a capture_request_id
                # This is a reconciliation case that requires administrative handling
                raise OrderServiceError(
                    "Reconciliation required: remote capture exists but local has no request ID.",
                    409
                )

            if order_status == "APPROVED":
                # Step 6-7: PAYPAL APPROVED
                # Generate capture_request_id and persist CAPTURING before calling PayPal
                new_capture_request_id = str(uuid.uuid4())
                try:
                    capturing_record = self._store.begin_capture(
                        record.local_order_id, new_capture_request_id
                    )
                except OrderStoreError as error:
                    raise OrderServiceError("Cannot begin capture.", 500) from error

                # Use the EXACT request ID returned/persisted by begin_capture
                persisted_request_id = capturing_record.capture_request_id
                assert persisted_request_id is not None

                # Step 7: CAPTURE NORMAL
                try:
                    capture_result = self._paypal_client.capture_order(
                        record.paypal_order_id, persisted_request_id
                    )
                except PayPalAmbiguousResultError as error:
                    # Step 13: RESULTADO AMBIGUO
                    raise OrderServiceError(
                        "Capture outcome is uncertain; order remains CAPTURING.", 502
                    ) from error
                except PayPalClientError as error:
                    # Step 14: FALLO DETERMINISTA DE CAPTURE
                    # For deterministic failures where we know capture did NOT occur
                    try:
                        self._store.reset_capture_attempt(
                            record.local_order_id, persisted_request_id
                        )
                    except OrderStoreError:
                        pass  # Best effort; order may remain CAPTURING
                    raise OrderServiceError("Capture failed deterministically.", 502) from error

                # Step 8-9: VERIFICACION ESTRICTA y MARK PAID
                return self._finalize_paid(
                    capture_result, capturing_record, persisted_request_id
                )

        # record.status == "CAPTURING"
        # Step 10-12: CAPTURING reconciliation
        if record.capture_request_id is None:
            raise OrderServiceError("CAPTURING order has no capture request ID.", 500)

        if order_status == "COMPLETED" and capture_status == "COMPLETED":
            # Step 11: CAPTURING + PAYPAL COMPLETED
            # Verify all fields match
            if (paypal_order_id != record.paypal_order_id or
                capture_id is None or not capture_id or
                capture_amount is None or capture_currency is None or
                capture_amount != _format_amount_cents(record.amount_cents) or
                capture_currency != record.currency):
                # Step 12 verification: mismatches
                raise OrderServiceError(
                    "PayPal capture data does not match local order.", 400
                )
            # Step 9: MARK PAID
            try:
                paid_record = self._store.mark_paid(
                    record.local_order_id,
                    record.paypal_order_id,
                    capture_id,
                    record.amount_cents,
                    record.currency,
                    record.capture_request_id,
                )
            except OrderStoreError as error:
                raise OrderServiceError("Marking paid failed.", 500) from error
            return {
                "local_order_id": paid_record.local_order_id,
                "paypal_order_id": paid_record.paypal_order_id,
                "capture_id": paid_record.paypal_capture_id,
                "status": "PAID",
                "amount": _format_amount_cents(paid_record.amount_cents),
                "currency": paid_record.currency,
            }

        if order_status == "APPROVED":
            # Step 12: CAPTURING + PAYPAL APPROVED
            # Reuse the EXACT capture_request_id persisted
            persisted_request_id = record.capture_request_id
            assert persisted_request_id is not None

            try:
                capture_result = self._paypal_client.capture_order(
                    record.paypal_order_id, persisted_request_id
                )
            except PayPalAmbiguousResultError as error:
                raise OrderServiceError(
                    "Capture outcome is uncertain; order remains CAPTURING.", 502
                ) from error
            except PayPalClientError as error:
                try:
                    self._store.reset_capture_attempt(
                        record.local_order_id, persisted_request_id
                    )
                except OrderStoreError:
                    pass
                raise OrderServiceError("Capture failed deterministically.", 502) from error

            return self._finalize_paid(
                capture_result, record, persisted_request_id
            )

        # Other PayPal statuses
        raise OrderServiceError("Unexpected PayPal order status for capture.", 400)

    def _finalize_paid(
        self,
        capture_result: dict[str, Any],
        record: Any,
        capture_request_id: str,
    ) -> dict[str, str]:
        """Verify capture result matches local order and mark PAID."""
        # Step 8: VERIFICACION ESTRICTA
        paypal_order_id = capture_result.get("order_id")
        order_status = capture_result.get("order_status")
        capture_id = capture_result.get("capture_id")
        capture_status = capture_result.get("capture_status")
        capture_amount = capture_result.get("amount")
        capture_currency = capture_result.get("currency")

        if (paypal_order_id != record.paypal_order_id or
            order_status != "COMPLETED" or
            capture_status != "COMPLETED" or
            capture_id is None or not capture_id or
            capture_amount is None or capture_currency is None or
            capture_amount != _format_amount_cents(record.amount_cents) or
            capture_currency != record.currency):
            raise OrderServiceError(
                "Capture verification failed: data mismatch.", 400
            )

        # Step 9: MARK PAID
        try:
            paid_record = self._store.mark_paid(
                record.local_order_id,
                record.paypal_order_id,
                capture_id,
                record.amount_cents,
                record.currency,
                capture_request_id,
            )
        except OrderStoreError as error:
            raise OrderServiceError("Marking paid failed.", 500) from error

        return {
            "local_order_id": paid_record.local_order_id,
            "paypal_order_id": paid_record.paypal_order_id,
            "capture_id": paid_record.paypal_capture_id,
            "status": "PAID",
            "amount": _format_amount_cents(paid_record.amount_cents),
            "currency": paid_record.currency,
        }
