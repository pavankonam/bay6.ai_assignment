"""Outcome → customer message. Decision outcomes are template-only (FR50)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

from app.responses import templates

# FR50: these outcomes never use the LLM.
TEMPLATE_ONLY_OUTCOMES = frozenset(
    {
        "CLAIM_CREATED", "CLAIM_INELIGIBLE", "CLAIM_REJECTED", "CANCEL_COMPLETED",
        "CANCEL_REJECTED", "NOT_ELIGIBLE_CANCEL", "WRITE_NOT_ELIGIBLE", "APPROVAL_EXPIRED",
        "WRITE_WITHDRAWN", "WRITE_OUTCOME_UNKNOWN", "CLAIM_NOT_FILED_DUPLICATE", "ESCALATED",
        "DECLINED_BY_CUSTOMER", "SYSTEM_UNAVAILABLE",
    }
)  # fmt: skip


def last_question_meta(state: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """The most recent question asked for the task in progress, and the slots it asked for."""
    if not state.get("active_intent"):
        return None, []
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and not message.tool_calls:
            question = message.additional_kwargs.get("question")
            if question:
                asked = message.additional_kwargs.get("asked_slots") or []
                return str(question), [str(s) for s in asked]
    return None, []


def last_question(state: Mapping[str, Any]) -> str | None:
    return last_question_meta(state)[0]


def render_template(outcome: str | None, state: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    snapshot = facts.get("policy") or state.get("policy_snapshot") or {}
    if facts.get("template") == "okay_more_help":
        return templates.okay_more_help()
    if facts.get("template") == "no_resubmit":
        return f"Okay, I won't resubmit it. {templates.more_help()}"
    if outcome == "STATUS_PROVIDED":
        return templates.policy_status(snapshot)
    if outcome == "NOT_ELIGIBLE_CANCEL":
        if facts.get("source") == "quote":
            return templates.not_eligible_cancel_quote()
        return templates.not_eligible_cancel(snapshot)
    if outcome == "CLAIM_INELIGIBLE":
        checks = state.get("claim_checks") or {}
        return templates.claim_ineligible(
            checks.get("reason"), state.get("verified_policy_id"), checks.get("incident_date")
        )
    if outcome == "CLAIM_NOT_FILED_DUPLICATE":
        return templates.claim_not_filed_duplicate()
    if outcome == "SYSTEM_UNAVAILABLE":
        return templates.system_unavailable()
    if outcome == "OUT_OF_SCOPE_ANSWERED":
        text = (
            templates.COVERAGE_ANSWER
            if facts.get("coverage_question")
            else (templates.out_of_scope())
        )
        return _with_question(text, state)
    if outcome == "SMALLTALK":
        return _with_question(templates.smalltalk(), state)
    return templates.more_help()


def _with_question(text: str, state: Mapping[str, Any]) -> str:
    question = last_question(state)
    return f"{text} {question}" if question else text
