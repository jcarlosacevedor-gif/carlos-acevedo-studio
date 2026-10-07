"""Flask placeholder API for the future PayPal integration."""

from datetime import datetime, timezone
import hmac
from pathlib import Path
import re
import sqlite3

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException, MethodNotAllowed

from .config import ConfigurationError, PayPalConfig, get_netlify_proxy_auth_config, get_ops_audit_token, get_order_db_path, get_public_site_base_url, get_stale_order_thresholds
import jwt
from .observability import emit_event
from .pricing import PricingError, calculate_custom_song_price
from .order_store import OrderStore, OrderStoreError
from .order_service import OrderService, OrderServiceError
from .paypal_client import PayPalClient, PayPalClientError, validate_order_id
from .stale_audit import audit_stale, emit_stale_events


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

    def show_order(self, *args, **kwargs):
        return PayPalClient.from_environment().show_order(*args, **kwargs)

    def capture_order(self, *args, **kwargs):
        return PayPalClient.from_environment().capture_order(*args, **kwargs)


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
    try:
        proxy_auth = get_netlify_proxy_auth_config()
        stale_thresholds = get_stale_order_thresholds()
        ops_audit_token = get_ops_audit_token()
    except ConfigurationError:
        try:
            emit_event(
                "operational_error",
                operation="configuration",
                outcome="failed",
                reason_code="configuration_error",
                source="api",
            )
        except Exception:
            pass
        raise

    def emit_operational_error(reason_code: str) -> None:
        try:
            emit_event(
                "operational_error",
                operation="stale_scan",
                outcome="failed",
                reason_code=reason_code,
                source="api",
            )
        except Exception:
            pass

    def audit_response(status: str, status_code: int):
        return jsonify({"status": status}), status_code

    def service_for_request():
        if order_service is not None:
            return order_service
        path = Path(database_path) if database_path is not None else get_order_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        base_url = get_public_site_base_url()
        return_url = f"{base_url}/paypal/return"
        cancel_url = f"{base_url}/paypal/cancel"
        return OrderService(OrderStore(path), paypal_client or _DeferredPayPalClient(), return_url, cancel_url)

    @app.errorhandler(APIError)
    def handle_api_error(error: APIError):
        return jsonify({"error": error.message}), error.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    @app.before_request
    def require_netlify_proxy_signature():
        if proxy_auth is None or not request.path.startswith("/api/paypal/"):
            return None
        signature = request.headers.get("x-nf-sign")
        if not signature:
            return jsonify({"error": "Proxy authorization required."}), 403
        try:
            claims = jwt.decode(signature, proxy_auth.secret, algorithms=["HS256"], issuer="netlify", options={"require": ["exp", "iss", "deploy_context", "netlify_id", "site_url"]})
        except jwt.PyJWTError:
            return jsonify({"error": "Proxy authorization required."}), 403
        if (
            claims.get("deploy_context") != proxy_auth.deploy_context
            or claims.get("netlify_id") != proxy_auth.site_id
            or claims.get("site_url") != proxy_auth.site_url
        ):
            return jsonify({"error": "Proxy authorization required."}), 403
        return None

    @app.after_request
    def disable_internal_audit_caching(response):
        if request.path == "/internal/audit-stale":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def home_page():
        return send_from_directory(PROJECT_ROOT, "index.html")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.post("/internal/audit-stale", provide_automatic_options=False)
    def internal_audit_stale():
        supplied_authorization = request.headers.get("Authorization", "")
        expected_authorization = (
            f"Bearer {ops_audit_token}"
            if ops_audit_token is not None
            else "Bearer 00000000000000000000000000000000"
        )
        authorized = hmac.compare_digest(supplied_authorization, expected_authorization)
        if ops_audit_token is None or not authorized:
            return audit_response("error", 403)

        if request.query_string or request.content_length not in (None, 0):
            return audit_response("error", 400)
        if request.get_data(cache=False):
            return audit_response("error", 400)

        try:
            path = Path(database_path) if database_path is not None else get_order_db_path()
            if not path.is_file():
                raise OrderStoreError("Order database does not exist.")
            store = OrderStore(path, initialize=False, read_only=True)
            report = audit_stale(store, stale_thresholds, datetime.now(timezone.utc))
        except ConfigurationError:
            emit_operational_error("configuration_error")
            return audit_response("error", 500)
        except (OrderStoreError, sqlite3.Error, OSError, TypeError, ValueError):
            emit_operational_error("sqlite_error")
            return audit_response("error", 500)
        except Exception:
            emit_operational_error("sqlite_error")
            return audit_response("error", 500)

        try:
            emit_stale_events(report)
        except Exception:
            pass
        responses = {
            0: ("ok", 200),
            2: ("warning", 200),
            3: ("critical", 409),
        }
        response = responses.get(report.highest_exit)
        if response is None:
            emit_operational_error("sqlite_error")
            return audit_response("error", 500)
        return audit_response(*response)

    @app.route(
        "/internal/audit-stale",
        methods=["GET", "OPTIONS"],
        provide_automatic_options=False,
    )
    def reject_get_internal_audit():
        raise MethodNotAllowed()

    @app.get("/paypal/return")
    def paypal_return_page():
        return send_from_directory(PROJECT_ROOT, "paypal-return.html")

    @app.get("/paypal/cancel")
    def paypal_cancel_page():
        return send_from_directory(PROJECT_ROOT, "paypal-cancel.html")

    @app.get("/api/paypal/orders/resolve")
    def reject_get_paypal_order_resolution():
        raise APIError("Resolve endpoint requires POST.", 405)

    @app.post("/api/paypal/orders/resolve")
    def resolve_paypal_order():
        payload = _json_object()
        if set(payload) != {"token"}:
            raise APIError("Resolve endpoint accepts only a token.", 400)
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise APIError("Token is required.", 400)
        try:
            validate_order_id(token)
        except (ValueError, PayPalClientError):
            raise APIError("Invalid token format.", 400)
        service = service_for_request()
        try:
            result = service.resolve_paypal_order(token)
        except OrderServiceError as error:
            raise APIError(str(error), error.status_code) from error
        return jsonify(result), 200

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
