import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend.config import (
    ConfigurationError,
    DEFAULT_ORDER_DB_PATH,
    get_order_db_path,
)


class FakePayPal:
    def create_order(self, amount_cents, currency, request_id, **kwargs):
        return {
            "order_id": "HOSTINGTESTORDER",
            "status": "CREATED",
            "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=HOSTINGTESTORDER",
        }


class OrderDatabasePathTests(unittest.TestCase):
    def test_default_path_preserves_historic_local_location(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_order_db_path(), DEFAULT_ORDER_DB_PATH)

    def test_configured_path_is_used_and_parent_is_created(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "persistent" / "orders.sqlite3"
            with patch.dict(os.environ, {"ORDER_DB_PATH": str(database_path)}, clear=True):
                self.assertEqual(get_order_db_path(), database_path)
                app = create_app(paypal_client=FakePayPal())
                response = app.test_client().post(
                    "/api/paypal/orders",
                    json={"product": "custom-song", "solo": "none", "brief": {}},
                )

            self.assertEqual(response.status_code, 201)
            self.assertTrue(database_path.is_file())

    def test_empty_configured_path_is_rejected(self):
        with patch.dict(os.environ, {"ORDER_DB_PATH": "   "}, clear=True):
            with self.assertRaises(ConfigurationError):
                get_order_db_path()


class HealthEndpointTests(unittest.TestCase):
    def test_health_returns_stable_json_without_touching_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "not-created" / "orders.sqlite3"
            with patch("backend.app.PayPalClient.from_environment") as paypal_from_environment:
                app = create_app(database_path=database_path)
                response = app.test_client().get("/health")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"status": "ok"})
            self.assertFalse(database_path.parent.exists())
            paypal_from_environment.assert_not_called()
