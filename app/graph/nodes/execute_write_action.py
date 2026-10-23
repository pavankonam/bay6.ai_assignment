"""`execute_write_action`: the only path to a write tool (FR25, FR26, FR47, FR47a)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from app.config import utcnow_iso
from app.graph.nodes.base import graph_node
from app.graph.nodes.process_tool_result import error_info
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.tools import errors
from app.tools.definitions import (
    CancelPolicyArgs,
    CreateClaimArgs,
    cancel_policy_call,
    create_claim_call,
    lookup_write_result,
)
from app.tools.errors import ToolResult, UnauthorizedWriteError
from app.validation.slots import task_specs
from app.validation.validators import validate_slot

log = get_logger(__name__)
SUCCESS_OUTCOME = {"create_claim": "CLAIM_CREATED", "cancel_policy": "CANCEL_COMPLETED"}


def check_write_guard(state: Mapping[str, Any]) -> None:
    """FR26: raise `UnauthorizedWriteError` unless every precondition holds."""
    action = state.get("pending_action")
    if not action:
        raise UnauthorizedWriteError("no pending action")
    if not state.get("verified"):
        raise UnauthorizedWriteError("identity not verified")
    payload = action.get("payload") or {}
    if payload.get("policy_id") != state.get("verified_policy_id"):
        raise UnauthorizedWriteError("payload policy does not match the verified policy")
    intent = "report_claim" if action.get("action_type") == "create_claim" else "cancel_policy"
    for spec in task_specs(intent):
        value = payload.get(spec.name)
        if value is None:
            if spec.required:
                raise UnauthorizedWriteError(f"required slot {spec.name} missing")
            continue
        if not validate_slot(spec.name, value).ok:
            raise UnauthorizedWriteError(f"slot {spec.name} is invalid")
    if not action.get("customer_confirmed"):
        raise UnauthorizedWriteError("customer has not confirmed")
    if action.get("requires_approval"):
        approval = state.get("approval") or {}
        if approval.get("status") != "approved":
            raise UnauthorizedWriteError("approval required but not approved")
        if not action.get("approval_id") or approval.get("approval_id") != action.get(
            "approval_id"
        ):
            raise UnauthorizedWriteError("approval id does not match the pending action")


def _call(action: Mapping[str, Any]) -> ToolResult:
    payload = dict(action.get("payload") or {})
    if action.get("action_type") == "create_claim":
        return create_claim_call(
            CreateClaimArgs(
                **payload,
                allow_duplicate=bool(action.get("allow_duplicate")),
                idempotency_key=str(action.get("idempotency_key")),
                approval_id=action.get("approval_id"),
            )
        )
    return cancel_policy_call(
        CancelPolicyArgs(
            **payload,
            idempotency_key=str(action.get("idempotency_key")),
            approval_id=str(action.get("approval_id") or ""),
        )
    )


def audit_messages(action: Mapping[str, Any], result: ToolResult) -> list[Any]:
    """Record the code-invoked write as a tool call + result (never shown to customers)."""
    call_id = "call_" + uuid.uuid4().hex
    name = str(action.get("action_type"))
    args = {
        **(action.get("payload") or {}),
        "idempotency_key": action.get("idempotency_key"),
        "approval_id": action.get("approval_id"),
    }
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id}],
            additional_kwargs={"created_at": utcnow_iso()},
        ),
        ToolMessage(content=str(result), tool_call_id=call_id, name=name, artifact=dict(result)),
    ]


def resolve_unknown_outcome(action: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """FR47a: check the backend by idempotency key. Returns ('found', data) / ('absent', None)
    / ('unknown', None)."""
    policy_id = (action.get("payload") or {}).get("policy_id")
    lookup = lookup_write_result(str(action.get("idempotency_key")), policy_id)
    if not lookup["ok"]:
        metrics.inc("write_outcome_lookups_total", result="unknown")
        return "unknown", None
    data = lookup["data"] or {}
    if data.get("found"):
        metrics.inc("write_outcome_lookups_total", result="found")
        return "found", dict(data.get("data") or {})
    metrics.inc("write_outcome_lookups_total", result="absent")
    return "absent", None


def success_update(action: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("action_type"))
    return {
        "last_outcome": SUCCESS_OUTCOME[action_type],
        "response_facts": {"outcome": SUCCESS_OUTCOME[action_type], "result": dict(data)},
        "error": None,
        "resume_step": None,
        "status": "in_progress",
        "phase": "done",
    }


@graph_node("execute_write_action")
def execute_write_action(state: Mapping[str, Any]) -> dict[str, Any]:
    action = state.get("pending_action") or {}
    try:
        check_write_guard(state)
    except UnauthorizedWriteError as exc:
        metrics.inc("unauthorized_write_blocked_total")
        log.critical("unauthorized_write_blocked", reason=str(exc))
        return {
            "error": error_info(
                errors.UNAUTHORIZED_WRITE, str(exc), node="execute_write_action", attempts=0
            ),
            "last_outcome": None,
        }
    result = _call(action)
    update: dict[str, Any] = {
        "tool_result": {"tool": action.get("action_type"), **result},
        "messages": audit_messages(action, result),
        "phase": "execute",
    }
    if result["ok"]:
        update.update(success_update(action, result["data"] or {}))
        return update
    err = result["error"]
    code = err["code"] if err else errors.INTERNAL
    message = err["message"] if err else ""
    attempts = err["attempts"] if err else 1
    if code == errors.NOT_ELIGIBLE:
        update.update(
            {
                "last_outcome": "WRITE_NOT_ELIGIBLE",
                "error": None,
                "resume_step": None,
                "response_facts": {"outcome": "WRITE_NOT_ELIGIBLE"},
            }
        )
        return update
    if code == errors.DUPLICATE_CLAIM:
        existing = (result.get("data") or {}).get("existing_claim_id")
        update.update(
            {
                "last_outcome": "CLAIM_NOT_FILED_DUPLICATE",
                "error": None,
                "response_facts": {
                    "outcome": "CLAIM_NOT_FILED_DUPLICATE",
                    "existing_claim_id": existing,
                    "at_write": True,
                },
            }
        )
        return update
    info = error_info(code, message, node="execute_write_action", attempts=attempts)
    if info["retryable"]:
        found, data = resolve_unknown_outcome(action)
        if found == "found":
            assert data is not None
            update.update(success_update(action, data))  # committed despite the timeout
            return update
        update["resume_step"] = "execute_write"
        if found == "unknown":
            info = {**info, "code": "WRITE_OUTCOME_UNKNOWN"}
    else:
        log.error("write_internal_error", error_code=code)
    update["error"] = info
    update["last_outcome"] = None
    return update
