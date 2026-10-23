"""`human_approval`: `interrupt()` only. Code before the interrupt re-runs on resume, so it only
reads (R7); the pending-review message was emitted by `announce_approval`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.types import interrupt

from app.approvals.service import DECISION_TO_STATUS, approvals_service
from app.graph.nodes.base import graph_node
from app.graph.state import ApprovalInfo
from app.observability.metrics import metrics

REJECTED_OUTCOME = {"create_claim": "CLAIM_REJECTED", "cancel_policy": "CANCEL_REJECTED"}


def interrupt_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    action = state.get("pending_action") or {}
    approval_id = str(action.get("approval_id"))
    approval = approvals_service.get(approval_id)
    return {
        "approval_id": approval_id,
        "action_type": action.get("action_type"),
        "customer": approval.customer if approval else {},
        "payload": action.get("payload"),
        "risk_flags": action.get("approval_reasons"),
        "summary": approval.summary if approval else "",
    }


@graph_node("human_approval")
def human_approval(state: Mapping[str, Any]) -> dict[str, Any]:
    action = state.get("pending_action") or {}
    decision = interrupt(interrupt_payload(state))
    # Resumed with {"decision": "approve"|"reject"|"expired", "reviewer_id", "comment", ...}
    status = DECISION_TO_STATUS[str(decision.get("decision"))]
    approval: ApprovalInfo = {
        "approval_id": str(action.get("approval_id")),
        "status": status,  # type: ignore[typeddict-item]
        "reviewer_id": decision.get("reviewer_id"),
        "comment": decision.get("comment"),
        "decided_at": decision.get("decided_at"),
    }
    action_type = str(action.get("action_type"))
    metrics.inc("approvals_total", action=action_type, decision=status)
    update: dict[str, Any] = {"approval": approval, "status": "in_progress"}
    if status == "rejected":
        update["last_outcome"] = REJECTED_OUTCOME[action_type]
    elif status == "expired":
        update["last_outcome"] = "APPROVAL_EXPIRED"
    else:
        update["phase"] = "execute"
    return update
