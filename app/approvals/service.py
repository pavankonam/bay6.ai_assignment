"""Approvals service: atomic insert-or-get, conditional decide/expire (FR37, FR42, FR43, §8.4.2).

Invariant: any row whose `status` leaves `pending` gets `resume_status='pending'` in the same
UPDATE, so every decision or expiry is guaranteed to be resumed (by the caller, the customer's
next message, or reconciliation).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col, select

from app import config
from app.approvals.models import Approval
from app.db import session_scope
from app.mock_api.repository import Policy
from app.observability.masking import mask_dob
from app.responses import templates

BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
DECISION_TO_STATUS = {"approve": "approved", "reject": "rejected", "expired": "expired"}
STATUS_TO_DECISION = {v: k for k, v in DECISION_TO_STATUS.items()}


@dataclass(frozen=True)
class ApprovalView:
    approval_id: str
    idempotency_key: str
    pending_action_id: str
    thread_id: str
    action_type: str
    payload: dict[str, Any]
    customer: dict[str, Any]
    risk_flags: list[str]
    summary: str
    status: str
    reviewer_id: str | None
    comment: str | None
    created_at: str
    decided_at: str | None
    resume_status: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def new_approval_id() -> str:
    return "APR-" + "".join(secrets.choice(BASE32) for _ in range(5))


def _view(row: Any) -> ApprovalView:
    return ApprovalView(
        approval_id=row.approval_id,
        idempotency_key=row.idempotency_key,
        pending_action_id=row.pending_action_id,
        thread_id=row.thread_id,
        action_type=row.action_type,
        payload=dict(row.payload or {}),
        customer=dict(row.customer or {}),
        risk_flags=list(row.risk_flags or []),
        summary=row.summary,
        status=row.status,
        reviewer_id=row.reviewer_id,
        comment=row.comment,
        created_at=row.created_at,
        decided_at=row.decided_at,
        resume_status=row.resume_status,
    )


def customer_for(policy_id: str) -> dict[str, Any]:
    """Reviewer view of the customer: full policy ID and name, masked DOB (NFR9). Read from the
    repository directly; the lookup tool never returns name or DOB (NFR8b)."""
    with session_scope() as session:
        policy = session.get(Policy, policy_id)
        if policy is None:
            return {"policy_id": policy_id, "holder_name": None, "dob_masked": None}
        return {
            "policy_id": policy_id,
            "holder_name": f"{policy.holder_first_name} {policy.holder_last_name}",
            "dob_masked": mask_dob(policy.dob),
        }


def build_summary(action: Mapping[str, Any], quote: Mapping[str, Any] | None) -> str:
    """Conversation summary for the reviewer, three sentences or fewer (FR37)."""
    payload = action.get("payload") or {}
    flags = ", ".join(action.get("approval_reasons") or []) or "none"
    if action.get("action_type") == "cancel_policy":
        refund = templates.money_exact(float((quote or {}).get("refund_estimate_usd") or 0))
        reason = templates.REASON_TEXT.get(str(payload.get("reason")), payload.get("reason"))
        return (
            f"Verified customer requests cancellation of {payload.get('policy_id')} effective "
            f"{payload.get('effective_date')} (reason: {reason}). Estimated refund {refund}. "
            f"Risk flags: {flags}."
        )
    return (
        f"Verified customer reports a claim on {payload.get('policy_id')} for an incident on "
        f"{payload.get('incident_date')} at {payload.get('incident_location')}. "
        f"Injuries: {templates.yes_no(payload.get('injuries'))}; estimate: "
        f"{templates.money(payload.get('estimated_damage_usd'))}. Risk flags: {flags}."
    )


class ApprovalsService:
    def __init__(self) -> None:
        self.ttl = timedelta(hours=config.get_settings().APPROVAL_TTL_HOURS)

    # --- reads ----------------------------------------------------------------------------------

    def get(self, approval_id: str) -> ApprovalView | None:
        with session_scope() as session:
            row = session.get(Approval, approval_id)
            return _view(row) if row is not None else None

    def is_past_ttl(self, approval: ApprovalView, now: datetime | None = None) -> bool:
        created = datetime.fromisoformat(approval.created_at)
        ttl = timedelta(hours=config.get_settings().APPROVAL_TTL_HOURS)
        return (now or config.utcnow()) - created > ttl

    def displayed_status(self, approval: ApprovalView) -> str:
        """Read paths show past-TTL approvals as expired without persisting it (FR43)."""
        if approval.status == "pending" and self.is_past_ttl(approval):
            return "expired"
        return approval.status

    def list_approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        with session_scope() as session:
            rows = session.exec(select(Approval).order_by(col(Approval.created_at))).all()
            views = [_view(r) for r in rows]
        result = []
        for view in views:
            shown = self.displayed_status(view)
            if status is None or shown == status:
                result.append({**view.to_dict(), "status": shown})
        return result

    def needing_resume(self) -> list[ApprovalView]:
        with session_scope() as session:
            rows = session.exec(select(Approval).where(Approval.resume_status == "pending")).all()
            return [_view(r) for r in rows]

    def pending_past_ttl(self) -> list[ApprovalView]:
        with session_scope() as session:
            rows = session.exec(select(Approval).where(Approval.status == "pending")).all()
            views = [_view(r) for r in rows]
        return [v for v in views if self.is_past_ttl(v)]

    def for_threads(self, thread_ids: Sequence[str]) -> list[ApprovalView]:
        with session_scope() as session:
            rows = session.exec(
                select(Approval).where(col(Approval.thread_id).in_(list(thread_ids)))
            ).all()
            return [_view(r) for r in rows]

    # --- writes ---------------------------------------------------------------------------------

    def get_or_create(
        self, *, thread_id: str, action: Mapping[str, Any], quote: Mapping[str, Any] | None
    ) -> ApprovalView:
        """Atomic `INSERT … ON CONFLICT(idempotency_key) DO NOTHING`, then `SELECT` (FR37)."""
        key = str(action["idempotency_key"])
        payload = dict(action.get("payload") or {})
        values = {
            "idempotency_key": key,
            "pending_action_id": action["pending_action_id"],
            "thread_id": thread_id,
            "action_type": action["action_type"],
            "payload": payload,
            "customer": customer_for(str(payload.get("policy_id"))),
            "risk_flags": list(action.get("approval_reasons") or []),
            "summary": build_summary(action, quote),
            "status": "pending",
            "created_at": config.utcnow_iso(),
            "resume_status": "none",
        }
        for _ in range(5):  # approval_id is random: retry the (unlikely) primary-key collision
            statement = (
                insert(Approval)
                .values(approval_id=new_approval_id(), **values)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            with session_scope() as session:
                try:
                    session.exec(statement)
                except Exception as exc:  # pragma: no cover - PK collision, astronomically rare
                    if "UNIQUE constraint failed: approvals.approval_id" not in str(exc):
                        raise
                    continue
            with session_scope() as session:
                row = session.exec(select(Approval).where(Approval.idempotency_key == key)).first()
                if row is not None:
                    return _view(row)
        raise RuntimeError("Could not allocate an approval id")  # pragma: no cover

    def _transition(self, approval_id: str, values: dict[str, Any]) -> bool:
        statement = (
            update(Approval)
            .where(col(Approval.approval_id) == approval_id)
            .where(col(Approval.status) == "pending")
            .values(**values, resume_status="pending")
        )
        with session_scope() as session:
            result = session.exec(statement)
            return bool(result.rowcount)

    def decide(
        self, approval_id: str, decision: str, reviewer_id: str, comment: str | None
    ) -> bool:
        """Conditional update `WHERE status='pending'` (FR42). False → nothing changed (409)."""
        return self._transition(
            approval_id,
            {
                "status": DECISION_TO_STATUS[decision],
                "reviewer_id": reviewer_id,
                "comment": comment,
                "decided_at": config.utcnow_iso(),
            },
        )

    def expire(self, approval_id: str) -> bool:
        """The only code that persists expiry (FR43)."""
        return self._transition(
            approval_id, {"status": "expired", "decided_at": config.utcnow_iso()}
        )

    def mark_resumed(self, approval_id: str) -> None:
        statement = (
            update(Approval)
            .where(col(Approval.approval_id) == approval_id)
            .values(resume_status="done")
        )
        with session_scope() as session:
            session.exec(statement)


approvals_service = ApprovalsService()
