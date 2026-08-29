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
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,36}$")


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

    @app.post("/api/paypal/orders/<order_id>/capture")
    def capture_order_placeholder(order_id: str):
        if not ORDER_ID_PATTERN.fullmatch(order_id):
            raise APIError("Invalid PayPal order ID.", 400)
        return jsonify({
            "status": "not_implemented",
            "message": "PayPal capture is not connected yet.",
        }), 501

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
