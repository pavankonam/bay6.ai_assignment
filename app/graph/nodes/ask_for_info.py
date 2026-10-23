"""`ask_for_info`: ask for up to 2 missing slots in priority order (FR14), with feedback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from app.graph.messages import ASKED_SLOTS, NLU, annotation
from app.graph.nodes.base import graph_node
from app.graph.speech import say
from app.responses import templates
from app.validation.slots import IDENTITY_SLOTS, all_specs

MAX_SLOTS_PER_QUESTION = 2


def verified_this_turn(state: Mapping[str, Any]) -> bool:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            return False
        if (
            isinstance(message, ToolMessage)
            and message.name == "verify_policyholder"
            and isinstance(message.artifact, Mapping)
            and (message.artifact.get("data") or {}).get("verified")
        ):
            return True
    return False


def policy_lock_reported(state: Mapping[str, Any]) -> bool:
    result = state.get("tool_result") or {}
    return result.get("tool") == "verify_policyholder" and bool(
        (result.get("data") or {}).get("locked")
    )


def slots_to_ask(state: Mapping[str, Any]) -> list[str]:
    missing = list(state.get("missing_slots") or [])
    if any(s in IDENTITY_SLOTS for s in missing):
        return [s for s in IDENTITY_SLOTS if s in missing]  # identity is asked together
    orders = {spec.name: spec.ask_order for spec in all_specs(state.get("active_intent"))}
    if not missing:
        return []
    first = min(orders.get(s, 99) for s in missing)
    return [s for s in missing if orders.get(s, 99) == first][:MAX_SLOTS_PER_QUESTION]


@graph_node("ask_for_info")
def ask_for_info(state: Mapping[str, Any]) -> dict[str, Any]:
    asked = slots_to_ask(state)
    feedback = dict(state.get("invalid_slot_feedback") or {})
    slots = state.get("slots") or {}
    parts: list[str] = []
    intent = state.get("active_intent")
    if annotation(state, NLU).get("primary") == intent and intent in templates.INTENT_LEAD_INS:
        parts.append(templates.INTENT_LEAD_INS[intent])
    shown: list[str] = []
    for slot in asked:
        message = feedback.pop(slot, None)
        if message and message not in shown:
            shown.append(message)
    parts.extend(shown)
    if verified_this_turn(state):
        parts.append("Thanks, you're verified.")
    if policy_lock_reported(state):
        parts.append(templates.policy_locked())
    if (
        asked == ["dob"]
        and slots.get("policy_id")
        and state.get("verified_policy_id")
        not in (
            None,
            slots.get("policy_id"),
        )
    ):
        question = templates.confirm_dob_for(str(slots["policy_id"]))
    else:
        question = templates.question_for(asked)
    parts.append(question)
    attempts = dict(state.get("clarification_attempts") or {})
    for slot in asked:
        attempts[slot] = attempts.get(slot, 0) + 1
    return {
        "messages": [say(state, " ".join(parts), **{ASKED_SLOTS: asked, "question": question})],
        "clarification_attempts": attempts,
        "invalid_slot_feedback": feedback,
        "status": "awaiting_info",
    }
