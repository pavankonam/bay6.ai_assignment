"""`process_tool_result`: interpret the read-tool `ToolMessage` (FR18–FR21, FR30, FR34, FR47)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolMessage

from app.graph.nodes.base import graph_node
from app.graph.routing import INACTIVE_POLICY_STATUSES
from app.graph.state import ErrorInfo
from app.observability.logging import get_logger
from app.responses.templates import VERIFICATION_FAILED
from app.tools import errors
from app.tools.errors import RETRYABLE_CODES

log = get_logger(__name__)

# FR47b: non-retryable internal errors (escalate rather than "failed_retryable").
INTERNAL_CODES = frozenset(
    {errors.CONFLICT, errors.APPROVAL_REQUIRED, errors.UNAUTHORIZED_WRITE, errors.NOT_FOUND,
     errors.INTERNAL}
)  # fmt: skip


def error_info(code: str, message: str, *, node: str, attempts: int) -> ErrorInfo:
    retryable = code in RETRYABLE_CODES
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "internal": not retryable and code not in errors.BUSINESS_CODES,
        "node": node,
        "attempts": attempts,
    }


def _last_tool_message(state: Mapping[str, Any]) -> ToolMessage | None:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, ToolMessage):
            return message
    return None


def _envelope(message: ToolMessage | None) -> tuple[str, dict[str, Any]]:
    if message is None:
        return "unknown", {
            "ok": False,
            "data": None,
            "error": {"code": errors.INTERNAL, "message": "No tool output", "attempts": 0},
        }
    artifact = message.artifact
    if isinstance(artifact, Mapping) and "ok" in artifact:
        return str(message.name), dict(artifact)
    # ToolNode reported an error itself (e.g. invalid arguments): an internal failure.
    return str(message.name), {
        "ok": False,
        "data": None,
        "error": {"code": errors.INTERNAL, "message": "Tool execution error", "attempts": 1},
    }


@graph_node("process_tool_result")
def process_tool_result(state: Mapping[str, Any]) -> dict[str, Any]:
    tool, envelope = _envelope(_last_tool_message(state))
    result = {"tool": tool, **envelope}
    update: dict[str, Any] = {"tool_result": result}
    if not envelope.get("ok"):
        err = envelope.get("error") or {}
        code = str(err.get("code") or errors.INTERNAL)
        if tool == "get_cancellation_quote" and code == errors.NOT_ELIGIBLE:
            update["last_outcome"] = "NOT_ELIGIBLE_CANCEL"
            update["response_facts"] = {"outcome": "NOT_ELIGIBLE_CANCEL", "source": "quote"}
            return update
        info = error_info(
            code, str(err.get("message", "")), node=tool, attempts=int(err.get("attempts") or 1)
        )
        update["error"] = info
        if info["retryable"]:
            update["resume_step"] = "read_tool"
        if code == errors.NOT_FOUND:
            log.error("data_inconsistency", tool=tool, error_code=code)
        return update
    data = envelope.get("data") or {}
    slots = dict(state.get("slots") or {})
    # Successful call: any earlier read failure is resolved (FR47).
    update.update({"error": None, "resume_step": None, "status": "in_progress"})
    if tool == "verify_policyholder":
        if data.get("verified"):
            feedback = {
                k: v
                for k, v in (state.get("invalid_slot_feedback") or {}).items()
                if k not in ("policy_id", "dob")
            }
            update.update(
                {
                    "verified": True,
                    "verified_policy_id": slots.get("policy_id"),
                    "invalid_slot_feedback": feedback,
                    "phase": "lookup",
                }
            )
        else:
            # FR20a: clear identity so new values are accepted, generic feedback (FR20).
            slots.pop("policy_id", None)
            slots.pop("dob", None)
            feedback = dict(state.get("invalid_slot_feedback") or {})
            feedback.update({"policy_id": VERIFICATION_FAILED, "dob": VERIFICATION_FAILED})
            update.update(
                {
                    "verified": False,
                    "verified_policy_id": None,
                    "slots": slots,
                    "invalid_slot_feedback": feedback,
                    "verification_attempts": int(state.get("verification_attempts", 0)) + 1,
                    "policy_snapshot": None,
                    "quote": None,
                    "claim_checks": None,
                    "missing_slots": ["policy_id", "dob"],
                    "phase": "collect",
                }
            )
        return update
    if tool == "get_policy_status":
        update["policy_snapshot"] = data
        intent = state.get("active_intent")
        if intent == "policy_status":
            update["last_outcome"] = "STATUS_PROVIDED"
            update["response_facts"] = {"outcome": "STATUS_PROVIDED", "policy": data}
        elif intent == "cancel_policy" and data.get("status") in INACTIVE_POLICY_STATUSES:
            update["last_outcome"] = "NOT_ELIGIBLE_CANCEL"  # FR34: checked right after lookup
            update["response_facts"] = {"outcome": "NOT_ELIGIBLE_CANCEL", "policy": data}
        return update
    if tool == "get_cancellation_quote":
        update["quote"] = data
        update["phase"] = "confirm"
    return update
