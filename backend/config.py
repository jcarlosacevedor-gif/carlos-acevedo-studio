"""Environment-based PayPal configuration without exposing secrets."""

from dataclasses import dataclass
import os


class ConfigurationError(ValueError):
    """Raised when a required, safe configuration value is unavailable."""


PAYPAL_API_BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
PAYPAL_APPROVAL_HOSTS = {
    "sandbox": frozenset({"www.sandbox.paypal.com"}),
    "live": frozenset({"www.paypal.com"}),
}

# Protocol constants used across PayPal client and order persistence layers
REQUEST_ID_MAX_LENGTH = 108


@dataclass(frozen=True)
class PayPalConfig:
    environment: str
    client_id: str | None
    client_secret: str | None

    @property
    def api_base_url(self) -> str:
        return PAYPAL_API_BASE_URLS[self.environment]

    @property
    def approval_hosts(self) -> frozenset[str]:
        return PAYPAL_APPROVAL_HOSTS[self.environment]

    @classmethod
    def from_environment(cls, require_credentials: bool = False) -> "PayPalConfig":
        environment = os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").strip().lower()
        if environment not in PAYPAL_API_BASE_URLS:
            raise ConfigurationError("PAYPAL_ENVIRONMENT must be 'sandbox' or 'live'.")

        config = cls(
            environment=environment,
            client_id=os.environ.get("PAYPAL_CLIENT_ID") or None,
            client_secret=os.environ.get("PAYPAL_CLIENT_SECRET") or None,
        )
        if require_credentials and (not config.client_id or not config.client_secret):
            raise ConfigurationError("PayPal credentials are not configured.")
        return config
