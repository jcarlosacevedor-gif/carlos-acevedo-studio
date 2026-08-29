"""Flask placeholder API for the future PayPal integration."""

from pathlib import Path
import re

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException

from .config import ConfigurationError, PayPalConfig
from .pricing import PricingError, calculate_custom_song_price


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,36}$")


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


def create_app() -> Flask:
    """Create the local same-origin server without contacting PayPal."""
    app = Flask(__name__, static_folder=str(PROJECT_ROOT), static_url_path="")

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
        payload = _json_object()
        try:
            pricing = calculate_custom_song_price(payload)
            PayPalConfig.from_environment(require_credentials=False)
        except PricingError as error:
            raise APIError(str(error), 422) from error
        except ConfigurationError as error:
            raise APIError(str(error), 500) from error

        return jsonify({
            "status": "not_configured",
            "message": "PayPal order creation is not connected yet.",
            "pricing": pricing,
        }), 501

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
