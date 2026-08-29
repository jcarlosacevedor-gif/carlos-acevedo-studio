"""Manual, Sandbox-only smoke commands for the isolated PayPal client."""

import argparse
import getpass
import os
import sys
import uuid
from collections.abc import Callable, Mapping

from .config import ConfigurationError, PayPalConfig
from .paypal_client import PayPalClient, PayPalClientError
from .pricing import calculate_custom_song_price


class SmokeRunnerError(RuntimeError):
    """A safe error that may be printed by this development-only CLI."""


def load_sandbox_config(
    environ: Mapping[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> PayPalConfig:
    """Read credentials only from this process environment or interactive input."""
    source = os.environ if environ is None else environ
    environment = source.get("PAYPAL_ENVIRONMENT", "sandbox").strip().lower()
    if environment != "sandbox":
        raise SmokeRunnerError("This smoke runner only permits PAYPAL_ENVIRONMENT=sandbox.")

    client_id = source.get("PAYPAL_CLIENT_ID") or input_fn("PayPal Sandbox Client ID: ").strip()
    client_secret = source.get("PAYPAL_CLIENT_SECRET") or secret_fn("PayPal Sandbox Client Secret: ")
    if not client_id or not client_secret:
        raise SmokeRunnerError("PayPal Sandbox credentials are required.")
    return PayPalConfig("sandbox", client_id, client_secret)


def run_authentication(client: PayPalClient) -> None:
    client.check_authentication()


def run_create_199(client: PayPalClient, request_id_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> dict[str, str]:
    pricing = calculate_custom_song_price({"product": "custom-song", "solo": "none"})
    return client.create_order(
        amount_cents=pricing["amount_cents"],
        currency=pricing["currency"],
        request_id=str(request_id_factory()),
    )


def _format_amount(amount_cents: int, currency: str) -> str:
    return f"${amount_cents / 100:.2f} {currency}"


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    client_factory: Callable[[PayPalConfig], PayPalClient] = PayPalClient,
    request_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    stdout=None,
    stderr=None,
) -> int:
    parser = argparse.ArgumentParser(description="Manual PayPal Sandbox smoke runner.")
    parser.add_argument("command", choices=("auth", "create-199"))
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    try:
        client = client_factory(load_sandbox_config(environ, input_fn, secret_fn))
        if args.command == "auth":
            run_authentication(client)
            print("PayPal Sandbox authentication: OK", file=stdout)
            return 0

        result = run_create_199(client, request_id_factory)
        print("PayPal Sandbox Create Order: OK", file=stdout)
        print(f"Order ID: {result['order_id']}", file=stdout)
        print(f"Status: {result['status']}", file=stdout)
        pricing = calculate_custom_song_price({"product": "custom-song", "solo": "none"})
        amount = _format_amount(pricing["amount_cents"], pricing["currency"])
        print(f"Amount: {amount}", file=stdout)
        return 0
    except (SmokeRunnerError, ConfigurationError, PayPalClientError) as error:
        print(f"PayPal Sandbox smoke failed: {error}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
