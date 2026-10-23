"""`validate_tool_call`: allowlist enforcement + argument overwrite (FR23)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, ToolCall

from app.graph.nodes.base import graph_node
from app.graph.phases import tool_args, tool_phase
from app.observability.logging import get_logger
from app.tools.allowlist import allowed_tool_names, default_tool_name

log = get_logger(__name__)


def _last_tool_call_message(state: Mapping[str, Any]) -> AIMessage:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and message.tool_calls:
            return message
    raise RuntimeError("validate_tool_call found no proposed tool call")


@graph_node("validate_tool_call")
def validate_tool_call(state: Mapping[str, Any]) -> dict[str, Any]:
    """Emit a replacement `AIMessage` (same id) holding exactly one sanitized tool call."""
    proposal = _last_tool_call_message(state)
    intent = state.get("active_intent")
    phase = tool_phase(state)
    allowed = allowed_tool_names(intent, phase)
    first = proposal.tool_calls[0]
    if first["name"] in allowed:
        name, call_id = first["name"], first.get("id") or "call_" + uuid.uuid4().hex
    else:
        default = default_tool_name(intent, phase)
        if default is None:
            raise RuntimeError("No default tool for the current phase")
        log.warning(
            "tool_call_rejected",
            proposed=first["name"],
            replaced_with=default,
            intent=intent,
            phase=phase,
        )
        name, call_id = default, "call_" + uuid.uuid4().hex
    for extra in proposal.tool_calls[1:]:
        log.warning("tool_call_rejected", proposed=extra["name"], reason="extra_call")
    sanitized: ToolCall = {"name": name, "args": tool_args(name, state), "id": call_id}
    replacement = AIMessage(
        content=proposal.content,
        id=proposal.id,
        tool_calls=[sanitized],
        additional_kwargs=dict(proposal.additional_kwargs),
    )
    return {"messages": [replacement]}
