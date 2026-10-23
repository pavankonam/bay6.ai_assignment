"""Building customer-visible agent messages: greeting on the first turn only (FR5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

from app.graph.messages import ai
from app.responses import templates


def _prior_agent_replies(state: Mapping[str, Any]) -> bool:
    return any(isinstance(m, AIMessage) and not m.tool_calls for m in state.get("messages") or [])


def say(state: Mapping[str, Any], text: str, **meta: Any) -> AIMessage:
    """An agent reply. The thread's first reply is prefixed with the greeting (FR5)."""
    parts = []
    if state.get("turn_count", 1) == 1 and not _prior_agent_replies(state):
        parts.append(templates.greeting())
    parts.append(text)
    return ai(" ".join(p for p in parts if p), **meta)
