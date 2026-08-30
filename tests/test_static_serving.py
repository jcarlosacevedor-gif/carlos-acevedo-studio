import unittest

from backend.app import create_app


class StaticServingTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_existing_public_files_are_served_from_project_root(self):
        resources = {
            "/": "text/html",
            "/promo-lab.html": "text/html",
            "/assets/styles/main.css": "text/css",
            "/assets/scripts/custom-song-order.js": "javascript",
        }
        for path, content_type in resources.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.content_type)

    def test_api_routes_are_not_intercepted_by_static_files(self):
        response = self.client.post(
            "/api/paypal/orders",
            json={"product": "custom-song", "solo": "none"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json)

    def test_static_serving_cannot_escape_the_public_root(self):
        for path in ("/../README.md", "/%2e%2e/README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_paypal_return_page_is_served(self):
        response = self.client.get("/paypal/return?token=TEST&PayerID=TEST")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.content_type)

    def test_paypal_cancel_page_is_served(self):
        response = self.client.get("/paypal/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.content_type)

    def test_paypal_return_page_references_root_relative_assets(self):
        response = self.client.get("/paypal/return")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('href="/assets/styles/main.css"', html)
        self.assertIn('src="/assets/scripts/paypal-return.js"', html)
        self.assertIn('src="/assets/images/Logotipo_Zoom.png"', html)
        self.assertIn('href="/promo-lab.html"', html)

    def test_paypal_cancel_page_references_root_relative_assets(self):
        response = self.client.get("/paypal/cancel")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('href="/assets/styles/main.css"', html)
        self.assertIn('src="/assets/images/Logotipo_Zoom.png"', html)
        self.assertIn('href="/promo-lab.html"', html)
        self.assertNotIn("paypal-return.js", html)

    def test_paypal_return_page_does_not_reference_relative_assets(self):
        response = self.client.get("/paypal/return")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertNotIn('href="assets/styles/main.css"', html)
        self.assertNotIn('src="assets/scripts/paypal-return.js"', html)
        self.assertNotIn('src="assets/images/Logotipo_Zoom.png"', html)
        self.assertNotIn('href="promo-lab.html"', html)

    def test_assets_are_servable_from_root(self):
        assets = {
            "/assets/styles/main.css": "text/css",
            "/assets/scripts/paypal-return.js": "javascript",
            "/assets/images/Logotipo_Zoom.png": "image/png",
        }
        for path, content_type in assets.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.content_type)
