"""`escalate`: escalation message chosen by reason; `status = escalated | locked` (FR4, FR9,
FR15, FR19, FR47a, FR47b); applies the FR5a reset for terminal outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import get_settings
from app.graph.lifecycle import reset_task_state
from app.graph.messages import CONFIRMATION, NLU, PENDING_ANSWER, annotation
from app.graph.nodes.base import graph_node
from app.graph.speech import say
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.responses import templates

log = get_logger(__name__)


def escalation_reason(state: Mapping[str, Any]) -> str:
    settings = get_settings()
    if (
        state.get("status") == "locked"
        or int(state.get("verification_attempts", 0)) >= settings.MAX_VERIFICATION_ATTEMPTS
    ):
        return "locked"
    if int(state.get("turn_count", 0)) > settings.MAX_TURNS:
        return "turn_limit"
    if state.get("last_outcome") == "WRITE_OUTCOME_UNKNOWN":
        return "write_outcome_unknown"
    if (
        annotation(state, NLU).get("primary") == "human_handoff"
        or annotation(state, CONFIRMATION).get("decision") == "handoff"
        or annotation(state, PENDING_ANSWER).get("answer") == "handoff"
    ):
        return "handoff"
    error = state.get("error") or {}
    if error and not error.get("retryable"):
        return "internal_error"
    return "clarification"


@graph_node("escalate")
def escalate(state: Mapping[str, Any]) -> dict[str, Any]:
    reason = escalation_reason(state)
    outcome = (
        "WRITE_OUTCOME_UNKNOWN"
        if state.get("last_outcome") == "WRITE_OUTCOME_UNKNOWN"
        else "ESCALATED"
    )
    metrics.inc("escalations_total", reason=reason)
    log.warning("escalated", reason=reason, last_outcome=outcome)
    update: dict[str, Any] = {
        "messages": [say(state, templates.escalation(reason))],
        "last_outcome": outcome,
        "status": "locked" if reason == "locked" else "escalated",
        "pending_question": None,
    }
    update.update(reset_task_state({**state, "last_outcome": outcome}))
    return update
