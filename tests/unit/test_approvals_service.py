"""M5: approvals service — insert-or-get uniqueness, conditional decide/expire, TTL display."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest

from app.approvals.service import ApprovalsService, build_summary, customer_for, new_approval_id
from app.config import utcnow

pytestmark = pytest.mark.usefixtures("seeded")

ACTION: dict[str, Any] = {
    "pending_action_id": "pa-1",
    "action_type": "cancel_policy",
    "payload": {"policy_id": "POL-100001", "effective_date": "2026-11-01", "reason": "cost"},
    "idempotency_key": "idem-1",
    "requires_approval": True,
    "approval_reasons": ["cancellation_requires_review"],
}


def test_approval_id_format() -> None:
    assert re.fullmatch(r"APR-[A-Z2-7]{5}", new_approval_id())


def test_get_or_create_is_idempotent_and_builds_customer() -> None:
    svc = ApprovalsService()
    first = svc.get_or_create(thread_id="t1", action=ACTION, quote={"refund_estimate_usd": 175.55})
    second = svc.get_or_create(thread_id="t1", action=ACTION, quote=None)
    assert first.approval_id == second.approval_id
    assert first.customer == {
        "policy_id": "POL-100001",
        "holder_name": "Jane Doe",
        "dob_masked": "****-**-12",
    }
    assert first.status == "pending"
    assert first.resume_status == "none"
    assert "$175.55" in first.summary
    other = svc.get_or_create(
        thread_id="t1", action={**ACTION, "idempotency_key": "idem-2"}, quote=None
    )
    assert other.approval_id != first.approval_id


def test_decide_is_conditional_and_sets_resume_pending() -> None:
    svc = ApprovalsService()
    approval = svc.get_or_create(thread_id="t1", action=ACTION, quote=None)
    assert svc.decide(approval.approval_id, "approve", "marcus", "ok") is True
    decided = svc.get(approval.approval_id)
    assert decided is not None
    assert (decided.status, decided.resume_status, decided.reviewer_id, decided.comment) == (
        "approved",
        "pending",
        "marcus",
        "ok",
    )
    assert decided.decided_at is not None
    assert svc.decide(approval.approval_id, "reject", "x", None) is False
    assert svc.expire(approval.approval_id) is False
    svc.mark_resumed(approval.approval_id)
    assert svc.get(approval.approval_id).resume_status == "done"  # type: ignore[union-attr]
    assert [a.approval_id for a in svc.needing_resume()] == []


def test_expire_sets_resume_pending() -> None:
    svc = ApprovalsService()
    approval = svc.get_or_create(thread_id="t1", action=ACTION, quote=None)
    assert svc.expire(approval.approval_id) is True
    expired = svc.get(approval.approval_id)
    assert expired is not None
    assert (expired.status, expired.resume_status) == ("expired", "pending")
    assert [a.approval_id for a in svc.needing_resume()] == [approval.approval_id]


def test_ttl(set_env: Any) -> None:
    svc = ApprovalsService()
    approval = svc.get_or_create(thread_id="t1", action=ACTION, quote=None)
    assert not svc.is_past_ttl(approval)
    assert svc.is_past_ttl(approval, now=utcnow() + timedelta(hours=49))
    assert svc.pending_past_ttl() == []
    set_env(APPROVAL_TTL_HOURS="0")
    assert svc.is_past_ttl(approval, now=utcnow() + timedelta(seconds=1))
    assert svc.displayed_status(approval) == "expired"
    assert [a.approval_id for a in svc.pending_past_ttl()] == [approval.approval_id]
    assert [a.approval_id for a in svc.for_threads(["t1"])] == [approval.approval_id]


def test_customer_for_unknown_policy() -> None:
    assert customer_for("POL-000000") == {
        "policy_id": "POL-000000",
        "holder_name": None,
        "dob_masked": None,
    }


def test_claim_summary_is_three_sentences() -> None:
    summary = build_summary(
        {
            "action_type": "create_claim",
            "payload": {
                "policy_id": "POL-100001",
                "incident_date": "2026-10-21",
                "incident_location": "Main St",
                "injuries": True,
                "estimated_damage_usd": None,
            },
            "approval_reasons": ["injuries_reported", "damage_estimate_unknown"],
        },
        None,
    )
    assert len(re.findall(r"\.(\s|$)", summary)) == 3
    assert "injuries_reported, damage_estimate_unknown" in summary
