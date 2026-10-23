"""`plan_tool`: the LLM proposes the next read-tool call; code decides what may run (FR23, FR24)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

from app.config import get_settings, utcnow_iso
from app.graph.nodes.base import graph_node
from app.graph.phases import tool_phase
from app.llm import build_history, tool_call
from app.nlu.redaction import redactor_for
from app.tools.allowlist import bindable_tools, default_tool_name


def synthesize_tool_call(tool_name: str) -> AIMessage:
    """FR24: the deterministic `AIMessage` so `ToolNode` works the same in both modes."""
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {}, "id": "call_" + uuid.uuid4().hex}],
        additional_kwargs={"created_at": utcnow_iso()},
    )


@graph_node("plan_tool")
def plan_tool(state: Mapping[str, Any]) -> dict[str, Any]:
    intent = state.get("active_intent")
    phase = tool_phase(state)
    default = default_tool_name(intent, phase)
    if default is None:
        raise RuntimeError(
            f"plan_tool reached with no allowed tool (intent={intent}, phase={phase})"
        )
    update: dict[str, Any] = {"status": "in_progress"}
    if get_settings().TOOL_SELECTION_MODE == "deterministic":
        update["messages"] = [synthesize_tool_call(default)]
        return update
    slots = state.get("slots") or {}
    context = {
        "active_intent": intent,
        "phase": phase,
        "policy_id": slots.get("policy_id"),
        "effective_date": slots.get("effective_date"),
    }
    llm_calls = int(state.get("llm_calls", 0))
    outcome = tool_call(
        bindable_tools(intent, phase),
        node="plan_tool",
        prompt="plan_tool",
        context=context,
        history=build_history(state.get("messages") or [], redactor_for(state)),
        llm_calls=llm_calls,
    )
    proposed = outcome.value
    if proposed is None:
        message = synthesize_tool_call(default)
    else:
        message = AIMessage(
            content=proposed.content,
            tool_calls=proposed.tool_calls,
            id=proposed.id,
            additional_kwargs={"created_at": utcnow_iso()},
        )
    update["messages"] = [message]
    update["llm_calls"] = llm_calls + outcome.calls
    return update
