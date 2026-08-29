"""Tests for _DeferredPayPalClient to ensure it delegates all required methods."""

import unittest
from unittest.mock import MagicMock, patch

from backend.app import _DeferredPayPalClient
from backend.paypal_client import PayPalClient


class TestDeferredPayPalClient(unittest.TestCase):
    """Test that _DeferredPayPalClient correctly delegates to PayPalClient.from_environment()."""

    def test_create_order_delegates(self):
        """Verify create_order delegates to PayPalClient.from_environment()."""
        with patch.object(PayPalClient, 'from_environment') as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client
            mock_client.create_order.return_value = {"order_id": "TEST123", "status": "CREATED", "approval_url": "https://test"}

            deferred = _DeferredPayPalClient()
            result = deferred.create_order(19900, "USD", "req-123", return_url="https://return", cancel_url="https://cancel")

            # Verify delegation
            mock_from_env.assert_called_once()
            mock_client.create_order.assert_called_once_with(19900, "USD", "req-123", return_url="https://return", cancel_url="https://cancel")
            self.assertEqual(result, {"order_id": "TEST123", "status": "CREATED", "approval_url": "https://test"})

    def test_show_order_delegates(self):
        """Verify show_order delegates to PayPalClient.from_environment()."""
        with patch.object(PayPalClient, 'from_environment') as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client
            mock_client.show_order.return_value = {
                "order_id": "TEST123",
                "order_status": "APPROVED",
            }

            deferred = _DeferredPayPalClient()
            result = deferred.show_order("TEST123")

            mock_from_env.assert_called_once()
            mock_client.show_order.assert_called_once_with("TEST123")
            self.assertEqual(result["order_id"], "TEST123")
            self.assertEqual(result["order_status"], "APPROVED")

    def test_capture_order_delegates(self):
        """Verify capture_order delegates to PayPalClient.from_environment()."""
        with patch.object(PayPalClient, 'from_environment') as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client
            mock_client.capture_order.return_value = {
                "order_id": "TEST123",
                "order_status": "COMPLETED",
                "capture_id": "CAPTURE456",
                "capture_status": "COMPLETED",
                "amount": "199.00",
                "currency": "USD",
            }

            deferred = _DeferredPayPalClient()
            result = deferred.capture_order("TEST123", "req-456")

            mock_from_env.assert_called_once()
            mock_client.capture_order.assert_called_once_with("TEST123", "req-456")
            self.assertEqual(result["order_id"], "TEST123")
            self.assertEqual(result["capture_id"], "CAPTURE456")

    def test_lazy_initialization_no_early_call(self):
        """Verify PayPalClient.from_environment is not called until a method is invoked."""
        with patch.object(PayPalClient, 'from_environment') as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client

            # Just creating the deferred client should not call from_environment
            deferred = _DeferredPayPalClient()
            mock_from_env.assert_not_called()

            # Now calling a method should trigger it
            deferred.create_order(100, "USD", "req-1")
            mock_from_env.assert_called_once()

    def test_arguments_passed_through_intact(self):
        """Verify arguments are passed through unchanged to the real client."""
        with patch.object(PayPalClient, 'from_environment') as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client
            mock_client.show_order.return_value = {"order_id": "ID1", "order_status": "APPROVED"}

            deferred = _DeferredPayPalClient()
            # Pass various argument types
            result = deferred.show_order("order-id-123")

            mock_client.show_order.assert_called_once_with("order-id-123")

    def test_uses_same_configuration(self):
        """Verify all methods use the same PayPalClient instance."""
        with patch.object(PayPalClient, 'from_environment') as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client
            mock_client.create_order.return_value = {"order_id": "O1", "status": "CREATED", "approval_url": "url"}
            mock_client.show_order.return_value = {"order_id": "O1", "order_status": "APPROVED"}
            mock_client.capture_order.return_value = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": "C1", "capture_status": "COMPLETED", "amount": "10.00", "currency": "USD"}

            deferred = _DeferredPayPalClient()

            # Call all three methods
            deferred.create_order(100, "USD", "req-1")
            deferred.show_order("O1")
            deferred.capture_order("O1", "req-2")

            # from_environment should be called 3 times (once per method)
            self.assertEqual(mock_from_env.call_count, 3)
            # But all calls return the same mock_client instance
            self.assertIs(mock_client, mock_from_env.return_value)


class TestFlaskAppIntegration(unittest.TestCase):
    """Integration test using real create_app with mocked PayPalClient."""

    def test_flask_capture_endpoint_uses_deferred_client(self):
        """Test that Flask Capture endpoint traverses through _DeferredPayPalClient correctly."""
        from backend.app import create_app
        from backend.order_store import OrderStore
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = OrderStore(db_path)

            # Create a mock PayPalClient
            mock_paypal = MagicMock()
            mock_paypal.create_order.return_value = {
                "order_id": "MOCKORDER123",
                "status": "CREATED",
                "approval_url": "https://mock-approval",
            }
            mock_paypal.show_order.return_value = {
                "order_id": "MOCKORDER123",
                "order_status": "APPROVED",
            }
            mock_paypal.capture_order.return_value = {
                "order_id": "MOCKORDER123",
                "order_status": "COMPLETED",
                "capture_id": "MOCKCAPTURE456",
                "capture_status": "COMPLETED",
                "amount": "199.00",
                "currency": "USD",
            }

            # Patch from_environment to return our mock
            with patch.object(PayPalClient, 'from_environment', return_value=mock_paypal):
                # Create app with the mock store and paypal_client
                app = create_app(order_service=None, database_path=db_path, paypal_client=None)

                with app.test_client() as client:
                    # Step 1: Create order
                    create_response = client.post(
                        '/api/paypal/orders',
                        json={"product": "custom-song", "solo": "none", "brief": {}},
                        content_type='application/json'
                    )
                    self.assertEqual(create_response.status_code, 201)
                    create_data = create_response.get_json()
                    local_order_id = create_data["local_order_id"]

                    # Verify create_order was called through deferred client
                    self.assertEqual(mock_paypal.create_order.call_count, 1)

                    # Step 2: Capture order
                    capture_response = client.post(
                        f'/api/paypal/orders/{local_order_id}/capture',
                        json={},
                        content_type='application/json'
                    )
                    self.assertEqual(capture_response.status_code, 200)
                    capture_data = capture_response.get_json()

                    # Verify the response
                    self.assertEqual(capture_data["status"], "PAID")
                    self.assertEqual(capture_data["local_order_id"], local_order_id)
                    self.assertEqual(capture_data["capture_id"], "MOCKCAPTURE456")

                    # Verify show_order and capture_order were called through deferred client
                    mock_paypal.show_order.assert_called_once()
                    mock_paypal.capture_order.assert_called_once()

                    # Verify idempotency - second capture
                    capture_response2 = client.post(
                        f'/api/paypal/orders/{local_order_id}/capture',
                        json={},
                        content_type='application/json'
                    )
                    self.assertEqual(capture_response2.status_code, 200)
                    capture_data2 = capture_response2.get_json()

                    # Should return same capture_id
                    self.assertEqual(capture_data2["capture_id"], "MOCKCAPTURE456")
                    self.assertEqual(capture_data2["status"], "PAID")

                    # capture_order should NOT be called again for idempotency
                    self.assertEqual(mock_paypal.capture_order.call_count, 1)
