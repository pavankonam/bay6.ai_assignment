"""M4: FR26 write guard, prepare_action / FR27 keys, confirmation classification helpers."""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import select

from app.db import session_scope
from app.graph.nodes.execute_write_action import check_write_guard, execute_write_action
from app.graph.nodes.handle_confirmation import deterministic_changes
from app.graph.nodes.prepare_action import claim_approval_reasons, prepare_action
from app.mock_api.repository import Claim
from app.observability.metrics import metrics
from app.tools.errors import UnauthorizedWriteError
from app.tools.idempotency import idempotency_key

pytestmark = pytest.mark.usefixtures("seeded")

PAYLOAD = {
    "policy_id": "POL-100001",
    "incident_date": "2026-10-21",
    "incident_location": "Main St",
    "incident_description": "Rear-ended at a light",
    "injuries": False,
    "other_party_involved": True,
    "estimated_damage_usd": 2500.0,
    "police_report_number": None,
}


def write_state(**overrides: Any) -> dict[str, Any]:
    action = {
        "pending_action_id": "pa-1",
        "action_type": "create_claim",
        "payload": dict(PAYLOAD),
        "idempotency_key": "key-guard",
        "requires_approval": False,
        "approval_reasons": [],
        "allow_duplicate": False,
        "customer_confirmed": True,
        "approval_id": None,
    }
    action.update(overrides.pop("action", {}))
    state: dict[str, Any] = {
        "verified": True,
        "verified_policy_id": "POL-100001",
        "pending_action": action,
        "approval": None,
        "messages": [],
    }
    state.update(overrides)
    return state


def claim_count() -> int:
    with session_scope() as session:
        return len(session.exec(select(Claim)).all())


def test_guard_passes_for_valid_state() -> None:
    check_write_guard(write_state())


@pytest.mark.parametrize(
    "overrides",
    [
        {"verified": False},
        {"verified_policy_id": "POL-100002"},
        {"pending_action": None},
        {"action": {"customer_confirmed": False}},
        {"action": {"payload": {**PAYLOAD, "incident_location": None}}},
        {"action": {"payload": {**PAYLOAD, "incident_date": "2030-01-01"}}},
        {"action": {"requires_approval": True}},
        {
            "action": {"requires_approval": True, "approval_id": "APR-AAAAA"},
            "approval": {"approval_id": "APR-AAAAA", "status": "rejected"},
        },
        {
            "action": {"requires_approval": True, "approval_id": "APR-AAAAA"},
            "approval": {"approval_id": "APR-BBBBB", "status": "approved"},
        },
        {
            "action": {"requires_approval": True, "approval_id": None},
            "approval": {"approval_id": "APR-BBBBB", "status": "approved"},
        },
    ],
)
def test_guard_blocks(overrides: dict[str, Any]) -> None:
    with pytest.raises(UnauthorizedWriteError):
        check_write_guard(write_state(**overrides))


def test_guard_allows_matching_approval() -> None:
    check_write_guard(
        write_state(
            action={"requires_approval": True, "approval_id": "APR-AAAAA"},
            approval={"approval_id": "APR-AAAAA", "status": "approved"},
        )
    )


def test_blocked_write_is_internal_error_and_writes_nothing() -> None:
    before = claim_count()
    update = execute_write_action(write_state(verified=False))
    assert update["error"]["code"] == "UNAUTHORIZED_WRITE"
    assert update["error"]["internal"] is True
    assert update["error"]["retryable"] is False
    assert claim_count() == before
    assert metrics.total("unauthorized_write_blocked_total") == 1


def test_execute_write_success_records_audit_messages() -> None:
    update = execute_write_action(write_state())
    assert update["last_outcome"] == "CLAIM_CREATED"
    assert update["response_facts"]["result"]["claim_id"] == "CLM-2026-000002"
    call, result = update["messages"]
    assert call.tool_calls[0]["name"] == "create_claim"
    assert call.tool_calls[0]["args"]["idempotency_key"] == "key-guard"
    assert result.artifact["ok"] is True


def test_execute_write_not_eligible_and_duplicate() -> None:
    not_eligible = execute_write_action(
        write_state(
            verified_policy_id="POL-100002",
            action={
                "payload": {**PAYLOAD, "policy_id": "POL-100002", "incident_date": "2026-10-01"},
                "idempotency_key": "k-ne",
            },
        )
    )
    assert not_eligible["last_outcome"] == "WRITE_NOT_ELIGIBLE"
    duplicate = execute_write_action(
        write_state(
            verified_policy_id="POL-100006",
            action={
                "payload": {**PAYLOAD, "policy_id": "POL-100006", "incident_date": "2026-10-15"},
                "idempotency_key": "k-dup",
            },
        )
    )
    assert duplicate["last_outcome"] == "CLAIM_NOT_FILED_DUPLICATE"
    assert duplicate["response_facts"]["existing_claim_id"] == "CLM-2026-000001"


def test_prepare_action_builds_fresh_ids_and_keys() -> None:
    state = {
        "active_intent": "cancel_policy",
        "verified_policy_id": "POL-100001",
        "slots": {
            "policy_id": "POL-100001",
            "dob": "1985-04-12",
            "effective_date": "2026-11-01",
            "reason": "cost",
        },
        "duplicate_override": False,
    }
    config: Any = {"configurable": {"thread_id": "thread-xyz"}}
    first = prepare_action(state, config)["pending_action"]
    second = prepare_action(state, config)["pending_action"]
    assert first["pending_action_id"] != second["pending_action_id"]
    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["payload"] == {
        "policy_id": "POL-100001",
        "effective_date": "2026-11-01",
        "reason": "cost",
    }
    assert "dob" not in first["payload"]
    assert first["idempotency_key"] == idempotency_key(
        "thread-xyz", first["pending_action_id"], "cancel_policy", first["payload"]
    )
    assert first["requires_approval"] is True
    assert first["customer_confirmed"] is False
    assert first["approval_id"] is None


@pytest.mark.parametrize(
    ("slots", "reasons"),
    [
        ({"injuries": True, "estimated_damage_usd": 100.0}, ["injuries_reported"]),
        ({"injuries": False, "estimated_damage_usd": 10000.0}, ["high_damage_estimate"]),
        ({"injuries": False, "estimated_damage_usd": 9999.99}, []),
        ({"injuries": False, "estimated_damage_usd": None}, ["damage_estimate_unknown"]),
        (
            {"injuries": True, "estimated_damage_usd": None},
            ["injuries_reported", "damage_estimate_unknown"],
        ),
    ],
)
def test_claim_approval_reasons(slots: dict[str, Any], reasons: list[str]) -> None:
    assert claim_approval_reasons(slots) == reasons


def test_deterministic_changes() -> None:
    payload = {"policy_id": "POL-100001", "incident_date": "2026-10-21"}
    assert deterministic_changes("confirm but date is 10/19", payload, "report_claim") == {
        "incident_date": "2026-10-19"
    }
    assert deterministic_changes("yes 10/21 is right", payload, "report_claim") == {}
    assert deterministic_changes("use POL-100004", payload, "report_claim") == {
        "policy_id": "POL-100004"
    }
    assert deterministic_changes(
        "make it Nov 5", {"effective_date": "2026-11-01"}, "cancel_policy"
    ) == {"effective_date": "2026-11-05"}
