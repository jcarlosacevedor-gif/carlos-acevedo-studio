"""Flask placeholder API for the future PayPal integration."""

from pathlib import Path
import re

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException

from .config import ConfigurationError, PayPalConfig
from .pricing import PricingError, calculate_custom_song_price
from .order_store import OrderStore
from .order_service import OrderService, OrderServiceError
from .paypal_client import PayPalClient


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# For Capture endpoint: reject any attempt to supply business fields from the browser.
CAPTURE_FORBIDDEN_FIELDS = frozenset({
    "amount", "amount_cents", "price", "total", "currency", "quantity",
    "paypal_order_id", "capture_id", "status",
    "create_request_id", "capture_request_id",
})

# Local order IDs are UUID strings (36 chars with hyphens)
LOCAL_ORDER_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class _DeferredPayPalClient:
    def create_order(self, *args, **kwargs):
        return PayPalClient.from_environment().create_order(*args, **kwargs)


def _json_object() -> dict[str, object]:
    if not request.is_json:
        raise APIError("A JSON object is required.", 400)
    try:
        payload = request.get_json()
    except BadRequest as error:
        raise APIError("Invalid JSON body.", 400) from error
    if not isinstance(payload, dict):
        raise APIError("A JSON object is required.", 400)
    return payload


class APIError(Exception):
    def __init__(self, message: str, status_code: int):
        self.message = message
        self.status_code = status_code


def create_app(order_service=None, database_path=None, paypal_client=None) -> Flask:
    """Create the local same-origin server without contacting PayPal."""
    app = Flask(__name__, static_folder=str(PROJECT_ROOT), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    def service_for_request():
        if order_service is not None:
            return order_service
        path = Path(database_path or PROJECT_ROOT / "instance" / "orders.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        return OrderService(OrderStore(path), paypal_client or _DeferredPayPalClient(), "https://example.com/paypal/return", "https://example.com/paypal/cancel")

    @app.errorhandler(APIError)
    def handle_api_error(error: APIError):
        return jsonify({"error": error.message}), error.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    @app.get("/")
    def home_page():
        return send_from_directory(PROJECT_ROOT, "index.html")

    @app.post("/api/paypal/orders")
    def create_order_placeholder():
        try:
            return jsonify(service_for_request().create_order(_json_object())), 201
        except OrderServiceError as error:
            raise APIError(str(error), error.status_code) from error

    @app.post("/api/paypal/orders/<local_order_id>/capture")
    def capture_order(local_order_id: str):
        # Validate local_order_id format (UUID)
        if not LOCAL_ORDER_ID_PATTERN.fullmatch(local_order_id):
            raise APIError("Invalid local order ID.", 400)

        # Validate body: empty, empty JSON object {}, or no body at all
        if request.data and request.content_length:
            if not request.is_json:
                raise APIError("Capture endpoint expects empty body or JSON.", 400)
            try:
                payload = request.get_json()
            except BadRequest as error:
                raise APIError("Invalid JSON body.", 400) from error
            if not isinstance(payload, dict):
                raise APIError("A JSON object is required.", 400)

            # Capture receives zero business fields - reject any non-empty JSON object
            if payload:
                raise APIError("Capture endpoint does not accept request body data.", 400)

        try:
            result = service_for_request().capture_order(local_order_id)
        except OrderServiceError as error:
            raise APIError(str(error), error.status_code) from error

        return jsonify(result), 200

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
