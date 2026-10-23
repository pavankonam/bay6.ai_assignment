"""`ingest`: start-of-turn bookkeeping (Section 8.3 node table)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import utcnow_iso
from app.graph.messages import annotate, latest_human
from app.graph.nodes.base import graph_node
from app.observability.metrics import metrics

_RESET_TO_IN_PROGRESS = {None, "completed", "escalated", "awaiting_info", "in_progress"}

DEFAULTS: dict[str, Any] = {
    "intent_queue": [],
    "suspended_tasks": [],
    "pending_question": None,
    "active_intent": None,
    "slots": {},
    "slots_skipped": [],
    "missing_slots": [],
    "invalid_slot_feedback": {},
    "clarification_attempts": {},
    "confirmation_reask_count": 0,
    "verified": False,
    "verified_policy_id": None,
    "verification_attempts": 0,
    "policy_snapshot": None,
    "quote": None,
    "claim_checks": None,
    "duplicate_override": False,
    "pending_action": None,
    "approval": None,
    "error": None,
    "resume_step": None,
    "llm_calls": 0,
    "phase": "collect",
}


@graph_node("ingest")
def ingest(state: Mapping[str, Any]) -> dict[str, Any]:
    """Increment the turn, stamp `created_at`, reset per-turn fields and normalize status.

    Does not clear `error` or `resume_step` (FR47).
    """
    turn = int(state.get("turn_count", 0)) + 1
    status = state.get("status")
    update: dict[str, Any] = {key: value for key, value in DEFAULTS.items() if key not in state}
    update.update(
        {
            "turn_count": turn,
            "tool_result": None,
            "response_facts": {},
            "last_outcome": None,
            "status": "in_progress" if status in _RESET_TO_IN_PROGRESS else status,
        }
    )
    message = latest_human(state)
    if message is not None and "created_at" not in message.additional_kwargs:
        update["messages"] = annotate(state, created_at=utcnow_iso())
    metrics.inc("turns_total")
    return update
