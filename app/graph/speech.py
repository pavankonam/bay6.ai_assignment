"""Building customer-visible agent messages: greeting on the first turn only (FR5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.messages import NLU, ai, annotation
from app.responses import templates


def _replies_this_turn(state: Mapping[str, Any]) -> int:
    count = 0
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and not message.tool_calls:
            count += 1
    return count


def _prior_agent_replies(state: Mapping[str, Any]) -> bool:
    return any(isinstance(m, AIMessage) and not m.tool_calls for m in state.get("messages") or [])


def say(state: Mapping[str, Any], text: str, **meta: Any) -> AIMessage:
    """An agent reply. The thread's first reply is prefixed with the greeting (FR5)."""
    parts = []
    if state.get("turn_count", 1) == 1 and not _prior_agent_replies(state):
        parts.append(templates.greeting())
    parts.append(text)
    if _replies_this_turn(state) == 0 and annotation(state, NLU).get("other_intents"):
        parts.append(templates.other_request_later())
    return ai(" ".join(p for p in parts if p), **meta)
