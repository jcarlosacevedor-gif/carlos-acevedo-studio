"""Manual, Sandbox-only smoke commands for the isolated PayPal client."""

import argparse
import getpass
import os
import sys
import uuid
from collections.abc import Callable, Mapping

from .config import ConfigurationError, PayPalConfig
from .paypal_client import PayPalClient, PayPalClientError, validate_order_id
from .pricing import PricingError, calculate_custom_song_price


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


SOLO_CHOICES = ("none", "guitar-solo", "piano-solo")


def custom_song_pricing(solo: str) -> dict[str, object]:
    return calculate_custom_song_price({"product": "custom-song", "solo": solo})


def run_create(client: PayPalClient, solo: str, request_id_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> dict[str, str]:
    pricing = custom_song_pricing(solo)
    return client.create_order(
        amount_cents=pricing["amount_cents"],
        currency=pricing["currency"],
        request_id=str(request_id_factory()),
        return_url="https://example.com/paypal-sandbox-return",
        cancel_url="https://example.com/paypal-sandbox-cancel",
    )


def run_capture(client: PayPalClient, order_id: str, solo: str, request_id_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> dict[str, str | None]:
    order_id = validate_order_id(order_id)
    result = client.capture_order(order_id, str(request_id_factory()))
    pricing = custom_song_pricing(solo)
    expected_amount = f"{pricing['amount_cents'] // 100}.{pricing['amount_cents'] % 100:02d}"
    checks = {
        "order ID": result.get("order_id") == order_id,
        "order status": result.get("order_status") == "COMPLETED",
        "capture status": result.get("capture_status") == "COMPLETED",
        "amount": result.get("amount") == expected_amount,
        "currency": result.get("currency") == pricing["currency"],
    }
    failed_check = next((name for name, passed in checks.items() if not passed), None)
    if failed_check:
        raise SmokeRunnerError(f"Payment verification failed: {failed_check} did not match.")
    return result


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
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth")
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--solo", choices=SOLO_CHOICES, required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("order_id")
    capture_parser.add_argument("--solo", choices=SOLO_CHOICES, required=True)
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    try:
        if args.command == "capture":
            validate_order_id(args.order_id)
        client = client_factory(load_sandbox_config(environ, input_fn, secret_fn))
        if args.command == "auth":
            run_authentication(client)
            print("PayPal Sandbox authentication: OK", file=stdout)
            return 0

        if args.command == "capture":
            pricing = custom_song_pricing(args.solo)
            expected_amount = _format_amount(pricing["amount_cents"], pricing["currency"])
            print(f"Order to capture: {args.order_id}", file=stdout)
            print(f"Expected configuration: {args.solo}", file=stdout)
            print(f"Expected amount: {expected_amount}", file=stdout)
            print("This will capture the approved Sandbox order.", file=stdout)
            if input_fn("Continue? [y/N] ").strip().lower() != "y":
                print("Capture cancelled.", file=stdout)
                return 0
            result = run_capture(client, args.order_id, args.solo, request_id_factory)
            print("PAYMENT CONFIRMED", file=stdout)
            print(f"Order ID: {result['order_id']}", file=stdout)
            print(f"Capture ID: {result['capture_id']}", file=stdout)
            print(f"Amount: {result['amount']} {result['currency']}", file=stdout)
            return 0

        result = run_create(client, args.solo, request_id_factory)
        print("PayPal Sandbox Create Order: OK", file=stdout)
        print(f"Configuration: {args.solo}", file=stdout)
        print(f"Order ID: {result['order_id']}", file=stdout)
        print(f"Status: {result['status']}", file=stdout)
        pricing = custom_song_pricing(args.solo)
        amount = _format_amount(pricing["amount_cents"], pricing["currency"])
        print(f"Amount: {amount}", file=stdout)
        print(f"Approval URL: {result['approval_url']}", file=stdout)
        return 0
    except (SmokeRunnerError, ConfigurationError, PayPalClientError, PricingError) as error:
        print(f"PayPal Sandbox smoke failed: {error}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
