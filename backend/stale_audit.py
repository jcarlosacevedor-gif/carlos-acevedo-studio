"""Read-only stale-order classification shared by operational entry points."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import StaleOrderThresholds
from .observability import emit_event, safe_ref
from .order_store import AdminOrderRecord, OrderStore


@dataclass(frozen=True)
class StaleFinding:
    local_order_ref: str | None
    status: str
    age_seconds: int
    classification: str
    exit_code: int
    event_level: str | None = None
    reason_code: str | None = None
    outcome: str | None = None
    paypal_order_present: bool = False
    paypal_capture_present: bool = False
    capture_request_present: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "local_order_ref": self.local_order_ref,
            "status": self.status,
            "age_seconds": self.age_seconds,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class StaleAuditReport:
    findings: tuple[StaleFinding, ...]
    highest_exit: int

    def as_payload(self) -> dict[str, object]:
        return {
            "findings": [finding.as_payload() for finding in self.findings],
            "result": "incidents" if self.highest_exit > 0 else "ok",
        }


def _age_seconds(record: AdminOrderRecord, current: datetime) -> int:
    updated = datetime.fromisoformat(record.updated_at)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return max(0, int((current - updated.astimezone(timezone.utc)).total_seconds()))


def audit_stale(
    store: OrderStore,
    thresholds: StaleOrderThresholds,
    now: datetime,
) -> StaleAuditReport:
    """Classify stale records without mutating the store or producing logs."""
    current = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    findings: list[StaleFinding] = []
    highest_exit = 0

    for status in ("CAPTURING", "PENDING", "PAYPAL_CREATED"):
        for record in store.list_admin_orders_for_audit(status):
            age = _age_seconds(record, current)
            classification: str | None = None
            exit_code = 0
            event_level: str | None = None
            reason_code: str | None = None
            outcome: str | None = None
            if status in {"CAPTURING", "PENDING"}:
                if age >= thresholds.capturing_critical_seconds:
                    event_level, reason_code, exit_code = "ERROR", "stale_critical", 3
                    classification = "critical_manual_review" if status == "CAPTURING" else "manual_review"
                    outcome = "critical"
                elif age >= thresholds.capturing_warning_seconds:
                    event_level, reason_code, exit_code = "WARNING", "stale_warning", 2
                    classification = "warning" if status == "CAPTURING" else "attention"
                    outcome = "warning" if status == "CAPTURING" else "attention"
            elif age >= 86400:
                classification = "probable_abandoned_checkout"

            if classification is None:
                continue
            finding = StaleFinding(
                local_order_ref=safe_ref("local", record.local_order_id),
                status=status,
                age_seconds=age,
                classification=classification,
                exit_code=exit_code,
                event_level=event_level,
                reason_code=reason_code,
                outcome=outcome,
                paypal_order_present=record.paypal_order_id is not None,
                paypal_capture_present=record.paypal_capture_id is not None,
                capture_request_present=record.capture_request_id is not None,
            )
            findings.append(finding)
            highest_exit = max(highest_exit, exit_code)

    return StaleAuditReport(tuple(findings), highest_exit)


def emit_stale_events(report: StaleAuditReport, emitter=None) -> None:
    """Best-effort sanitized events for actionable CAPTURING/PENDING findings."""
    event_emitter = emitter or emit_event
    for finding in report.findings:
        if finding.status not in {"CAPTURING", "PENDING"} or finding.event_level is None:
            continue
        try:
            event_emitter(
                "stale_order_detected",
                level=finding.event_level,
                local_order_ref=finding.local_order_ref,
                status_from=finding.status,
                operation="stale_scan",
                outcome=finding.outcome,
                reason_code=finding.reason_code,
                source="stale_scan",
                paypal_order_present=finding.paypal_order_present,
                paypal_capture_present=finding.paypal_capture_present,
                capture_request_present=finding.capture_request_present,
            )
        except Exception:
            continue
