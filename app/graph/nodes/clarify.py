"""`clarify`: empty messages, low confidence, ambiguous dates, and "which request?" (FR6a, FR7)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import get_settings
from app.graph.messages import ASKED_SLOTS, NLU, annotation, latest_human_text
from app.graph.nodes.base import graph_node
from app.graph.routing import INTENT_CLARIFY_KEY
from app.graph.speech import say
from app.responses import templates


@graph_node("clarify")
def clarify(state: Mapping[str, Any]) -> dict[str, Any]:
    update: dict[str, Any] = {"status": "awaiting_info"}
    nlu = annotation(state, NLU)
    meta: dict[str, Any] = {ASKED_SLOTS: []}
    if not latest_human_text(state):
        text = templates.empty_message()
    elif nlu.get("date_ambiguity"):
        ambiguity = nlu["date_ambiguity"]
        text = templates.date_ambiguity(ambiguity)
        role = ambiguity.get("role")
        meta[ASKED_SLOTS] = [role] if role else []
    elif float(nlu.get("confidence", 1.0)) < get_settings().INTENT_CONFIDENCE_THRESHOLD:
        attempts = dict(state.get("clarification_attempts") or {})
        attempts[INTENT_CLARIFY_KEY] = attempts.get(INTENT_CLARIFY_KEY, 0) + 1
        update["clarification_attempts"] = attempts
        # FR48: if the rule fallback cannot proceed, say the system is unavailable.
        text = (
            templates.system_unavailable()
            if nlu.get("fallback")
            else (templates.capabilities_question())
        )
    else:
        slots = state.get("slots") or {}
        text = templates.which_request(bool(slots.get("policy_id") or slots.get("dob")))
    update["messages"] = [say(state, text, **meta)]
    return update
