"""Deterministic customer-facing templates (FR50: decision outcomes never use the LLM)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.config import get_settings

CAPABILITIES = (
    "checking your policy status, reporting a vehicle claim, or requesting a cancellation"
)
INTENT_LABELS = {
    "policy_status": "checking your policy status",
    "report_claim": "reporting a vehicle claim",
    "cancel_policy": "requesting a policy cancellation",
}
INTENT_LEAD_INS = {
    "policy_status": "I can help with that.",
    "report_claim": "I'm sorry to hear that. I'll help you report a claim.",
    "cancel_policy": "I can help with that.",
}
VERIFICATION_FAILED = (
    "I couldn't verify those details. Please double-check your policy number and date of birth."
)
COVERAGE_ANSWER = (
    "I can't determine coverage or payment. An adjuster will review your claim and determine "
    "eligibility."
)
NO_COLLISION_NOTE = (
    "Your policy does not include collision coverage; an adjuster will determine eligibility."
)
QUESTIONS: dict[frozenset[str], str] = {
    frozenset({"policy_id", "dob"}): (
        "Please provide your policy number (format POL-123456) and your date of birth."
    ),
    frozenset({"policy_id"}): "What's your policy number (format POL-123456)?",
    frozenset({"dob"}): "What's your date of birth?",
    frozenset({"incident_date"}): "When did the incident happen?",
    frozenset({"incident_location"}): "Where did the incident happen?",
    frozenset({"incident_description"}): "Briefly, what happened?",
    frozenset({"incident_location", "incident_description"}): (
        "Where did the incident happen, and briefly, what happened?"
    ),
    frozenset({"injuries", "other_party_involved"}): (
        "Was anyone injured? And was another vehicle or party involved?"
    ),
    frozenset({"injuries"}): "Was anyone injured?",
    frozenset({"other_party_involved"}): "Was another vehicle or party involved?",
    frozenset({"estimated_damage_usd", "police_report_number"}): (
        "Do you have a rough damage estimate or police report number? "
        '(Optional, you can say "skip")'
    ),
    frozenset({"estimated_damage_usd"}): (
        'Do you have a rough damage estimate? (Optional, you can say "skip")'
    ),
    frozenset({"police_report_number"}): (
        'Do you have a police report number? (Optional, you can say "skip")'
    ),
    frozenset({"effective_date", "reason"}): (
        "When should the cancellation take effect, and what's the reason? "
        "(sold vehicle, switching provider, cost, no longer needed, other)"
    ),
    frozenset({"effective_date"}): "When should the cancellation take effect?",
    frozenset({"reason"}): (
        "What's the reason for cancelling? "
        "(sold vehicle, switching provider, cost, no longer needed, other)"
    ),
}
DATE_ROLE_LABELS = {
    "dob": "date of birth",
    "incident_date": "incident date",
    "effective_date": "cancellation date",
}


def phone() -> str:
    return get_settings().SUPPORT_PHONE


def more_help() -> str:
    return "Is there anything else I can help with?"


def greeting() -> str:
    return "Hi, I'm the Acme Mutual support assistant."


def system_unavailable() -> str:
    return (
        "I'm having trouble reaching our systems right now. Please try again in a few minutes, "
        f"or call {phone()}."
    )


def escalation(reason: str = "default") -> str:
    contact = f"Please call {phone()} and a support specialist will help you."
    internal = f"Something went wrong on our side while handling your request. {contact}"
    texts = {
        "handoff": f"Of course. You can reach a support specialist at {phone()}.",
        "locked": (
            "I couldn't verify your identity, so I can't continue with policy requests in this "
            f"conversation. For your security, please call {phone()} to continue."
        ),
        "turn_limit": f"This conversation has gone on for a while. {contact}",
        "clarification": f"I'm sorry, I'm having trouble understanding. {contact}",
        "internal_error": internal,
        "recursion_limit": internal,
        "write_outcome_unknown": (
            "A specialist will confirm the status of your approved request and follow up with "
            f"you. You can also call {phone()}."
        ),
    }
    return texts.get(reason, f"I'm going to connect you with a specialist. {contact}")


def empty_message() -> str:
    return "I didn't catch that. How can I help?"


def capabilities_question() -> str:
    return f"I can help with {CAPABILITIES}. Which would you like?"


def which_request(identity_noted: bool) -> str:
    if identity_noted:
        return f"Thanks, I've noted your details. What can I help you with today: {CAPABILITIES}?"
    return f"How can I help you today? I can help with {CAPABILITIES}."


def out_of_scope() -> str:
    return f"I'm sorry, I can't help with that here. I can help with {CAPABILITIES}."


def smalltalk() -> str:
    return "Happy to help!"


def okay_more_help() -> str:
    return f"Okay. {more_help()}"


def other_request_later() -> str:
    return "I can help with your other request once this one is done."


def date_ambiguity(ambiguity: Mapping[str, Any]) -> str:
    dates = [str(d) for d in ambiguity.get("dates", [])]
    listed = " and ".join(dates)
    role = ambiguity.get("role")
    if role:
        return f"I saw {listed}. Which one is your {DATE_ROLE_LABELS.get(str(role), role)}?"
    options = [DATE_ROLE_LABELS[o] for o in ambiguity.get("options", []) if o in DATE_ROLE_LABELS]
    choices = ", ".join(options[:-1]) + (" or " if len(options) > 1 else "") + options[-1]
    return f"I saw {listed}. Is that your {choices}?"


def question_for(slots: Sequence[str]) -> str:
    key = frozenset(slots)
    if key in QUESTIONS:
        return QUESTIONS[key]
    return " ".join(QUESTIONS[frozenset({s})] for s in slots if frozenset({s}) in QUESTIONS)


def confirm_dob_for(policy_id: str) -> str:
    return f"To continue with policy {policy_id}, please confirm the date of birth on that policy."


def money(amount: float | None) -> str:
    if amount is None:
        return "not provided"
    return f"${amount:,.2f}" if amount != int(amount) else f"${int(amount):,}"


def money_exact(amount: float) -> str:
    return f"${amount:,.2f}"


def human_date(iso: str | None) -> str:
    if not iso:
        return "unknown"
    value = date.fromisoformat(iso)
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _coverage(coverage: Mapping[str, Any]) -> str:
    name = str(coverage.get("type", "")).replace("_", " ").title()
    deductible = coverage.get("deductible")
    return f"{name} (${int(deductible):,} deductible)" if deductible else name


def vehicle(snapshot: Mapping[str, Any]) -> str:
    parts = (snapshot.get(k) for k in ("vehicle_year", "vehicle_make", "vehicle_model"))
    return " ".join(str(p) for p in parts)


def lapse_date(snapshot: Mapping[str, Any]) -> str | None:
    for entry in snapshot.get("status_history") or []:
        if entry.get("status") == "LAPSED":
            return str(entry.get("from"))
    return None


def policy_status(snapshot: Mapping[str, Any]) -> str:
    status = str(snapshot.get("status"))
    lines = [
        f"Your policy {snapshot.get('policy_id')} for the {vehicle(snapshot)} is {status}.",
        f"Coverage period: {snapshot.get('term_start')} to {snapshot.get('term_end')}. "
        f"Premium paid through {snapshot.get('paid_through')}.",
        "Coverages: " + ", ".join(_coverage(c) for c in snapshot.get("coverages") or []) + ".",
    ]
    if status == "LAPSED":
        lapsed_on = lapse_date(snapshot)
        lines.append(
            f"It lapsed on {lapsed_on}. To ask about reinstatement, please call {phone()}."
        )
    if status in ("CANCELLED", "PENDING_CANCELLATION"):
        lines.append(f"Cancellation effective date: {snapshot.get('cancellation_effective_date')}.")
    lines.append(more_help())
    return "\n".join(lines)


def not_eligible_cancel(snapshot: Mapping[str, Any]) -> str:
    status = str(snapshot.get("status", "")).replace("_", " ").lower()
    effective = snapshot.get("cancellation_effective_date")
    when = f" (effective {effective})" if effective else ""
    return (
        f"Your policy {snapshot.get('policy_id')} is already {status}{when}, so I can't submit a "
        f"cancellation request. If you think that's wrong, please call {phone()}. {more_help()}"
    )


def not_eligible_cancel_quote() -> str:
    return (
        "That policy isn't eligible for cancellation through chat right now. "
        f"Please call {phone()} and a specialist can help. {more_help()}"
    )


def claim_ineligible(reason: str | None, policy_id: str | None, incident_date: str | None) -> str:
    contact = f"Please call {phone()} and a specialist can help."
    if reason == "TOO_OLD":
        return f"Claims for incidents more than 365 days ago can't be filed through chat. {contact}"
    if reason == "FUTURE_DATE":
        return f"The incident date can't be in the future, so I can't file this claim. {contact}"
    return (
        f"Your policy {policy_id} wasn't in force on {incident_date}, so I can't file this claim "
        f"through chat. {contact}"
    )


def duplicate_claim_question(claim_id: str, incident_date: str) -> str:
    return (
        f"There's already an open claim, {claim_id}, for an incident on {incident_date}. "
        "Do you want to file a new claim anyway? (yes / no)"
    )


def claim_not_filed_duplicate() -> str:
    return f"Okay, I won't file that claim. {more_help()}"


def next_task_offer(intent: str, source: str) -> str:
    if source == "queue":
        return f"You also mentioned {INTENT_LABELS[intent]}. Would you like to continue with that?"
    continuing = {
        "report_claim": "reporting your claim",
        "cancel_policy": "your cancellation request",
        "policy_status": "checking your policy status",
    }[intent]
    return f"Would you like to continue {continuing}?"


def retry_write_reask() -> str:
    return "Before we continue: should I retry your approved request? Reply 'retry' or 'no'."


APPROVAL_REASON_TEXT = {
    "injuries_reported": "injuries were reported",
    "high_damage_estimate": "the damage estimate is $10,000 or more",
    "damage_estimate_unknown": "there's no damage estimate yet",
}
REASON_TEXT = {
    "sold_vehicle": "sold vehicle",
    "switching_provider": "switching provider",
    "cost": "cost",
    "no_longer_needed": "no longer needed",
    "other": "other",
}


def yes_no(value: Any) -> str:
    return "Yes" if value else "No"


def restated(iso: str | None) -> str:
    """ISO date plus the long form, e.g. `2026-10-21 (October 21, 2026)` (FR28)."""
    return f"{iso} ({human_date(iso)})" if iso else "unknown"


def claim_summary(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    approval_reasons: Sequence[str],
    no_collision: bool,
    duplicate_of: str | None,
    feedback: Sequence[str] = (),
) -> str:
    police = payload.get("police_report_number") or "none"
    lines = [*feedback, "Here's your claim summary:"]
    lines += [
        f"• Policy: {payload.get('policy_id')} ({vehicle(snapshot)})",
        f"• Date: {restated(payload.get('incident_date'))}  "
        f"• Location: {payload.get('incident_location')}",
        f"• Description: {payload.get('incident_description')}",
        f"• Injuries: {yes_no(payload.get('injuries'))}  "
        f"• Other party: {yes_no(payload.get('other_party_involved'))}  "
        f"• Estimate: {money(payload.get('estimated_damage_usd'))}  • Police report: {police}",
    ]
    if no_collision:
        lines.append(NO_COLLISION_NOTE)
    if duplicate_of:
        lines.append(f"Note: this will be filed even though claim {duplicate_of} is open.")
    if approval_reasons:
        why = " and ".join(APPROVAL_REASON_TEXT.get(r, r) for r in approval_reasons)
        lines.append(
            f"Because {why}, a support specialist will review this claim before it's filed."
        )
    lines.append("Shall I submit this claim? (yes / no / edit)")
    return "\n".join(lines)


def cancellation_summary(
    payload: Mapping[str, Any],
    quote: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    feedback: Sequence[str] = (),
) -> str:
    premium = money(float(snapshot.get("annual_premium_usd") or 0))
    refund = money_exact(float(quote.get("refund_estimate_usd", 0)))
    fee = money(float(quote.get("fee_usd", 0)))
    effective = restated(payload.get("effective_date"))
    reason = REASON_TEXT.get(str(payload.get("reason")), payload.get("reason"))
    lines = [
        *feedback,
        f"Cancelling {payload.get('policy_id')} effective {effective} (reason: {reason}).",
        f"Estimated refund: {refund} (pro-rated for {quote.get('remaining_days')} of "
        f"{quote.get('term_days')} days on a {premium} premium, minus {fee} cancellation fee).",
    ]
    if snapshot.get("status") == "LAPSED":
        lines.append("Any outstanding balance will be reviewed by a specialist.")
    lines.append("A support specialist will review this request before it's processed.")
    lines.append("Do you want to submit this cancellation request? (yes / no / edit)")
    return "\n".join(lines)


def confirmation_reask(action_type: str) -> str:
    noun = "claim" if action_type == "create_claim" else "cancellation request"
    return f"Sorry, I didn't catch that. Shall I submit this {noun}? Please reply yes, no, or edit."


def declined() -> str:
    return f"Okay, I won't submit it. {more_help()}"


def specialist_note(comment: str | None) -> str:
    return f' Note from specialist: "{comment}"' if comment else ""


def claim_created(claim_id: str, next_steps: str, approved: bool, comment: str | None) -> str:
    lead = "Your claim was approved and has been filed." if approved else ""
    body = f"Your claim {claim_id} has been filed. {next_steps}"
    if lead:
        body = f"{lead} Claim number: {claim_id}. {next_steps}"
    return f"{body}{specialist_note(comment)} {more_help()}"


def cancel_completed(
    policy_id: str, effective_date: str, cancellation_id: str, refund: float, comment: str | None
) -> str:
    return (
        f"Your cancellation was approved. Policy {policy_id} will be cancelled effective "
        f"{effective_date}. Confirmation: {cancellation_id}. Estimated refund "
        f"{money_exact(refund)} within 7–10 business days.{specialist_note(comment)} {more_help()}"
    )


def write_not_eligible(action_type: str) -> str:
    noun = "claim" if action_type == "create_claim" else "cancellation"
    return (
        f"We couldn't complete your {noun} because the policy is no longer eligible for it. "
        f"Please call {phone()} and a specialist can help."
    )


def duplicate_at_write(claim_id: str | None) -> str:
    existing = f" ({claim_id})" if claim_id else ""
    return (
        f"There's already an open claim{existing} for that incident date, so I didn't file a new "
        f"one. If you'd still like to file, please start the claim again or call {phone()}."
    )
