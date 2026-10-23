"""Every conditional edge function (Section 8.3). Each is a pure function of state: no I/O, no LLM.

Settings (thresholds/limits) are configuration, not I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from langgraph.graph import END

from app.config import get_settings
from app.graph.messages import CONFIRMATION, NLU, PENDING_ANSWER, annotation, latest_human_text
from app.graph.state import TERMINAL_OUTCOMES
from app.tools import errors

IDENTITY_SLOTS = ("policy_id", "dob")
INACTIVE_POLICY_STATUSES = frozenset({"CANCELLED", "PENDING_CANCELLATION"})
INTENT_CLARIFY_KEY = "_intent"

State = Mapping[str, Any]


def route_entry(
    state: State,
) -> Literal["escalate", "clarify", "handle_confirmation", "handle_pending_question", "understand"]:
    if state.get("status") == "locked" or state.get("turn_count", 0) > get_settings().MAX_TURNS:
        return "escalate"
    if not latest_human_text(state):
        return "clarify"
    if state.get("status") == "awaiting_confirmation":
        return "handle_confirmation"
    if state.get("pending_question"):
        return "handle_pending_question"
    return "understand"


def route_intent(state: State) -> Literal["escalate", "respond", "clarify", "validate_slots"]:
    """Precedence: handoff → out_of_scope/smalltalk → low confidence or ambiguous dates →
    read-retry → supported intent / provide_info (the last two both enter `validate_slots`)."""
    nlu = annotation(state, NLU)
    primary = nlu.get("primary")
    if primary == "human_handoff":
        return "escalate"
    if primary in ("out_of_scope", "smalltalk"):
        return "respond"
    low_confidence = float(nlu.get("confidence", 0.0)) < get_settings().INTENT_CONFIDENCE_THRESHOLD
    if nlu.get("date_ambiguity") or low_confidence:
        attempts = (state.get("clarification_attempts") or {}).get(INTENT_CLARIFY_KEY, 0)
        if attempts >= get_settings().MAX_CLARIFICATION_ATTEMPTS:
            return "escalate"
        return "clarify"
    return "validate_slots"


def route_slots(
    state: State,
) -> Literal[
    "escalate", "clarify", "ask_for_info", "plan_tool", "respond", "ask_yes_no", "prepare_action"
]:
    """`route_slots` precedence (Section 8.3), first match wins."""
    slots = state.get("slots") or {}
    missing = state.get("missing_slots") or []
    attempts = state.get("clarification_attempts") or {}
    max_attempts = get_settings().MAX_CLARIFICATION_ATTEMPTS
    # 1. Max clarification attempts exceeded for a slot that is still missing.
    if any(attempts.get(slot, 0) >= max_attempts for slot in missing):
        return "escalate"
    # 2. No active intent: identity retained, no tool calls (FR6a).
    intent = state.get("active_intent")
    if not intent:
        return "clarify"
    # 3. Identity missing.
    if any(not slots.get(slot) for slot in IDENTITY_SLOTS):
        return "ask_for_info"
    # 4. Not verified for this policy.
    if not state.get("verified") or state.get("verified_policy_id") != slots.get("policy_id"):
        return "plan_tool"
    # 5. No snapshot for the verified policy (a status request always looks up fresh data).
    snapshot = state.get("policy_snapshot") or {}
    if intent == "policy_status" or snapshot.get("policy_id") != state.get("verified_policy_id"):
        return "plan_tool"
    checks = state.get("claim_checks")
    if intent == "report_claim" and checks:
        # 6. Claim ineligible (FR31).
        if not checks.get("eligible"):
            return "respond"
        # 7. Duplicate claim without override (FR33).
        if checks.get("duplicate_claim_id") and not state.get("duplicate_override"):
            return "ask_yes_no"
    # 8. Required or not-yet-asked optional task slots missing.
    if any(slot not in IDENTITY_SLOTS for slot in missing):
        return "ask_for_info"
    # 9. Cancellation needs a quote for the current effective date.
    if intent == "cancel_policy":
        quote = state.get("quote") or {}
        if quote.get("effective_date") != slots.get("effective_date"):
            return "plan_tool"
    # 10.
    return "prepare_action"


def route_after_tool(
    state: State,
) -> Literal[
    "handle_error", "ask_for_info", "escalate", "respond", "validate_slots", "prepare_action"
]:
    """The `route_after_tool` decision table (Section 8.3)."""
    result = state.get("tool_result") or {}
    tool = result.get("tool")
    if not result.get("ok"):
        code = (result.get("error") or {}).get("code")
        if tool == "get_cancellation_quote" and code == errors.NOT_ELIGIBLE:
            return "respond"
        return "handle_error"
    if tool == "verify_policyholder":
        if not state.get("verified"):
            max_attempts = get_settings().MAX_VERIFICATION_ATTEMPTS
            if state.get("verification_attempts", 0) >= max_attempts:
                return "escalate"
            return "ask_for_info"
        return "validate_slots"
    if tool == "get_policy_status":
        intent = state.get("active_intent")
        if intent == "policy_status":
            return "respond"
        status = (state.get("policy_snapshot") or {}).get("status")
        if intent == "cancel_policy" and status in INACTIVE_POLICY_STATUSES:
            return "respond"
        return "validate_slots"
    if tool == "get_cancellation_quote":
        return "prepare_action"
    return "handle_error"


def route_confirm(
    state: State,
) -> Literal[
    "escalate",
    "announce_approval",
    "execute_write_action",
    "invalidate_on_edit",
    "ask_confirmation",
    "respond",
]:
    decision = annotation(state, CONFIRMATION).get("decision")
    if decision == "handoff":
        return "escalate"
    if decision == "confirm_yes":
        action = state.get("pending_action") or {}
        return "announce_approval" if action.get("requires_approval") else "execute_write_action"
    if decision == "edit":
        return "invalidate_on_edit"
    if decision == "other":
        return "ask_confirmation"
    return "respond"


def route_approval(state: State) -> Literal["execute_write_action", "respond"]:
    approval = state.get("approval") or {}
    return "execute_write_action" if approval.get("status") == "approved" else "respond"


def route_write(state: State) -> Literal["handle_error", "respond"]:
    return "handle_error" if state.get("error") else "respond"


def route_pending_question(
    state: State,
) -> Literal[
    "escalate", "validate_slots", "execute_write_action", "ask_yes_no", "respond", "understand"
]:
    answer = annotation(state, PENDING_ANSWER)
    kind = answer.get("kind")
    value = answer.get("answer")
    if value == "handoff":
        return "escalate"
    if value == "yes":
        return "execute_write_action" if kind == "retry_write" else "validate_slots"
    if value == "reask":
        return "ask_yes_no"
    if value == "no":
        if state.get("last_outcome") == "WRITE_OUTCOME_UNKNOWN":
            return "escalate"
        return "respond"
    return "understand"


def route_error(state: State) -> Literal["escalate", "respond"]:
    if state.get("last_outcome") == "WRITE_OUTCOME_UNKNOWN":
        return "escalate"
    error = state.get("error") or {}
    if error.get("internal") or not error.get("retryable", False):
        return "escalate"
    return "respond"


def route_next_task(state: State) -> str:
    if state.get("pending_question"):
        return END
    if state.get("last_outcome") not in TERMINAL_OUTCOMES:
        return END
    if state.get("intent_queue") or state.get("suspended_tasks"):
        return "offer_next_task"
    return END
