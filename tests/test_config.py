import os
import unittest
from unittest.mock import patch

from backend.config import ConfigurationError, PayPalConfig


class PayPalConfigTests(unittest.TestCase):
    def test_sandbox_and_live_urls(self):
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "sandbox"}, clear=True):
            self.assertEqual(PayPalConfig.from_environment().api_base_url, "https://api-m.sandbox.paypal.com")
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "live"}, clear=True):
            self.assertEqual(PayPalConfig.from_environment().api_base_url, "https://api-m.paypal.com")

    def test_invalid_environment_is_rejected(self):
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "staging"}, clear=True):
            with self.assertRaises(ConfigurationError):
                PayPalConfig.from_environment()

    def test_missing_credentials_is_safe(self):
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "sandbox"}, clear=True):
            with self.assertRaises(ConfigurationError) as raised:
                PayPalConfig.from_environment(require_credentials=True)
        self.assertNotIn("SECRET", str(raised.exception).upper())
