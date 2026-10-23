"""`handle_error`: classify a tool/write failure (FR47, FR47a, FR47b; Section 9.3)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node
from app.observability.logging import get_logger
from app.tools import errors

log = get_logger(__name__)


@graph_node("handle_error")
def handle_error(state: Mapping[str, Any]) -> dict[str, Any]:
    error = dict(state.get("error") or {})
    code = str(error.get("code") or errors.INTERNAL)
    if not error.get("retryable"):
        # FR47b: internal / non-retryable → escalate (never "failed_retryable").
        if code == errors.UNAUTHORIZED_WRITE:
            log.critical("internal_error", error_code=code, node=error.get("node"))
        else:
            log.error("internal_error", error_code=code, node=error.get("node"))
        error.update({"internal": True, "retryable": False})
        return {"error": error, "last_outcome": "ESCALATED", "resume_step": None}
    update: dict[str, Any] = {"status": "failed_retryable", "last_outcome": "SYSTEM_UNAVAILABLE"}
    action = state.get("pending_action") or {}
    if state.get("resume_step") == "execute_write":
        outcome_unknown = code == "WRITE_OUTCOME_UNKNOWN"
        update["pending_question"] = {
            "kind": "retry_write",
            "context": {"reask_count": 0, "outcome_unknown": outcome_unknown},
        }
        update["response_facts"] = {
            "outcome": "SYSTEM_UNAVAILABLE",
            "template": "write_outcome_unknown_offer"
            if outcome_unknown
            else "approved_write_failed",
            "approved": bool(action.get("requires_approval")),
        }
        log.warning("write_failed", error_code=code, idempotency_key=action.get("idempotency_key"))
    else:
        update["resume_step"] = "read_tool"
        update["response_facts"] = {"outcome": "SYSTEM_UNAVAILABLE"}
        log.warning("read_failed", error_code=code, node=error.get("node"))
    return update
