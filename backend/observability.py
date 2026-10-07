"""Small, fail-safe structured observability helpers for payment operations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
from typing import TextIO
from urllib.error import HTTPError, URLError


SCHEMA_VERSION = 1

EVENT_LEVELS = {
    "order_created_local": "INFO",
    "paypal_order_created": "INFO",
    "capture_started": "INFO",
    "capture_ambiguous": "WARNING",
    "capture_reconciled": "INFO",
    "paid": "INFO",
    "failed": "WARNING",
    "operational_error": "ERROR",
    "stale_order_detected": "WARNING",
}

REASON_CODES = frozenset({
    "timeout",
    "network",
    "http_408",
    "http_429",
    "paypal_5xx",
    "invalid_response",
    "create_ambiguous",
    "sqlite_error",
    "configuration_error",
    "show_failed",
    "stale_warning",
    "stale_critical",
})

_REF_PREFIXES = {
    "local": "local",
    "paypal_order": "ppord",
    "paypal_capture": "cap",
    "capture_request": "req",
}
_REF_PATTERNS = {
    field: re.compile(rf"^{prefix}_[0-9a-f]{{12}}$")
    for field, prefix in {
        "local_order_ref": "local",
        "paypal_order_ref": "ppord",
        "paypal_capture_ref": "cap",
        "capture_request_ref": "req",
    }.items()
}
_STRING_ENUMS = {
    "status_from": frozenset({"PENDING", "PAYPAL_CREATED", "CAPTURING", "PAID", "FAILED", "CANCELLED"}),
    "status_to": frozenset({"PENDING", "PAYPAL_CREATED", "CAPTURING", "PAID", "FAILED", "CANCELLED"}),
    "operation": frozenset({"create_order", "attach_paypal_order", "capture_order", "show_order", "mark_paid", "sqlite", "configuration", "stale_scan", "reconcile"}),
    "outcome": frozenset({"committed", "ambiguous", "failed", "eligible_for_apply_paid", "retry_requires_existing_capture_request_id", "no_action", "manual_review", "paid", "warning", "critical", "attention", "probable_abandoned_checkout"}),
    "source": frozenset({"api", "admin_cli", "stale_scan"}),
}
_BOOLEAN_FIELDS = frozenset({
    "order_id_matches",
    "capture_present",
    "capture_completed",
    "amount_matches",
    "currency_matches",
    "applied",
    "retryable",
    "paypal_order_present",
    "paypal_capture_present",
    "capture_request_present",
})
_INTEGER_FIELDS = frozenset({"http_status", "duration_ms"})
_ALLOWED_FIELDS = (
    frozenset({
        "local_order_ref",
        "paypal_order_ref",
        "paypal_capture_ref",
        "capture_request_ref",
        "status_from",
        "status_to",
        "operation",
        "outcome",
        "reason_code",
        "source",
    })
    | _BOOLEAN_FIELDS
    | _INTEGER_FIELDS
)


def safe_ref(kind: str, identifier: str | None) -> str | None:
    """Return a stable, type-separated reference without exposing the identifier."""
    if identifier is None:
        return None
    if kind not in _REF_PREFIXES or not isinstance(identifier, str) or not identifier:
        raise ValueError("Invalid safe reference input.")
    digest = hashlib.sha256(f"{kind}\0{identifier}".encode("utf-8")).hexdigest()[:12]
    return f"{_REF_PREFIXES[kind]}_{digest}"


def reason_code_for_exception(error: BaseException) -> str:
    """Classify an exception chain without serializing any exception text."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, HTTPError):
            if current.code == 408:
                return "http_408"
            if current.code == 429:
                return "http_429"
            if 500 <= current.code <= 599:
                return "paypal_5xx"
            return "invalid_response"
        if isinstance(current, (TimeoutError,)):
            return "timeout"
        if isinstance(current, URLError):
            if isinstance(current.reason, TimeoutError):
                return "timeout"
            return "network"
        current = current.__cause__ or current.__context__
    return "invalid_response"


def _validated_fields(fields: dict[str, object]) -> dict[str, object]:
    if not set(fields).issubset(_ALLOWED_FIELDS):
        raise ValueError("Event contains a non-allowlisted field.")
    clean: dict[str, object] = {}
    for name, value in fields.items():
        if value is None:
            continue
        if name in _REF_PATTERNS:
            if not isinstance(value, str) or _REF_PATTERNS[name].fullmatch(value) is None:
                raise ValueError("Invalid safe reference.")
        elif name == "reason_code":
            if value not in REASON_CODES:
                raise ValueError("Invalid reason code.")
        elif name in _STRING_ENUMS:
            if value not in _STRING_ENUMS[name]:
                raise ValueError("Invalid enumerated event field.")
        elif name in _BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise ValueError("Invalid boolean event field.")
        elif name in _INTEGER_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Invalid integer event field.")
            if name == "http_status" and not 100 <= value <= 599:
                raise ValueError("Invalid HTTP status.")
        clean[name] = value
    return clean


def emit_event(
    event: str,
    *,
    level: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    **fields: object,
) -> bool:
    """Emit one JSON line; return False instead of affecting application behavior."""
    try:
        default_level = EVENT_LEVELS[event]
        chosen_level = level or default_level
        if chosen_level not in {"INFO", "WARNING", "ERROR"}:
            raise ValueError("Invalid event level.")
        allowed_levels = {
            "capture_reconciled": {"INFO", "WARNING"},
            "stale_order_detected": {"WARNING", "ERROR"},
        }.get(event, {default_level})
        if chosen_level not in allowed_levels:
            raise ValueError("This event has a fixed level.")
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": chosen_level,
            "environment": os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").strip().lower(),
            "service": "backend",
        }
        if payload["environment"] not in {"sandbox", "live"}:
            payload["environment"] = "unknown"
        payload.update(_validated_fields(fields))
        stream = (stderr or sys.stderr) if chosen_level in {"WARNING", "ERROR"} else (stdout or sys.stdout)
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        return True
    except Exception:
        return False
