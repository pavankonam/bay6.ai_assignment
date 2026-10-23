"""`ask_confirmation`: show the full payload summary and wait for yes / no / edit (FR28)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.messages import ASKED_SLOTS, CONFIRMATION, annotation
from app.graph.nodes.base import graph_node
from app.graph.speech import say
from app.responses import templates
from app.rules import has_collision_coverage


def summary_text(state: Mapping[str, Any]) -> str:
    action = state.get("pending_action") or {}
    payload = action.get("payload") or {}
    snapshot = state.get("policy_snapshot") or {}
    feedback = list(dict.fromkeys((state.get("invalid_slot_feedback") or {}).values()))
    if action.get("action_type") == "create_claim":
        reasons = list(action.get("approval_reasons") or [])
        checks = state.get("claim_checks") or {}
        return templates.claim_summary(
            payload,
            snapshot,
            approval_reasons=reasons,
            no_collision=not has_collision_coverage(snapshot.get("coverages") or []),
            duplicate_of=checks.get("duplicate_claim_id")
            if action.get("allow_duplicate")
            else None,
            feedback=feedback,
        )
    return templates.cancellation_summary(payload, state.get("quote") or {}, snapshot, feedback)


@graph_node("ask_confirmation")
def ask_confirmation(state: Mapping[str, Any]) -> dict[str, Any]:
    action = state.get("pending_action") or {}
    reask = annotation(state, CONFIRMATION).get("decision") == "other"
    if reask:
        text = templates.confirmation_reask(str(action.get("action_type")))
        update: dict[str, Any] = {}
    else:
        text = summary_text(state)
        update = {"confirmation_reask_count": 0, "invalid_slot_feedback": {}}
    update.update(
        {
            "messages": [say(state, text, **{ASKED_SLOTS: []})],
            "status": "awaiting_confirmation",
            "phase": "confirm",
        }
    )
    return update
