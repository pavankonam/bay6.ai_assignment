"""Rule-based language understanding.

- `strict=True` is the FR48 fallback used when the LLM fails: policy-number regex + date extractor
  (done by the caller), keyword intents, bare yes/no replies. No free-text slot extraction.
- `strict=False` additionally extracts free-text answers to the question the agent just asked.
  It powers the fake LLM's unscripted replies (tests and the no-API-key demo).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from app.nlu.schemas import IntentLabel, UnderstandResult
from app.validation.validators import SKIP_VALUE, map_reason, parse_amount

HANDOFF_RE = re.compile(
    r"\b(human|person|agent|representative|someone real|call me|real person|operator|"
    r"speak (to|with) someone|talk (to|with) someone)\b",
    re.IGNORECASE,
)
COVERAGE_QUESTION_RE = re.compile(
    r"\b(will|would|is|are|does|do|am|can)\b[^.?!]{0,40}\b(covered|cover|covers|pay|paid|"
    r"payout|reimburse|reimbursed)\b",
    re.IGNORECASE,
)
CANCEL_RE = re.compile(r"\bcancel", re.IGNORECASE)
CLAIM_RE = re.compile(
    r"\b(claim|accident|crash|crashed|hit|rear[- ]?ended|collision|fender[- ]bender|"
    r"side[- ]?swiped|vandali[sz]ed|stolen|theft|totaled|dented|damaged)\b",
    re.IGNORECASE,
)
CLAIM_STRICT_RE = re.compile(r"\b(claim|accident|crash|hit)\b", re.IGNORECASE)
STATUS_RE = re.compile(
    r"\b(status|active|lapsed|in force|still valid|still good|policy details|"
    r"coverage period|deductibles?|paid through|is it valid)\b",
    re.IGNORECASE,
)
STATUS_STRICT_RE = re.compile(r"\bstatus\b", re.IGNORECASE)
OUT_OF_SCOPE_RE = re.compile(
    r"\b(home|house|renters?|life|health|pet|travel|boat|motorcycle|quote for|new policy|"
    r"add (a )?driver|change (my )?address|billing|weather|joke)\b",
    re.IGNORECASE,
)
SMALLTALK_RE = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|thx|good (morning|afternoon|evening)|ok|okay|cool|"
    r"great|yes|no|y|n|yep|nope|sure|bye|goodbye)[\s!.,]*$",
    re.IGNORECASE,
)
CORRECTION_RE = re.compile(
    r"\b(actually|sorry,? i meant|i meant|correction|instead|wrong|should be|change (it|that))\b",
    re.IGNORECASE,
)
SKIP_RE = re.compile(
    r"^\s*(skip|no|none|nope|n/a|na|don'?t know|do not know|not sure|no idea|i don'?t have "
    r"(one|it|that)|no report|no estimate)[\s.!]*$",
    re.IGNORECASE,
)
NO_REPORT_RE = re.compile(r"\bno (police )?report\b|\bno report number\b", re.IGNORECASE)
NO_ESTIMATE_RE = re.compile(r"\bno (estimate|idea)\b|\bdon'?t know (the )?(cost|estimate)", re.I)
REPORT_NUMBER_RE = re.compile(
    r"\b(?:police\s+)?report(?:\s+(?:number|no\.?|#))?\s*(?:is|:|#)?\s*([A-Z0-9][A-Z0-9-]{2,29})\b",
    re.IGNORECASE,
)
AMOUNT_IN_TEXT_RE = re.compile(
    r"(?:\$\s*\d[\d,]*(?:\.\d+)?\s*(?:k|thousand|grand)?)|"
    r"(?:\b\d[\d,]*(?:\.\d+)?\s*(?:k|thousand|grand|dollars|bucks|usd)\b)|"
    r"(?:\b(?:about|around|roughly|approximately|estimate(?:d)?(?: is| of)?)\s+"
    r"\d[\d,]*(?:\.\d+)?\b)",
    re.IGNORECASE,
)
BARE_NUMBER_RE = re.compile(r"^\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*(k|thousand)?\s*$", re.IGNORECASE)
NEGATION_RE = re.compile(
    r"\b(no|not|none|nobody|no one|noone|didn'?t|wasn'?t|weren'?t|without|neither|nor)\b",
    re.IGNORECASE,
)
INJURY_RE = re.compile(r"\b(injur\w*|hurt|harmed|wounded|ambulance|hospital)\b", re.IGNORECASE)
PARTY_RE = re.compile(
    r"\b(another|other|car|vehicle|driver|party|truck|someone|somebody|they|bike|cyclist|"
    r"pedestrian|single|just me|only me|by myself)\b",
    re.IGNORECASE,
)
EXPLICIT_PARTY_RE = re.compile(
    r"\b(another|other|the other) (car|vehicle|driver|party|truck)\b|\bsomeone (hit|rear)",
    re.IGNORECASE,
)
SOLO_RE = re.compile(r"\b(single|just me|only me|by myself|no other|nobody else)\b", re.IGNORECASE)
YES_RE = re.compile(r"^\s*(yes|yeah|yep|y|yup|sure|correct|right|there was|i was)\b", re.I)
NO_RE = re.compile(r"^\s*(no|nope|n|nah|not)\b", re.IGNORECASE)
GIBBERISH_RE = re.compile(r"^[^aeiouy\s]{6,}$|^[\W\d_]+$", re.IGNORECASE)
_SEGMENT_RE = re.compile(r"[,;.]|\band\b|\bbut\b", re.IGNORECASE)
_LOCATION_PREFIX_RE = re.compile(r"^(it (was|happened) |this was )?(at|on|near|in)\s+", re.I)

# FR29: bare replies (whole trimmed message, ignoring case and trailing punctuation).
AFFIRMATIVE = frozenset(
    {"yes", "y", "yep", "yeah", "confirm", "submit", "ok", "okay", "go ahead", "sounds good"}
)
NEGATIVE = frozenset({"no", "n", "nope", "don't", "dont", "cancel"})
RETRY_YES = frozenset({"retry", "try again"})
RETRY_NO = frozenset({"stop", "cancel that", "wait", "withdraw", "withdraw it"})


def bare(text: str) -> str:
    return re.sub(r"[\s.!?,]+$", "", text.strip().lower())


def bare_yes_no(text: str, *, retry: bool = False) -> str | None:
    """'yes' / 'no' for a bare reply, else None (FR29, FR11a)."""
    reply = bare(text)
    if reply in AFFIRMATIVE or (retry and reply in RETRY_YES):
        return "yes"
    if reply in NEGATIVE or (retry and reply in RETRY_NO):
        return "no"
    return None


def is_handoff(text: str) -> bool:
    return bool(HANDOFF_RE.search(text))


def is_coverage_question(text: str) -> bool:
    """FR51: "will I be covered?", "will this be paid?" and similar."""
    explicit = re.search(r"\b(am i|will i be|is (this|it|that)) covered\b", text, re.IGNORECASE)
    return bool(explicit) or (bool(COVERAGE_QUESTION_RE.search(text)) and "?" in text)


def is_skip(text: str) -> bool:
    return bool(SKIP_RE.match(text))


def keyword_intents(text: str, *, strict: bool) -> list[IntentLabel]:
    found: list[tuple[int, IntentLabel]] = []
    patterns: tuple[tuple[re.Pattern[str], IntentLabel], ...] = (
        (STATUS_STRICT_RE if strict else STATUS_RE, "policy_status"),
        (CLAIM_STRICT_RE if strict else CLAIM_RE, "report_claim"),
        (CANCEL_RE, "cancel_policy"),
    )
    for pattern, intent in patterns:
        match = pattern.search(text)
        if match:
            found.append((match.start(), intent))
    return [intent for _, intent in sorted(found)]


@dataclass
class RuleContext:
    active_intent: str | None = None
    asked_slots: Collection[str] = ()
    has_identity: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _segments(text: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_RE.split(text) if s and s.strip()]


def _bool_answers(text: str, asked: Collection[str]) -> dict[str, Any]:
    """Injuries / other party from replies like "No injuries, yes another car"."""
    found: dict[str, Any] = {}
    segments = _segments(text)
    party_asked = "other_party_involved" in asked
    for segment in segments:
        negated = bool(NEGATION_RE.search(segment))
        explicit_party = bool(EXPLICIT_PARTY_RE.search(segment))
        if INJURY_RE.search(segment) and "injuries" not in found:
            found["injuries"] = not negated
        elif SOLO_RE.search(segment) and "other_party_involved" not in found:
            found["other_party_involved"] = False
        elif (
            explicit_party or (party_asked and PARTY_RE.search(segment))
        ) and "other_party_involved" not in found:
            found["other_party_involved"] = not negated
    bool_slots = [s for s in ("injuries", "other_party_involved") if s in asked]
    bare_answers = [s for s in segments if YES_RE.match(s) or NO_RE.match(s)]
    if bare_answers and len(bare_answers) == len(segments):
        unassigned = [s for s in bool_slots if s not in found]
        for slot, answer in zip(unassigned, bare_answers, strict=False):
            found[slot] = bool(YES_RE.match(answer))
    return found


def _amount(text: str, asked: Collection[str]) -> float | None:
    match = AMOUNT_IN_TEXT_RE.search(text)
    if match is None and "estimated_damage_usd" in asked and BARE_NUMBER_RE.match(text):
        return parse_amount(text.strip())
    if match is None:
        return None
    token = re.sub(
        r"^(about|around|roughly|approximately|estimate(d)?( is| of)?)\s+",
        "",
        match.group(0).strip(),
        flags=re.IGNORECASE,
    )
    return parse_amount(token)


def heuristic_task_slots(text: str, ctx: RuleContext, intent: str | None) -> dict[str, Any]:
    """Free-text slot answers (non-strict mode only)."""
    asked = set(ctx.asked_slots)
    slots: dict[str, Any] = {}
    if intent == "report_claim":
        if "incident_location" in asked and not is_skip(text):
            location = _LOCATION_PREFIX_RE.sub("", text.strip()).rstrip(".")
            if "incident_description" in asked and " - " in location:
                location, description = location.split(" - ", 1)
                slots["incident_description"] = description.strip()
            slots["incident_location"] = location.strip()
        elif "incident_description" in asked and not is_skip(text):
            slots["incident_description"] = text.strip()
        elif (
            ctx.active_intent != "report_claim"
            and CLAIM_RE.search(text)
            and not re.search(r"\bPOL\b|\[DOB\]|\d", text, re.IGNORECASE)
        ):
            slots["incident_description"] = text.strip().rstrip(".")
        slots.update(_bool_answers(text, asked))
        amount = _amount(text, asked)
        if amount is not None:
            slots["estimated_damage_usd"] = amount
        report = REPORT_NUMBER_RE.search(text)
        if report and not NO_REPORT_RE.search(text) and any(c.isdigit() for c in report.group(1)):
            slots["police_report_number"] = report.group(1)
        if NO_REPORT_RE.search(text):
            slots["police_report_number"] = SKIP_VALUE
        if NO_ESTIMATE_RE.search(text):
            slots["estimated_damage_usd"] = SKIP_VALUE
        if is_skip(text):
            for slot in ("estimated_damage_usd", "police_report_number"):
                if slot in asked:
                    slots[slot] = SKIP_VALUE
    elif intent == "cancel_policy":
        reason = map_reason(text)
        if reason is not None and (reason != "other" or "reason" in asked):
            slots["reason"] = reason
    return slots


def rule_understand(text: str, ctx: RuleContext, *, strict: bool) -> UnderstandResult:
    """Classify + extract with rules. Identity and dates come from the deterministic extractors."""
    intents: list[IntentLabel] = []
    if is_handoff(text):
        intents = ["human_handoff"]
    elif not strict and is_coverage_question(text):
        intents = ["out_of_scope"]
    else:
        intents = keyword_intents(text, strict=strict)
    if not intents:
        question = text.strip().endswith("?")
        if not strict and OUT_OF_SCOPE_RE.search(text) and (question or not ctx.active_intent):
            intents = ["out_of_scope"]
        elif SMALLTALK_RE.match(text) and not (ctx.asked_slots and bare_yes_no(text)):
            intents = ["smalltalk"]
        elif ctx.active_intent or ctx.has_identity:
            intents = ["provide_info"]
        elif not strict and question:
            intents = ["out_of_scope"]
    if (
        intents
        and intents[0] in ("policy_status", "report_claim", "cancel_policy")
        and intents[0] == ctx.active_intent
        and ctx.asked_slots
    ):
        intents = ["provide_info", *intents[1:]]
    if not intents:
        # Nothing recognized: low confidence → clarify (FR7); in strict fallback, clarify shows
        # the system-unavailable message (FR48).
        confidence = 0.0 if strict else (0.2 if GIBBERISH_RE.match(text.strip()) else 0.4)
        return UnderstandResult(intents=["provide_info"], confidence=confidence)
    task_slots: dict[str, dict[str, Any]] = {}
    if not strict:
        target = ctx.active_intent if intents[0] == "provide_info" else intents[0]
        for intent in {target, *intents}:
            if intent in ("report_claim", "cancel_policy"):
                extracted = heuristic_task_slots(text, ctx, intent)
                if extracted:
                    task_slots[str(intent)] = extracted
    return UnderstandResult(
        intents=intents,
        confidence=0.9,
        task_slots=task_slots,
        is_correction=bool(CORRECTION_RE.search(text)),
    )
