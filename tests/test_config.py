import os
import unittest
from unittest.mock import patch

from backend.config import ConfigurationError, PayPalConfig, get_ops_audit_token, get_public_site_base_url


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


class OpsAuditTokenTests(unittest.TestCase):
    def test_sandbox_allows_missing_token(self):
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "sandbox"}, clear=True):
            self.assertIsNone(get_ops_audit_token())

    def test_live_requires_token_without_disclosing_a_value(self):
        sentinel = "PRIVATE_OPS_TOKEN_SENTINEL"
        with patch.dict(
            os.environ,
            {"PAYPAL_ENVIRONMENT": "live", "OPS_AUDIT_TOKEN": sentinel},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError) as raised:
                get_ops_audit_token()
        self.assertNotIn(sentinel, str(raised.exception))

        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "live"}, clear=True):
            with self.assertRaises(ConfigurationError):
                get_ops_audit_token()

    def test_valid_high_entropy_token_is_returned(self):
        token = "ops_" + "x" * 40
        with patch.dict(
            os.environ,
            {"PAYPAL_ENVIRONMENT": "sandbox", "OPS_AUDIT_TOKEN": token},
            clear=True,
        ):
            self.assertEqual(get_ops_audit_token(), token)


class PublicSiteBaseUrlTests(unittest.TestCase):
    def test_default_localhost(self):
        with patch.dict(os.environ, {}, clear=True):
            result = get_public_site_base_url()
        self.assertEqual(result, "http://127.0.0.1:8000")

    def test_nginx_style_url_with_port(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "http://nginx:8080"}, clear=True):
            result = get_public_site_base_url()
        self.assertEqual(result, "http://nginx:8080")

    def test_https_production_url(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "https://example.com"}, clear=True):
            result = get_public_site_base_url()
        self.assertEqual(result, "https://example.com")

    def test_https_with_path(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "https://example.com/myapp"}, clear=True):
            result = get_public_site_base_url()
        self.assertEqual(result, "https://example.com/myapp")

    def test_https_with_trailing_slash(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "https://example.com/"}, clear=True):
            result = get_public_site_base_url()
        self.assertEqual(result, "https://example.com")

    def test_http_localhost_with_path_and_trailing_slash(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "http://localhost:3000/app/"}, clear=True):
            result = get_public_site_base_url()
        self.assertEqual(result, "http://localhost:3000/app")

    def test_empty_value_rejected(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": ""}, clear=True):
            with self.assertRaises(ConfigurationError):
                get_public_site_base_url()

    def test_missing_scheme_rejected(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "example.com"}, clear=True):
            with self.assertRaises(ConfigurationError):
                get_public_site_base_url()

    def test_invalid_scheme_rejected(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_BASE_URL": "ftp://example.com"}, clear=True):
            with self.assertRaises(ConfigurationError):
                get_public_site_base_url()
