"""Application service for the future web PayPal Create flow."""

from __future__ import annotations

import json
from typing import Any
import uuid

from .order_store import OrderStore, OrderStoreError
from .paypal_client import PayPalAmbiguousResultError, PayPalClientError
from .pricing import PricingError, calculate_custom_song_price


FORBIDDEN_FIELDS = frozenset({"amount", "amount_cents", "price", "total", "currency", "quantity", "paypal_order_id", "capture_id", "status", "create_request_id", "capture_request_id"})


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
