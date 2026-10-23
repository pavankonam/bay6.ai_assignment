"""`understand`: deterministic pre-extraction + one structured LLM call (FR6–FR12a, FR48).

The result is stored as this turn's `nlu` annotation on the human message (DECISIONS D-021);
`validate_slots` validates and merges it. Intent switches suspend the current task here (FR11).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.config import get_settings, today
from app.graph.lifecycle import SUSPEND_RESET, identity_only, task_only
from app.graph.messages import NLU, annotate, last_asked_slots, latest_human_text
from app.graph.nodes.base import graph_node
from app.graph.state import INTENTS, QueuedTask, SuspendedTask
from app.llm import build_history, structured_call
from app.nlu.redaction import redactor_for
from app.nlu.rules import RuleContext, is_coverage_question, keyword_intents, rule_understand
from app.nlu.schemas import UnderstandResult
from app.validation.extractors import (
    DateAssignment,
    DateMention,
    extract_dates,
    extract_policy_ids,
)
from app.validation.slots import DATE_SLOTS, TASK_DATE_SLOT, relevant_date_roles, task_slot_names

_BARE_NUMBER_RE = re.compile(r"(?<![\w/-])\d{3,10}(?![\w/-])")


def _date_slots(slots: list[str]) -> list[str]:
    return [s for s in slots if s in DATE_SLOTS]


def _identity_candidates(
    text: str, asked: list[str], result: UnderstandResult, dates: DateAssignment
) -> tuple[dict[str, Any], dict[str, str]]:
    identity: dict[str, Any] = {}
    errors: dict[str, str] = {}
    policies = extract_policy_ids(text)
    if policies.values:
        identity["policy_id"] = policies.values[0]  # deterministic wins (FR12)
    elif policies.invalid:
        identity["policy_id"] = policies.invalid[0]  # fails validation → specific feedback
    elif result.identity_slots.get("policy_id"):
        identity["policy_id"] = str(result.identity_slots["policy_id"])
    elif "policy_id" in asked:
        bare = _BARE_NUMBER_RE.search(text)
        if bare:
            identity["policy_id"] = bare.group(0)
    dob = dates.by_role().get("dob")
    if dob is not None:
        if dob.value is not None:
            identity["dob"] = dob.value.isoformat()
        elif dob.error:
            errors["dob"] = dob.error
    elif isinstance(result.identity_slots.get("dob"), str):
        # The LLM's value must parse as an ISO date (FR12a).
        with contextlib.suppress(ValueError):
            identity["dob"] = date.fromisoformat(str(result.identity_slots["dob"])).isoformat()
    return identity, errors


def _task_candidates(
    task_intent: str | None, result: UnderstandResult, dates: DateAssignment
) -> tuple[dict[str, Any], dict[str, str]]:
    if task_intent not in ("report_claim", "cancel_policy"):
        return {}, {}
    allowed = set(task_slot_names(task_intent))
    raw = result.task_slots.get(task_intent, {})
    task = {k: v for k, v in raw.items() if k in allowed and v is not None and v != ""}
    errors: dict[str, str] = {}
    date_slot = TASK_DATE_SLOT[task_intent]
    mention = dates.by_role().get(date_slot)
    if mention is not None:
        if mention.value is not None:
            task[date_slot] = mention.value.isoformat()  # deterministic span wins
        else:
            task.pop(date_slot, None)
            if mention.error:
                errors[date_slot] = mention.error
    elif date_slot in task:
        try:
            task[date_slot] = date.fromisoformat(str(task[date_slot])).isoformat()
        except ValueError:
            task.pop(date_slot)
    return task, errors


def _queue_others(
    state: Mapping[str, Any],
    others: Sequence[str],
    result: UnderstandResult,
    dates: DateAssignment,
) -> list[QueuedTask]:
    """FR10: queue the other supported intents with the slot candidates attributed to them."""
    queue: list[QueuedTask] = list(state.get("intent_queue") or [])
    queued: set[str] = {task["intent"] for task in queue}
    for intent in others:
        if intent in queued:
            continue
        slots, _errors = _task_candidates(intent, result, dates)
        queue.append({"intent": intent, "slots": slots})  # type: ignore[typeddict-item]
        queued.add(intent)
    return queue


def _mention_context(mention: DateMention) -> dict[str, Any]:
    """Code-derived date info for the prompt; DOB values are never included (NFR8a)."""
    could_be_dob = mention.role == "dob" or "dob" in mention.candidate_roles
    value = mention.value.isoformat() if mention.value and not could_be_dob else None
    return {"value": "[DOB]" if could_be_dob else value, "deterministic_role": mention.role}


def _suspend(state: Mapping[str, Any], new_intent: str) -> dict[str, Any]:
    """FR11: move the current task (task slots only) to `suspended_tasks` and switch."""
    current = state.get("active_intent")
    slots = state.get("slots") or {}
    update: dict[str, Any] = {**SUSPEND_RESET, "active_intent": new_intent}
    update["slots"] = identity_only(slots)
    suspended = list(state.get("suspended_tasks") or [])
    if current and current != "policy_status":  # a status check has nothing to resume
        task: SuspendedTask = {
            "intent": current,
            "slots": task_only(slots),
            "policy_id": str(slots.get("policy_id") or state.get("verified_policy_id") or ""),
        }
        suspended = [t for t in suspended if t["intent"] != current] + [task]
    update["suspended_tasks"] = suspended
    if state.get("status") == "failed_retryable":
        update.update({"error": None, "resume_step": None, "status": "in_progress"})
    return update


@graph_node("understand")
def understand(state: Mapping[str, Any]) -> dict[str, Any]:
    text = latest_human_text(state)
    now = today()
    active = state.get("active_intent")
    asked = last_asked_slots(state)
    guess = keyword_intents(text, strict=False)
    pre = extract_dates(
        text,
        today=now,
        asked_date_slots=_date_slots(asked),
        relevant_roles=relevant_date_roles(active or (guess[0] if guess else None)),
    )
    pre_dob = pre.by_role().get("dob")
    redact = redactor_for(state, extra_dob=pre_dob.value if pre_dob else None)
    slots = state.get("slots") or {}
    context = {
        "active_intent": active,
        "phase": state.get("phase"),
        "filled_slots": sorted(k for k, v in slots.items() if v is not None),
        "last_asked_slots": asked,
        "identity_in_message": bool(extract_policy_ids(text).values or pre_dob),
        "date_mentions": [_mention_context(m) for m in pre.mentions],
    }
    llm_calls = int(state.get("llm_calls", 0))
    outcome = structured_call(
        UnderstandResult,
        node="understand",
        prompt="classify_extract",
        context=context,
        history=build_history(state.get("messages") or [], redact),
        llm_calls=llm_calls,
    )
    fallback = outcome.value is None
    result = outcome.value or rule_understand(
        text,
        RuleContext(active, asked, has_identity=bool(extract_policy_ids(text).values or pre_dob)),
        strict=True,
    )
    intents = list(result.intents) or ["provide_info"]
    coverage_question = is_coverage_question(text)
    if coverage_question and intents[0] != "human_handoff":
        intents = ["out_of_scope"]  # FR51: answered with the coverage template
    primary = intents[0]
    task_intent = primary if primary in INTENTS else active
    dates = extract_dates(
        text,
        today=now,
        asked_date_slots=_date_slots(asked),
        relevant_roles=relevant_date_roles(task_intent),
        llm_roles=[(d.text, d.role) for d in result.date_roles],
    )
    identity, identity_errors = _identity_candidates(text, asked, result, dates)
    task, task_errors = _task_candidates(task_intent, result, dates)
    ambiguous_role = (dates.ambiguity or {}).get("role")
    if ambiguous_role:  # FR12a: never guess between two dates claiming one role
        identity.pop(str(ambiguous_role), None)
        task.pop(str(ambiguous_role), None)
    other_intents = [i for i in intents[1:] if i in INTENTS and i != primary]
    confidence = 1.0 if coverage_question else float(result.confidence)
    nlu = {
        "intents": intents,
        "primary": primary,
        "confidence": confidence,
        "fallback": fallback,
        "coverage_question": coverage_question,
        "identity": identity,
        "task_intent": task_intent,
        "task": task,
        "date_errors": {**identity_errors, **task_errors},
        "date_ambiguity": dates.ambiguity,
        "dates": [m.to_dict() for m in dates.mentions],
        "is_correction": bool(result.is_correction),
        "other_intents": other_intents,
        "merged": False,
    }
    update: dict[str, Any] = {
        "intent_confidence": confidence,
        "llm_calls": llm_calls + outcome.calls,
        "messages": annotate(state, **{NLU: nlu}),
    }
    confident = confidence >= get_settings().INTENT_CONFIDENCE_THRESHOLD
    if primary in INTENTS and confident and other_intents:
        update["intent_queue"] = _queue_others(state, other_intents, result, dates)
    if primary in INTENTS and confident:
        if active is None:
            update.update({"active_intent": primary, "phase": "collect"})
        elif primary != active:
            update.update(_suspend(state, primary))
    return update
