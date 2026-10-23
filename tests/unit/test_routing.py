"""M2: every conditional edge function, table-driven (Section 8.3)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END

from app.graph import routing

VERIFIED_SLOTS = {"policy_id": "POL-100001", "dob": "1985-04-12"}
SNAPSHOT = {"policy_id": "POL-100001", "status": "ACTIVE"}


def human(text: str = "hello", **annotations: Any) -> HumanMessage:
    return HumanMessage(content=text, id="h1", additional_kwargs=annotations)


def st(text: str = "hello", annotations: dict[str, Any] | None = None, **values: Any) -> dict:
    return {"messages": [human(text, **(annotations or {}))], **values}


def verified(**values: Any) -> dict:
    base: dict[str, Any] = {
        "messages": [human()],
        "active_intent": "report_claim",
        "slots": dict(VERIFIED_SLOTS),
        "verified": True,
        "verified_policy_id": "POL-100001",
        "policy_snapshot": dict(SNAPSHOT),
        "missing_slots": [],
    }
    base.update(values)
    return base


# --- route_entry -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (st(status="locked", turn_count=1), "escalate"),
        (st(turn_count=31), "escalate"),
        (st("   ", turn_count=1), "clarify"),
        (st(status="awaiting_confirmation", turn_count=2), "handle_confirmation"),
        (
            st(pending_question={"kind": "next_task", "context": {}}, turn_count=2),
            "handle_pending_question",
        ),
        (st(turn_count=1), "understand"),
        # precedence: locked beats empty and confirmation
        (st("", status="locked", turn_count=1), "escalate"),
        (
            st(
                "",
                status="awaiting_confirmation",
                pending_question={"kind": "next_task", "context": {}},
            ),
            "clarify",
        ),
        (
            st(
                status="awaiting_confirmation",
                pending_question={"kind": "next_task", "context": {}},
            ),
            "handle_confirmation",
        ),
        (st(turn_count=30), "understand"),
    ],
)
def test_route_entry(state: dict, expected: str) -> None:
    assert routing.route_entry(state) == expected


def test_route_entry_no_messages_is_empty() -> None:
    assert routing.route_entry({"messages": [AIMessage(content="hi")]}) == "clarify"


# --- route_intent ------------------------------------------------------------------------------


def nlu(primary: str | None, confidence: float = 0.9, **extra: Any) -> dict[str, Any]:
    return {"nlu": {"primary": primary, "confidence": confidence, **extra}}


@pytest.mark.parametrize(
    ("annotations", "values", "expected"),
    [
        (nlu("human_handoff", 0.1, date_ambiguity={"x": 1}), {}, "escalate"),
        (nlu("out_of_scope", 0.2), {}, "respond"),
        (nlu("smalltalk"), {}, "respond"),
        (nlu("report_claim", 0.3), {}, "clarify"),
        (nlu("report_claim", 0.95, date_ambiguity={"dates": ["4/12/85", "10/20"]}), {}, "clarify"),
        (
            nlu("report_claim", 0.3),
            {"clarification_attempts": {"_intent": 3}},
            "escalate",
        ),
        # read-retry: a non-new-intent message after a read failure re-enters validate_slots
        (
            nlu("provide_info"),
            {"status": "failed_retryable", "resume_step": "read_tool"},
            "validate_slots",
        ),
        (nlu("report_claim"), {}, "validate_slots"),
        (nlu("provide_info"), {}, "validate_slots"),
        # smalltalk during a read failure leaves the failure in place → respond
        (nlu("smalltalk"), {"status": "failed_retryable", "resume_step": "read_tool"}, "respond"),
        # threshold is inclusive: 0.6 is confident enough
        (nlu("policy_status", 0.6), {}, "validate_slots"),
        ({}, {}, "clarify"),
    ],
)
def test_route_intent(annotations: dict, values: dict, expected: str) -> None:
    assert routing.route_intent(st(annotations=annotations, **values)) == expected


# --- route_slots: each precedence step ---------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        # 1. max clarification attempts for a still-missing slot
        (
            verified(
                missing_slots=["incident_location"], clarification_attempts={"incident_location": 3}
            ),
            "escalate",
        ),
        # 1 beats 2: even with no active intent
        (
            {"missing_slots": ["policy_id"], "clarification_attempts": {"policy_id": 3}},
            "escalate",
        ),
        # attempts on a slot that is no longer missing do not escalate
        (verified(clarification_attempts={"incident_location": 5}), "prepare_action"),
        # 2. no active intent (FR6a), identity present or not
        ({"slots": dict(VERIFIED_SLOTS), "active_intent": None}, "clarify"),
        ({"slots": {}}, "clarify"),
        # 3. identity missing
        (verified(slots={"policy_id": "POL-100001"}, verified=False), "ask_for_info"),
        (verified(slots={"dob": "1985-04-12"}), "ask_for_info"),
        # 4. not verified / verified for another policy
        (verified(verified=False), "plan_tool"),
        (verified(verified_policy_id="POL-100002"), "plan_tool"),
        # 5. snapshot not loaded / for another policy; status intent always looks up fresh
        (verified(policy_snapshot=None), "plan_tool"),
        (verified(policy_snapshot={"policy_id": "POL-100002"}), "plan_tool"),
        (verified(active_intent="policy_status"), "plan_tool"),
        # 6. claim ineligible
        (
            verified(
                claim_checks={
                    "incident_date": "2026-10-21",
                    "eligible": False,
                    "reason": "TOO_OLD",
                    "duplicate_claim_id": None,
                },
                missing_slots=["incident_location"],
            ),
            "respond",
        ),
        # 7. duplicate without override
        (
            verified(
                claim_checks={
                    "incident_date": "2026-10-15",
                    "eligible": True,
                    "reason": None,
                    "duplicate_claim_id": "CLM-2026-000001",
                },
                missing_slots=["incident_location"],
            ),
            "ask_yes_no",
        ),
        # 7 skipped with override → 8
        (
            verified(
                claim_checks={
                    "incident_date": "2026-10-15",
                    "eligible": True,
                    "reason": None,
                    "duplicate_claim_id": "CLM-2026-000001",
                },
                duplicate_override=True,
                missing_slots=["incident_location"],
            ),
            "ask_for_info",
        ),
        # 8. task slots missing
        (verified(missing_slots=["injuries", "other_party_involved"]), "ask_for_info"),
        # 9. cancellation quote missing / stale
        (
            verified(
                active_intent="cancel_policy",
                slots={**VERIFIED_SLOTS, "effective_date": "2026-11-01", "reason": "cost"},
            ),
            "plan_tool",
        ),
        (
            verified(
                active_intent="cancel_policy",
                slots={**VERIFIED_SLOTS, "effective_date": "2026-11-05", "reason": "cost"},
                quote={"effective_date": "2026-11-01"},
            ),
            "plan_tool",
        ),
        # 10. ready
        (
            verified(
                active_intent="cancel_policy",
                slots={**VERIFIED_SLOTS, "effective_date": "2026-11-01", "reason": "cost"},
                quote={"effective_date": "2026-11-01"},
            ),
            "prepare_action",
        ),
        (verified(), "prepare_action"),
        # eligible claim, no duplicate → falls through
        (
            verified(
                claim_checks={
                    "incident_date": "2026-10-21",
                    "eligible": True,
                    "reason": None,
                    "duplicate_claim_id": None,
                }
            ),
            "prepare_action",
        ),
    ],
)
def test_route_slots(state: dict, expected: str) -> None:
    assert routing.route_slots(state) == expected


def test_route_slots_no_policy_data_before_verification() -> None:
    """Steps 6–7 (claim/duplicate data) can never fire before steps 4–5 pass (R18)."""
    state = verified(
        verified=False,
        claim_checks={
            "incident_date": "2026-10-15",
            "eligible": True,
            "reason": None,
            "duplicate_claim_id": "CLM-2026-000001",
        },
    )
    assert routing.route_slots(state) == "plan_tool"


# --- route_after_tool: every table row ---------------------------------------------------------


def tool(name: str, ok: bool = True, code: str | None = None, **data: Any) -> dict[str, Any]:
    return {
        "tool": name,
        "ok": ok,
        "data": data or None,
        "error": None
        if ok
        else {"code": code, "retryable": code in {"TIMEOUT", "SERVICE_UNAVAILABLE"}},
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"tool_result": tool("get_policy_status", False, "TIMEOUT")}, "handle_error"),
        (
            {"tool_result": tool("verify_policyholder", False, "SERVICE_UNAVAILABLE")},
            "handle_error",
        ),
        ({"tool_result": tool("get_cancellation_quote", False, "CONFLICT")}, "handle_error"),
        ({"tool_result": tool("get_policy_status", False, "INTERNAL")}, "handle_error"),
        (
            {
                "tool_result": tool("verify_policyholder", verified=False),
                "verified": False,
                "verification_attempts": 1,
            },
            "ask_for_info",
        ),
        (
            {
                "tool_result": tool("verify_policyholder", verified=False),
                "verified": False,
                "verification_attempts": 3,
            },
            "escalate",
        ),
        (
            {"tool_result": tool("verify_policyholder", verified=True), "verified": True},
            "validate_slots",
        ),
        ({"tool_result": tool("get_policy_status"), "active_intent": "policy_status"}, "respond"),
        (
            {
                "tool_result": tool("get_policy_status"),
                "active_intent": "cancel_policy",
                "policy_snapshot": {"status": "CANCELLED"},
            },
            "respond",
        ),
        (
            {
                "tool_result": tool("get_policy_status"),
                "active_intent": "cancel_policy",
                "policy_snapshot": {"status": "PENDING_CANCELLATION"},
            },
            "respond",
        ),
        (
            {
                "tool_result": tool("get_policy_status"),
                "active_intent": "cancel_policy",
                "policy_snapshot": {"status": "LAPSED"},
            },
            "validate_slots",
        ),
        (
            {
                "tool_result": tool("get_policy_status"),
                "active_intent": "report_claim",
                "policy_snapshot": {"status": "CANCELLED"},
            },
            "validate_slots",
        ),
        ({"tool_result": tool("get_policy_status", False, "NOT_FOUND")}, "handle_error"),
        ({"tool_result": tool("get_cancellation_quote", False, "NOT_FOUND")}, "handle_error"),
        ({"tool_result": tool("get_cancellation_quote")}, "prepare_action"),
        ({"tool_result": tool("get_cancellation_quote", False, "NOT_ELIGIBLE")}, "respond"),
        ({"tool_result": tool("create_claim")}, "handle_error"),
        ({}, "handle_error"),
    ],
)
def test_route_after_tool(state: dict, expected: str) -> None:
    assert routing.route_after_tool(state) == expected


# --- confirmation / approval / write / pending question / error / next task -------------------


@pytest.mark.parametrize(
    ("decision", "pending_action", "expected"),
    [
        ("handoff", {"requires_approval": True}, "escalate"),
        ("confirm_yes", {"requires_approval": True}, "announce_approval"),
        ("confirm_yes", {"requires_approval": False}, "execute_write_action"),
        ("edit", {}, "invalidate_on_edit"),
        ("other", {}, "ask_confirmation"),
        ("confirm_no", {}, "respond"),
        (None, {}, "respond"),
    ],
)
def test_route_confirm(decision: str | None, pending_action: dict, expected: str) -> None:
    state = st(annotations={"confirmation": {"decision": decision}}, pending_action=pending_action)
    assert routing.route_confirm(state) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [("approved", "execute_write_action"), ("rejected", "respond"), ("expired", "respond")],
)
def test_route_approval(status: str, expected: str) -> None:
    assert routing.route_approval({"approval": {"status": status}}) == expected
    assert routing.route_approval({}) == "respond"


def test_route_write() -> None:
    assert routing.route_write({"error": {"code": "TIMEOUT"}}) == "handle_error"
    assert routing.route_write({"error": None, "last_outcome": "CLAIM_CREATED"}) == "respond"


@pytest.mark.parametrize(
    ("answer", "kind", "last_outcome", "expected"),
    [
        ("handoff", "retry_write", None, "escalate"),
        ("handoff", "next_task", None, "escalate"),
        ("yes", "next_task", None, "validate_slots"),
        ("yes", "file_duplicate_claim", None, "validate_slots"),
        ("yes", "restart_expired", None, "validate_slots"),
        ("yes", "retry_write", None, "execute_write_action"),
        ("reask", "retry_write", None, "ask_yes_no"),
        ("no", "retry_write", "WRITE_WITHDRAWN", "respond"),
        ("no", "retry_write", "WRITE_OUTCOME_UNKNOWN", "escalate"),
        ("no", "next_task", None, "respond"),
        ("unrelated", "file_duplicate_claim", None, "understand"),
        ("unrelated", "next_task", None, "understand"),
    ],
)
def test_route_pending_question(
    answer: str, kind: str, last_outcome: str | None, expected: str
) -> None:
    state = st(
        annotations={"pending_answer": {"answer": answer, "kind": kind}},
        last_outcome=last_outcome,
    )
    assert routing.route_pending_question(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"error": {"retryable": True, "internal": False}}, "respond"),
        ({"error": {"retryable": False, "internal": True}}, "escalate"),
        (
            {
                "error": {"retryable": True, "internal": False},
                "last_outcome": "WRITE_OUTCOME_UNKNOWN",
            },
            "escalate",
        ),
        ({"error": {"retryable": False, "internal": False}}, "escalate"),
        ({"error": None}, "escalate"),
    ],
)
def test_route_error(state: dict, expected: str) -> None:
    assert routing.route_error(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            {
                "pending_question": {"kind": "restart_expired"},
                "last_outcome": "APPROVAL_EXPIRED",
                "suspended_tasks": [{"intent": "report_claim"}],
            },
            END,
        ),
        ({"last_outcome": "SMALLTALK", "suspended_tasks": [{"intent": "report_claim"}]}, END),
        ({"last_outcome": None, "intent_queue": [{"intent": "cancel_policy"}]}, END),
        (
            {"last_outcome": "STATUS_PROVIDED", "suspended_tasks": [{"intent": "report_claim"}]},
            "offer_next_task",
        ),
        (
            {"last_outcome": "CLAIM_CREATED", "intent_queue": [{"intent": "cancel_policy"}]},
            "offer_next_task",
        ),
        ({"last_outcome": "CLAIM_CREATED", "intent_queue": [], "suspended_tasks": []}, END),
    ],
)
def test_route_next_task(state: dict, expected: str) -> None:
    assert routing.route_next_task(state) == expected


def test_routing_functions_are_pure(set_env: Callable[..., None]) -> None:
    """Same state in → same answer out, no mutation."""
    state = verified(missing_slots=["injuries"])
    snapshot = repr(state)
    assert routing.route_slots(state) == routing.route_slots(state)
    assert repr(state) == snapshot
