import os
import base64
import json
import unittest
from unittest.mock import Mock, patch

import jwt

from backend.app import create_app
from backend.config import ConfigurationError, get_netlify_proxy_auth_config


AUTH = {
    "PAYPAL_ENVIRONMENT": "sandbox",
    "NETLIFY_PROXY_SIGNING_SECRET": "sandbox-test-secret",
    "NETLIFY_PROXY_EXPECTED_SITE_ID": "sandbox-site",
    "NETLIFY_PROXY_EXPECTED_SITE_URL": "https://sandbox.example.test",
    "NETLIFY_PROXY_EXPECTED_DEPLOY_CONTEXT": "branch-deploy",
}


class NetlifyProxyAuthTests(unittest.TestCase):
    def token(self, **changes):
        claims = {"iss": "netlify", "exp": 4_000_000_000, "netlify_id": "sandbox-site", "site_url": "https://sandbox.example.test", "deploy_context": "branch-deploy"}
        claims.update(changes)
        return jwt.encode(claims, AUTH["NETLIFY_PROXY_SIGNING_SECRET"], algorithm="HS256")

    def test_sandbox_can_remain_unprotected_but_partial_and_live_are_rejected(self):
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "sandbox"}, clear=True):
            self.assertIsNone(get_netlify_proxy_auth_config())
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "sandbox", "NETLIFY_PROXY_SIGNING_SECRET": "x"}, clear=True):
            with self.assertRaises(ConfigurationError): get_netlify_proxy_auth_config()
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "live"}, clear=True):
            with self.assertRaises(ConfigurationError): get_netlify_proxy_auth_config()

    def test_valid_signature_allows_api_and_health_remains_public(self):
        service = Mock()
        service.resolve_paypal_order.return_value = {"local_order_id": "x"}
        with patch.dict(os.environ, AUTH, clear=True):
            client = create_app(order_service=service).test_client()
            self.assertEqual(client.get("/health").status_code, 200)
            response = client.get("/api/paypal/orders/resolve?token=PAYPALORDER123", headers={"x-nf-sign": self.token()})
        self.assertEqual(response.status_code, 200)
        service.resolve_paypal_order.assert_called_once()

    def test_invalid_signatures_are_generic_and_have_no_service_side_effect(self):
        cases = [None, self.token(iss="other"), self.token(netlify_id="other"), self.token(site_url="https://other"), self.token(deploy_context="production"), self.token(exp=1), jwt.encode({"iss": "netlify", "exp": 4_000_000_000}, "other-secret", algorithm="HS256")]
        for signature in cases:
            with self.subTest(signature=signature is None):
                service = Mock()
                with patch.dict(os.environ, AUTH, clear=True):
                    client = create_app(order_service=service).test_client()
                    headers = {} if signature is None else {"x-nf-sign": signature, "X-Forwarded-For": "forged", "Forwarded": "forged"}
                    response = client.get("/api/paypal/orders/resolve?token=PAYPALORDER123", headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json, {"error": "Proxy authorization required."})
                service.resolve_paypal_order.assert_not_called()
                self.assertNotIn("sandbox-site", response.get_data(as_text=True))

    def test_malformed_tampered_and_non_hs256_tokens_are_rejected(self):
        valid = self.token()
        header, payload, signature = valid.split(".")
        tampered = f"{header}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"
        none_header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
        none_payload = base64.urlsafe_b64encode(json.dumps({"iss": "netlify", "exp": 4_000_000_000, "netlify_id": "sandbox-site", "site_url": "https://sandbox.example.test", "deploy_context": "branch-deploy"}).encode()).decode().rstrip("=")
        cases = ["not-a-jwt", tampered, jwt.encode(json.loads(base64.urlsafe_b64decode(payload + "==")), AUTH["NETLIFY_PROXY_SIGNING_SECRET"], algorithm="HS384"), jwt.encode(json.loads(base64.urlsafe_b64decode(payload + "==")), AUTH["NETLIFY_PROXY_SIGNING_SECRET"], algorithm="HS512"), f"{none_header}.{none_payload}."]
        for signature in cases:
            with self.subTest(signature=signature[:8]):
                service = Mock()
                with patch.dict(os.environ, AUTH, clear=True):
                    response = create_app(order_service=service).test_client().get("/api/paypal/orders/resolve?token=PAYPALORDER123", headers={"x-nf-sign": signature})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json, {"error": "Proxy authorization required."})
                service.resolve_paypal_order.assert_not_called()

    def test_blank_and_live_partial_proxy_configuration_are_rejected_safely(self):
        blank = {"PAYPAL_ENVIRONMENT": "sandbox", "NETLIFY_PROXY_SIGNING_SECRET": " ", "NETLIFY_PROXY_EXPECTED_SITE_ID": "", "NETLIFY_PROXY_EXPECTED_SITE_URL": " ", "NETLIFY_PROXY_EXPECTED_DEPLOY_CONTEXT": ""}
        with patch.dict(os.environ, blank, clear=True):
            self.assertIsNone(get_netlify_proxy_auth_config())
        partial = dict(blank, NETLIFY_PROXY_SIGNING_SECRET="real-partial")
        with patch.dict(os.environ, partial, clear=True):
            with self.assertRaises(ConfigurationError): get_netlify_proxy_auth_config()
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "live", "NETLIFY_PROXY_SIGNING_SECRET": "partial"}, clear=True):
            with self.assertRaises(ConfigurationError): create_app()

    def test_failure_logs_are_sanitized_categories_before_service(self):
        cases = [
            ({}, "reason=missing_header", []),
            ({"x-nf-sign": "malformed.jwt.value"}, "reason=malformed_or_invalid_token", []),
            ({"x-nf-sign": self.token(site_url="https://secret-claim.example")}, "reason=invalid_claim", ["site_url"]),
            ({"x-nf-sign": self.token(netlify_id="secret-site", deploy_context="secret-context")}, "reason=invalid_claim", ["netlify_id", "deploy_context"]),
        ]
        for headers, expected_reason, claim_names in cases:
            with self.subTest(reason=expected_reason):
                service = Mock()
                with patch.dict(os.environ, AUTH, clear=True), self.assertLogs("backend.app", level="WARNING") as logs:
                    response = create_app(order_service=service).test_client().get("/api/paypal/orders/resolve?token=PAYPALORDER123", headers={**headers, "Authorization": "secret-auth"})
                output = "\n".join(logs.output)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json, {"error": "Proxy authorization required."})
                self.assertIn(expected_reason, output)
                for name in claim_names: self.assertIn(name, output)
                for secret in ("PAYPALORDER123", "secret-auth", "sandbox-test-secret", "https://secret-claim.example", "secret-site", "secret-context"):
                    self.assertNotIn(secret, output)
                service.resolve_paypal_order.assert_not_called()

    def test_pyjwt_failure_reasons_are_sanitized_and_specific(self):
        claims = {"iss": "netlify", "exp": 4_000_000_000, "netlify_id": "sandbox-site", "site_url": "https://sandbox.example.test", "deploy_context": "branch-deploy"}
        cases = [
            (jwt.encode(claims, "other-secret", algorithm="HS256"), "signature_mismatch"),
            (self.token(exp=1), "expired_signature"),
            (self.token(iss="other"), "invalid_issuer"),
            (jwt.encode(claims, AUTH["NETLIFY_PROXY_SIGNING_SECRET"], algorithm="HS384"), "invalid_algorithm"),
            ("not.a.jwt", "malformed_or_invalid_token"),
        ]
        for signature, reason in cases:
            with self.subTest(reason=reason):
                service = Mock()
                with patch.dict(os.environ, AUTH, clear=True), self.assertLogs("backend.app", level="WARNING") as logs:
                    response = create_app(order_service=service).test_client().get("/api/paypal/orders/resolve?token=PAYPALORDER123", headers={"x-nf-sign": signature, "Authorization": "secret-auth"})
                output = "\n".join(logs.output)
                self.assertIn(f"reason={reason}", output)
                self.assertEqual(response.json, {"error": "Proxy authorization required."})
                self.assertNotIn("PAYPALORDER123", output)
                self.assertNotIn("secret-auth", output)
                self.assertNotIn("sandbox-test-secret", output)
                service.resolve_paypal_order.assert_not_called()
