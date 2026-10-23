"""Rule-based answers for unscripted fake-LLM calls (tests that don't script, and the demo).

Registered with `app.llm_fake` on first use. These mimic a well-behaved model: they never see
the raw DOB (it is redacted before the call) and only use what the request contains.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import today
from app.llm_fake import FakeRequest, register_heuristic
from app.nlu.rules import (
    CORRECTION_RE,
    RuleContext,
    bare_yes_no,
    heuristic_task_slots,
    is_handoff,
    rule_understand,
)
from app.nlu.schemas import ConfirmationResult, UnderstandResult, YesNoResult
from app.responses.outcomes import render_template
from app.validation.extractors import extract_dates

_EDIT_CUE_RE = re.compile(
    r"\b(change|actually|instead|correct(ion)?|wrong|should be|meant|but|edit|update)\b", re.I
)
_LOCATION_EDIT_RE = re.compile(r"\blocation\s+(?:to|is|was|should be)\s+(.+?)\s*[.!]?$", re.I)
_YES_START_RE = re.compile(r"^\s*(yes|yeah|yep|ok|okay|sure|confirm|go ahead|submit)\b", re.I)
_NO_START_RE = re.compile(r"^\s*(no|nope|don'?t|do not|never mind|cancel)\b", re.I)


def _understand(request: FakeRequest) -> UnderstandResult:
    ctx = request.context
    rule_ctx = RuleContext(
        active_intent=ctx.get("active_intent"),
        asked_slots=ctx.get("last_asked_slots") or [],
        has_identity=bool(ctx.get("filled_slots") or ctx.get("identity_in_message")),
    )
    return rule_understand(request.user_text, rule_ctx, strict=False)


def _confirmation(request: FakeRequest) -> ConfirmationResult:
    text = request.user_text
    if is_handoff(text):
        return ConfirmationResult(decision="handoff")
    fields = request.context.get("summary_fields") or {}
    changes: dict[str, Any] = {}
    if _EDIT_CUE_RE.search(text):
        location = _LOCATION_EDIT_RE.search(text)
        if location and "incident_location" in fields:
            changes["incident_location"] = location.group(1)
        date_field = next((f for f in ("incident_date", "effective_date") if f in fields), None)
        if date_field:
            dates = extract_dates(text, today=today(), asked_date_slots=[date_field])
            for mention in dates.mentions:
                if mention.value is not None:
                    changes[date_field] = mention.value.isoformat()
        intent = "report_claim" if "incident_date" in fields else "cancel_policy"
        extracted = heuristic_task_slots(
            text, RuleContext(active_intent=intent, asked_slots=list(fields)), intent
        )
        changes.update(
            {
                k: v
                for k, v in extracted.items()
                if k in fields
                and k not in changes
                and k not in ("incident_location", "incident_description")
            }
        )
        if changes:
            return ConfirmationResult(decision="edit", slot_changes=changes)
        if CORRECTION_RE.search(text):
            return ConfirmationResult(decision="other")
    if _YES_START_RE.match(text) and not _EDIT_CUE_RE.search(text):
        return ConfirmationResult(decision="confirm_yes")
    if _NO_START_RE.match(text):
        return ConfirmationResult(decision="confirm_no")
    return ConfirmationResult(decision="other")


def _yes_no(request: FakeRequest) -> YesNoResult:
    text = request.user_text
    if is_handoff(text):
        return YesNoResult(answer="handoff")
    bare = bare_yes_no(text, retry=request.context.get("question_kind") == "retry_write")
    if bare is not None:
        return YesNoResult(answer="yes" if bare == "yes" else "no")
    if re.match(r"^\s*(yes|yeah|sure|please|ok|okay|retry)\b", text, re.I):
        return YesNoResult(answer="yes")
    if re.match(r"^\s*(no|nope|not now|never mind|stop)\b", text, re.I):
        return YesNoResult(answer="no")
    return YesNoResult(answer="unrelated")


def _confirm_or_yes_no(request: FakeRequest) -> Any:
    if request.tools and request.tools[0] == "ConfirmationResult":
        return _confirmation(request)
    return _yes_no(request)


def _respond(request: FakeRequest) -> str:
    """A factual reply rendered from the facts in the context block."""
    facts = request.context.get("facts") or {}
    outcome = request.context.get("outcome")
    state: dict[str, Any] = {"policy_snapshot": facts.get("policy")}
    text = render_template(outcome, state, facts)
    question = request.context.get("pending_question_text")
    if question and question not in text:
        text = f"{text} {question}"
    return text


register_heuristic("classify_extract", _understand)
register_heuristic("confirm_yes_no", _confirm_or_yes_no)
register_heuristic("respond", _respond)
