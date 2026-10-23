"""`prepare_action`: build a fresh `pending_action` (FR27, FR32, FR36)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from app.config import get_settings
from app.graph.nodes.base import graph_node, thread_id_of
from app.graph.state import PendingAction
from app.tools.idempotency import business_payload, idempotency_key

ACTION_FOR_INTENT: dict[str, Literal["create_claim", "cancel_policy"]] = {
    "report_claim": "create_claim",
    "cancel_policy": "cancel_policy",
}


def claim_approval_reasons(slots: Mapping[str, Any]) -> list[str]:
    """FR32: injuries, or estimate ≥ threshold, or estimate skipped/unknown."""
    reasons: list[str] = []
    if slots.get("injuries") is True:
        reasons.append("injuries_reported")
    estimate = slots.get("estimated_damage_usd")
    if estimate is None:
        reasons.append("damage_estimate_unknown")
    elif float(estimate) >= get_settings().CLAIM_APPROVAL_THRESHOLD_USD:
        reasons.append("high_damage_estimate")
    return reasons


@graph_node("prepare_action")
def prepare_action(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    intent = str(state.get("active_intent"))
    action_type = ACTION_FOR_INTENT[intent]
    slots = {**(state.get("slots") or {}), "policy_id": state.get("verified_policy_id")}
    payload = business_payload(action_type, slots)
    pending_action_id = str(uuid.uuid4())
    if action_type == "create_claim":
        reasons = claim_approval_reasons(slots)
    else:
        reasons = ["cancellation_requires_review"]  # FR36: every cancellation, no exceptions
    action: PendingAction = {
        "pending_action_id": pending_action_id,
        "action_type": action_type,
        "payload": payload,
        "idempotency_key": idempotency_key(
            thread_id_of(config), pending_action_id, action_type, payload
        ),
        "requires_approval": bool(reasons),
        "approval_reasons": reasons,
        "allow_duplicate": bool(state.get("duplicate_override")),
        "customer_confirmed": False,
        "approval_id": None,
    }
    return {"pending_action": action, "phase": "confirm"}
