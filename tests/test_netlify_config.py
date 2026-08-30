from pathlib import Path
import tomllib
import unittest
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).parent.parent
NETLIFY_CONFIG = PROJECT_ROOT / "netlify.toml"
RENDER_API_ORIGIN = "https://carlos-acevedo-studio-backend-preview.onrender.com"


class NetlifyProxyConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.raw_config = NETLIFY_CONFIG.read_text(encoding="utf-8")
        self.config = tomllib.loads(self.raw_config)
        self.redirects = self.config.get("redirects", [])

    def test_config_exists_and_has_exactly_one_api_proxy_rule(self):
        self.assertTrue(NETLIFY_CONFIG.is_file())
        self.assertEqual(len(self.redirects), 1)
        self.assertEqual(self.redirects[0]["from"], "/api/*")
        self.assertEqual(
            self.redirects[0]["to"],
            f"{RENDER_API_ORIGIN}/api/:splat",
        )
        self.assertEqual(self.redirects[0]["status"], 200)
        self.assertIs(self.redirects[0]["force"], True)

    def test_proxy_does_not_cover_static_or_paypal_return_routes(self):
        proxied_paths = [rule["from"] for rule in self.redirects]
        self.assertEqual(proxied_paths, ["/api/*"])
        for path in ("/paypal/return", "/paypal/cancel", "/assets/*", "/*", "/health"):
            self.assertNotIn(path, proxied_paths)

    def test_proxy_target_is_https_and_has_no_embedded_credentials_or_query(self):
        target = self.redirects[0]["to"]
        parsed = urlsplit(target)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "carlos-acevedo-studio-backend-preview.onrender.com")
        self.assertIsNone(parsed.username)
        self.assertIsNone(parsed.password)
        self.assertEqual(parsed.query, "")
        self.assertEqual(parsed.fragment, "")

    def test_config_contains_no_secret_or_token_markers(self):
        lowered = self.raw_config.lower()
        for marker in ("client_secret", "client_id", "authorization", "password", "token="):
            self.assertNotIn(marker, lowered)


class SameOriginPaymentFrontendTests(unittest.TestCase):
    def test_frontend_keeps_relative_api_paths_and_has_no_render_hostname(self):
        custom_order = (PROJECT_ROOT / "assets" / "scripts" / "custom-song-order.js").read_text(encoding="utf-8")
        paypal_return = (PROJECT_ROOT / "assets" / "scripts" / "paypal-return.js").read_text(encoding="utf-8")

        self.assertIn('fetch("/api/paypal/orders"', custom_order)
        self.assertIn("/api/paypal/orders/resolve?token=", paypal_return)
        self.assertIn("/api/paypal/orders/", paypal_return)
        self.assertNotIn("onrender.com", custom_order)
        self.assertNotIn("onrender.com", paypal_return)
        self.assertNotIn("API_BASE_URL", custom_order)
        self.assertNotIn("API_BASE_URL", paypal_return)
