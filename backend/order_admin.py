"""Minimal, safe administrative CLI for payment-order reconciliation."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Callable, TextIO

from .config import ConfigurationError, get_order_db_path
from .order_service import evaluate_capture_reconciliation
from .order_store import AdminOrderRecord, OrderStore, OrderStoreError, STATUSES
from .paypal_client import PayPalClient, PayPalClientError, PayPalConfigurationError


MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50
_AGE_PATTERN = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[smhd])$")
_AGE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class OrderAdminError(RuntimeError):
    """Safe operational error suitable for stderr."""


def _safe_ref(value: str | None) -> str | None:
    if value is None:
        return None
    suffix = value[-8:]
    return f"...{suffix}"


def _amount(amount_cents: int) -> str:
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"


def _parse_age(value: str) -> timedelta:
    match = _AGE_PATTERN.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("age must use a positive integer plus s, m, h, or d")
    seconds = int(match.group("value")) * _AGE_SECONDS[match.group("unit")]
    return timedelta(seconds=seconds)


def _parse_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    return limit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.order_admin",
        description="Safely inspect and reconcile Custom Song payment orders.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List operational order metadata (read-only).")
    list_parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    list_parser.add_argument("--older-than", type=_parse_age, metavar="AGE")
    list_parser.add_argument("--limit", type=_parse_limit, default=DEFAULT_LIST_LIMIT)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect one order without private brief data.")
    inspect_parser.add_argument("local_order_id")

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Run Show Order; never executes PayPal Capture.",
    )
    reconcile_parser.add_argument("local_order_id")
    reconcile_parser.add_argument(
        "--apply-paid",
        action="store_true",
        help="Apply CAPTURING -> PAID only when every remote check passes.",
    )
    return parser


def _load_store(*, read_only: bool) -> OrderStore:
    database_path = get_order_db_path()
    if not Path(database_path).is_file():
        raise OrderAdminError("Order database does not exist.")
    return OrderStore(database_path, initialize=False, read_only=read_only)


def _metadata(record: AdminOrderRecord) -> dict[str, object]:
    return {
        "local_order_ref": _safe_ref(record.local_order_id),
        "status": record.status,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "product": record.product,
        "solo": record.solo,
        "amount": _amount(record.amount_cents),
        "currency": record.currency,
        "paypal_order_present": record.paypal_order_id is not None,
        "paypal_capture_present": record.paypal_capture_id is not None,
        "capture_request_present": record.capture_request_id is not None,
    }


def _reconcile_output(
    record: AdminOrderRecord,
    *,
    action: str,
    applied: bool = False,
    remote_order_status: str | None = None,
    order_id_matches: bool | None = None,
    capture_present: bool | None = None,
    capture_completed: bool | None = None,
    amount_matches: bool | None = None,
    currency_matches: bool | None = None,
) -> dict[str, object]:
    return {
        "local_order_ref": _safe_ref(record.local_order_id),
        "local_status": record.status,
        "remote_order_status": remote_order_status,
        "order_id_matches": order_id_matches,
        "capture_present": capture_present,
        "capture_completed": capture_completed,
        "amount_matches": amount_matches,
        "currency_matches": currency_matches,
        "action": action,
        "applied": applied,
    }


def _write_json(stream: TextIO, payload: object) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _run_list(args: argparse.Namespace, stdout: TextIO, now: datetime | None) -> int:
    store = _load_store(read_only=True)
    cutoff = None
    if args.older_than is not None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = (current.astimezone(timezone.utc) - args.older_than).isoformat(timespec="seconds")
    records = store.list_admin_orders(status=args.status, limit=args.limit, updated_before=cutoff)
    _write_json(stdout, [_metadata(record) for record in records])
    return 0


def _run_inspect(args: argparse.Namespace, stdout: TextIO) -> int:
    store = _load_store(read_only=True)
    record = store.get_admin_order(args.local_order_id)
    if record is None:
        raise OrderAdminError("Order was not found.")
    _write_json(stdout, _metadata(record))
    return 0


def _run_reconcile(
    args: argparse.Namespace,
    stdout: TextIO,
    paypal_client_factory: Callable[[], object],
) -> int:
    store = _load_store(read_only=not args.apply_paid)
    record = store.get_admin_order(args.local_order_id)
    if record is None:
        raise OrderAdminError("Order was not found.")

    if record.status == "PAID":
        _write_json(stdout, _reconcile_output(record, action="already_reconciled"))
        return 0
    if record.status != "CAPTURING":
        _write_json(stdout, _reconcile_output(record, action="not_supported"))
        return 3 if args.apply_paid else 0
    if record.paypal_order_id is None:
        _write_json(stdout, _reconcile_output(record, action="manual_review"))
        return 3 if args.apply_paid else 0

    paypal_client = paypal_client_factory()
    paypal_order = paypal_client.show_order(record.paypal_order_id)
    evaluation = evaluate_capture_reconciliation(record, paypal_order)
    payload = _reconcile_output(
        record,
        action=evaluation.action,
        remote_order_status=evaluation.remote_order_status,
        order_id_matches=evaluation.order_id_matches,
        capture_present=evaluation.capture_present,
        capture_completed=evaluation.capture_completed,
        amount_matches=evaluation.amount_matches,
        currency_matches=evaluation.currency_matches,
    )
    if not args.apply_paid:
        _write_json(stdout, payload)
        return 0
    if evaluation.action != "eligible_for_apply_paid":
        _write_json(stdout, payload)
        return 3

    assert record.capture_request_id is not None
    assert record.paypal_order_id is not None
    assert evaluation.capture_id is not None
    paid = store.mark_capturing_paid(
        record.local_order_id,
        record.paypal_order_id,
        evaluation.capture_id,
        record.amount_cents,
        record.currency,
        record.capture_request_id,
    )
    payload["action"] = "paid"
    payload["applied"] = True
    payload["local_status"] = paid.status
    _write_json(stdout, payload)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    paypal_client_factory: Callable[[], object] | None = None,
    now: datetime | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = _parser().parse_args(argv)
    paypal_client_factory = paypal_client_factory or PayPalClient.from_environment
    try:
        if args.command == "list":
            return _run_list(args, stdout, now)
        if args.command == "inspect":
            return _run_inspect(args, stdout)
        return _run_reconcile(args, stdout, paypal_client_factory)
    except ConfigurationError:
        stderr.write("error: invalid or incomplete configuration.\n")
        return 4
    except OrderAdminError as error:
        stderr.write(f"error: {error}\n")
        return 4
    except PayPalConfigurationError:
        stderr.write("error: PayPal credentials or environment are not configured.\n")
        return 4
    except PayPalClientError:
        stderr.write("error: PayPal Show Order failed; no local changes were made.\n")
        return 5
    except (OrderStoreError, sqlite3.Error, OSError):
        stderr.write("error: order database operation failed safely.\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
