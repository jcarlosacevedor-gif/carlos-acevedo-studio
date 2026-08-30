"""Environment-based PayPal configuration without exposing secrets."""

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when a required, safe configuration value is unavailable."""


@dataclass(frozen=True)
class NetlifyProxyAuthConfig:
    secret: str
    site_id: str
    site_url: str
    deploy_context: str


def get_netlify_proxy_auth_config() -> NetlifyProxyAuthConfig | None:
    """Return complete proxy auth config, or None only for unprotected Sandbox/local."""
    names = (
        "NETLIFY_PROXY_SIGNING_SECRET",
        "NETLIFY_PROXY_EXPECTED_SITE_ID",
        "NETLIFY_PROXY_EXPECTED_SITE_URL",
        "NETLIFY_PROXY_EXPECTED_DEPLOY_CONTEXT",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    environment = os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").strip().lower()
    if environment not in PAYPAL_API_BASE_URLS:
        raise ConfigurationError("PAYPAL_ENVIRONMENT must be 'sandbox' or 'live'.")
    if not any(values.values()):
        if environment == "live":
            raise ConfigurationError("Live requires complete Netlify proxy authorization configuration.")
        return None
    if not all(values.values()):
        raise ConfigurationError("Netlify proxy authorization configuration must be complete.")
    return NetlifyProxyAuthConfig(values[names[0]], values[names[1]], values[names[2]], values[names[3]])


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
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ORDER_DB_PATH = PROJECT_ROOT / "instance" / "orders.sqlite3"


def _validate_base_url(value: str) -> str:
    """Validate and normalize a base URL for the public site."""
    if not value or not isinstance(value, str):
        raise ConfigurationError("PUBLIC_SITE_BASE_URL must be a non-empty string.")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ConfigurationError("PUBLIC_SITE_BASE_URL must be a valid absolute URL (e.g., http://localhost:8000).")
    if parsed.scheme not in ("http", "https"):
        raise ConfigurationError("PUBLIC_SITE_BASE_URL must use http or https scheme.")
    result = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.path:
        result += parsed.path.rstrip("/")
    return result


def get_public_site_base_url() -> str:
    """Get the configured public site base URL, validated."""
    return _validate_base_url(os.environ.get("PUBLIC_SITE_BASE_URL", "http://127.0.0.1:8000"))


def get_order_db_path() -> Path:
    """Return the configured SQLite path or preserve the historic local default."""
    value = os.environ.get("ORDER_DB_PATH")
    if value is None:
        return DEFAULT_ORDER_DB_PATH
    if not value.strip():
        raise ConfigurationError("ORDER_DB_PATH must be omitted or a non-empty path.")
    return Path(value)


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
