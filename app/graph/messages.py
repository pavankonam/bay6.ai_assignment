"""Message helpers: timestamps (FR53b), customer-visible filtering (FR53) and per-turn annotations.

Per-turn interpretation (the `understand` result, the confirmation decision, the yes/no answer)
is stored on the turn's `HumanMessage.additional_kwargs`, so the `SupportState` schema stays
exactly as specified while routing remains a pure function of state (see DECISIONS.md).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage

from app.config import utcnow_iso

NLU = "nlu"
CONFIRMATION = "confirmation"
PENDING_ANSWER = "pending_answer"
ASKED_SLOTS = "asked_slots"


def ai(text: str, **meta: Any) -> AIMessage:
    """An agent message stamped with `created_at` (FR53b)."""
    return AIMessage(content=text, additional_kwargs={"created_at": utcnow_iso(), **meta})


def human(text: str, **meta: Any) -> HumanMessage:
    return HumanMessage(content=text, additional_kwargs={"created_at": utcnow_iso(), **meta})


def text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = [str(p.get("text", "")) if isinstance(p, Mapping) else str(p) for p in content]
    return "".join(parts)


def is_customer_visible(message: BaseMessage) -> bool:
    if isinstance(message, HumanMessage):
        return True
    return isinstance(message, AIMessage) and not message.tool_calls


def customer_visible(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    return [m for m in messages if is_customer_visible(m)]


def latest_human(state: Mapping[str, Any]) -> HumanMessage | None:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            return message
    return None


def latest_human_text(state: Mapping[str, Any]) -> str:
    message = latest_human(state)
    return text_of(message).strip() if message is not None else ""


def annotation(state: Mapping[str, Any], key: str) -> dict[str, Any]:
    """This turn's annotation `key` from the latest human message (empty if absent)."""
    message = latest_human(state)
    if message is None:
        return {}
    value = message.additional_kwargs.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def annotate(state: Mapping[str, Any], **values: Any) -> list[AnyMessage]:
    """Replace the latest human message (same id) with extra `additional_kwargs`."""
    message = latest_human(state)
    if message is None:
        return []
    kwargs = {**message.additional_kwargs, **values}
    return [HumanMessage(content=message.content, id=message.id, additional_kwargs=kwargs)]


def last_asked_slots(state: Mapping[str, Any]) -> list[str]:
    """Slots named by the most recent agent question (set by `ask_for_info`)."""
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and not message.tool_calls:
            asked = message.additional_kwargs.get(ASKED_SLOTS)
            return [str(s) for s in asked] if isinstance(asked, list) else []
    return []
