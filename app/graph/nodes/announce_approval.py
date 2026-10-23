"""`announce_approval`: all HITL side effects happen here, before the interrupt (FR37, FR38)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.approvals.service import approvals_service
from app.graph.messages import ASKED_SLOTS
from app.graph.nodes.base import graph_node, thread_id_of
from app.graph.speech import say
from app.responses import templates


@graph_node("announce_approval")
def announce_approval(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    action = dict(state.get("pending_action") or {})
    approval = approvals_service.get_or_create(
        thread_id=thread_id_of(config), action=action, quote=state.get("quote")
    )
    return {
        "messages": [
            say(state, templates.pending_review(approval.approval_id), **{ASKED_SLOTS: []})
        ],
        "pending_action": {**action, "approval_id": approval.approval_id},
        "approval": {
            "approval_id": approval.approval_id,
            "status": "pending",
            "reviewer_id": None,
            "comment": None,
            "decided_at": None,
        },
        "status": "awaiting_approval",
        "phase": "approve",
    }
