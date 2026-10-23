"""`handle_confirmation`: classify the reply to a summary (FR29, FR29b).

Regex only for bare replies; anything else goes to `ConfirmationResult`. An edit always wins over
a yes: LLM slot changes, or deterministic extractor values that differ from the summary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import today
from app.graph.messages import CONFIRMATION, annotate, latest_human_text
from app.graph.nodes.base import graph_node
from app.llm import build_history, structured_call
from app.nlu.redaction import redactor_for
from app.nlu.rules import bare_yes_no, is_handoff
from app.nlu.schemas import ConfirmationResult
from app.validation.extractors import extract_dates, extract_policy_ids
from app.validation.slots import TASK_DATE_SLOT


def deterministic_changes(
    text: str, payload: Mapping[str, Any], intent: str | None
) -> dict[str, Any]:
    """Slot values the extractors find in the message that differ from the summary."""
    changes: dict[str, Any] = {}
    policies = extract_policy_ids(text).values
    if policies and policies[0] != payload.get("policy_id"):
        changes["policy_id"] = policies[0]
    date_slot = TASK_DATE_SLOT.get(intent or "")
    if date_slot:
        dates = extract_dates(
            text, today=today(), asked_date_slots=[date_slot], relevant_roles={date_slot}
        )
        mention = dates.by_role().get(date_slot)
        value = mention.value.isoformat() if mention and mention.value else None
        if value is not None and value != payload.get(date_slot):
            changes[date_slot] = value
    return changes


@graph_node("handle_confirmation")
def handle_confirmation(state: Mapping[str, Any]) -> dict[str, Any]:
    text = latest_human_text(state)
    action = dict(state.get("pending_action") or {})
    payload = action.get("payload") or {}
    update: dict[str, Any] = {}
    changes: dict[str, Any] = {}
    if is_handoff(text):
        decision = "handoff"
    else:
        bare = bare_yes_no(text)
        if bare is not None:
            decision = "confirm_yes" if bare == "yes" else "confirm_no"
        else:
            llm_calls = int(state.get("llm_calls", 0))
            outcome = structured_call(
                ConfirmationResult,
                node="handle_confirmation",
                prompt="confirm_yes_no",
                context={"question_kind": "confirmation", "summary_fields": sorted(payload)},
                history=build_history(state.get("messages") or [], redactor_for(state)),
                llm_calls=llm_calls,
            )
            update["llm_calls"] = llm_calls + outcome.calls
            # FR48: without the LLM a non-bare message is "other", never "confirm_yes".
            result = outcome.value or ConfirmationResult(decision="other")
            decision = result.decision
            changes = {k: v for k, v in result.slot_changes.items() if k in payload}
            changes.update(deterministic_changes(text, payload, state.get("active_intent")))
            if changes and decision != "handoff":
                decision = "edit"  # edit wins over yes
    if decision == "other":
        if int(state.get("confirmation_reask_count", 0)) >= 1:
            decision = "confirm_no"  # a second "other" counts as no
        else:
            update["confirmation_reask_count"] = 1
    if decision == "confirm_yes":
        update["pending_action"] = {**action, "customer_confirmed": True}
        update["phase"] = "approve" if action.get("requires_approval") else "execute"
    elif decision == "confirm_no":
        update.update({"pending_action": None, "last_outcome": "DECLINED_BY_CUSTOMER"})
    elif decision == "handoff":
        update.update({"pending_action": None, "last_outcome": "ESCALATED"})
    update["messages"] = annotate(
        state, **{CONFIRMATION: {"decision": decision, "slot_changes": changes}}
    )
    return update
