"""Approval reconciliation (FR43a, Hardening). Runs on startup and via the admin endpoint."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from app.observability.logging import get_logger

if TYPE_CHECKING:
    from app.runtime import SupportRuntime

log = get_logger(__name__)


@dataclass
class ReconcileReport:
    resumed: int = 0
    expired: int = 0
    repaired_interrupts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reconcile(runtime: SupportRuntime) -> ReconcileReport:
    """Holding each thread's lock:
    1. repair threads stuck between `announce_approval` and `interrupt()`;
    2. expire pending approvals past their TTL (through `expire()`, which sets resume pending);
    3. resume every approval with `resume_status='pending'` (decided/expired, not resumed).
    Write idempotency (FR27) makes a repeated resume safe."""
    report = ReconcileReport()
    approvals = runtime.approvals
    for approval in [a for a in approvals.list_views() if a.status == "pending"]:
        with runtime.locks.hold(approval.thread_id):
            snapshot = runtime.snapshot(approval.thread_id)
            if snapshot.next == ("human_approval",) and not snapshot.interrupts:
                runtime.ensure_interrupt(approval.thread_id)
                report.repaired_interrupts += 1
    for approval in approvals.pending_past_ttl():
        with runtime.locks.hold(approval.thread_id):
            if approvals.expire(approval.approval_id):
                report.expired += 1
    for approval in approvals.needing_resume():
        with runtime.locks.hold(approval.thread_id):
            current = approvals.get(approval.approval_id)
            if current is not None and current.resume_status == "pending":
                runtime.resume_thread(current)
                report.resumed += 1
    log.info("approvals_reconciled", **report.to_dict())
    return report
