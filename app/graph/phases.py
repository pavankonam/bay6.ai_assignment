"""Phase helpers shared by `validate_slots`, `plan_tool` and `validate_tool_call`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.state import Phase


def tool_phase(state: Mapping[str, Any]) -> Phase | None:
    """Which read-tool phase the state is in: verify → lookup → quote (None if no read needed)."""
    slots = state.get("slots") or {}
    if not state.get("verified") or state.get("verified_policy_id") != slots.get("policy_id"):
        return "verify"
    snapshot = state.get("policy_snapshot") or {}
    intent = state.get("active_intent")
    if intent == "policy_status" or snapshot.get("policy_id") != state.get("verified_policy_id"):
        return "lookup"
    if intent == "cancel_policy":
        return "quote"
    return None


def tool_args(tool_name: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments for a read tool, taken only from validated slots / verified state (FR23, NFR7)."""
    slots = state.get("slots") or {}
    if tool_name == "verify_policyholder":
        return {"policy_id": slots.get("policy_id"), "dob": slots.get("dob")}
    if tool_name == "get_policy_status":
        return {"policy_id": state.get("verified_policy_id")}
    if tool_name == "get_cancellation_quote":
        return {
            "policy_id": state.get("verified_policy_id"),
            "effective_date": slots.get("effective_date"),
        }
    raise ValueError(f"No argument mapping for tool {tool_name!r}")
