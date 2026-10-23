"""Cross-thread per-policy verification lockout (FR19a, Hardening)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlmodel import col, select

from app import config
from app.approvals.models import VerificationFailure
from app.db import session_scope
from app.observability.logging import get_logger
from app.observability.metrics import metrics

log = get_logger(__name__)


def _window_start(now: datetime | None = None) -> str:
    hours = config.get_settings().POLICY_VERIFY_LOCK_HOURS
    return ((now or config.utcnow()) - timedelta(hours=hours)).isoformat()


def recent_failures(policy_id: str) -> int:
    with session_scope() as session:
        rows = session.exec(
            select(VerificationFailure)
            .where(VerificationFailure.policy_id == policy_id)
            .where(col(VerificationFailure.failed_at) >= _window_start())
        ).all()
        return len(rows)


def is_locked(policy_id: str) -> bool:
    return recent_failures(policy_id) >= config.get_settings().POLICY_VERIFY_LOCK_FAILURES


def record_failure(policy_id: str, thread_id: str) -> bool:
    """Record a failed verification under the submitted policy ID. Attempts made while the policy
    is already locked are not recorded, so they cannot extend the window. Returns True when this
    failure locks the policy."""
    if is_locked(policy_id):
        return False
    with session_scope() as session:
        session.add(
            VerificationFailure(
                policy_id=policy_id, thread_id=thread_id, failed_at=config.utcnow_iso()
            )
        )
    if is_locked(policy_id):
        metrics.inc("verification_lockouts_total")
        log.warning("policy_verification_locked", policy_id=policy_id)
        return True
    return False


def unlock(policy_id: str) -> int:
    """Operator unlock after phone verification: delete the policy's failure rows."""
    with session_scope() as session:
        result = session.exec(
            delete(VerificationFailure).where(col(VerificationFailure.policy_id) == policy_id)
        )
        removed = int(result.rowcount or 0)
    log.warning("policy_verification_unlocked", policy_id=policy_id, removed=removed)
    return removed


def purge_failures_older_than(days: int) -> int:
    cutoff = (config.utcnow() - timedelta(days=days)).isoformat()
    with session_scope() as session:
        result = session.exec(
            delete(VerificationFailure).where(col(VerificationFailure.failed_at) < cutoff)
        )
        return int(result.rowcount or 0)
