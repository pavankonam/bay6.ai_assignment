"""`close_task`: FR5a reset for terminal outcomes; stash an expired task for restart (FR43)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.lifecycle import reset_task_state, task_only
from app.graph.nodes.base import graph_node


@graph_node("close_task")
def close_task(state: Mapping[str, Any]) -> dict[str, Any]:
    update = reset_task_state(state)
    if state.get("last_outcome") == "APPROVAL_EXPIRED" and state.get("active_intent"):
        slots = state.get("slots") or {}
        update["pending_question"] = {
            "kind": "restart_expired",
            "context": {
                "intent": state.get("active_intent"),
                "slots": task_only(slots),
                "policy_id": slots.get("policy_id"),
            },
        }
    return update
