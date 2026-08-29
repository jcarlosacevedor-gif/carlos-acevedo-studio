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
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json["status"], "not_configured")

    def test_static_serving_cannot_escape_the_public_root(self):
        for path in ("/../README.md", "/%2e%2e/README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
