"""`ask_yes_no`: the duplicate-claim question (FR33) or the `retry_write` re-ask (FR11a)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node
from app.graph.speech import say
from app.responses import templates


@graph_node("ask_yes_no")
def ask_yes_no(state: Mapping[str, Any]) -> dict[str, Any]:
    pending = state.get("pending_question")
    if pending and pending.get("kind") == "retry_write":
        return {
            "messages": [say(state, templates.retry_write_reask())],
            "status": "awaiting_info",
        }
    checks = state.get("claim_checks") or {}
    claim_id = str(checks.get("duplicate_claim_id"))
    incident = str(checks.get("incident_date"))
    return {
        "messages": [say(state, templates.duplicate_claim_question(claim_id, incident))],
        "pending_question": {
            "kind": "file_duplicate_claim",
            "context": {"claim_id": claim_id, "incident_date": incident},
        },
        "status": "awaiting_info",
    }
