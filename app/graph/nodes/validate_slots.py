"""`validate_slots`: merge + validate candidates, apply corrections, compute what is missing,
and run verification-gated claim checks (FR6a, FR12, FR13, FR16, FR21, FR29a, FR31, FR33, FR47)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, cast

from langchain_core.messages import HumanMessage

from app.config import today
from app.graph.invalidation import invalidation_updates
from app.graph.messages import NLU, annotate, annotation, last_asked_slots, latest_human_text
from app.graph.nodes.base import graph_node
from app.graph.phases import tool_phase
from app.graph.state import ClaimChecks, Phase
from app.nlu.rules import is_skip
from app.rules import policy_in_force_on
from app.tools.definitions import find_open_claims
from app.validation.slots import (
    IDENTITY_SLOTS,
    all_specs,
    optional_slot_names,
    task_slot_names,
)
from app.validation.validators import SKIP_VALUE, validate_slot

CLAIM_WINDOW_DAYS = 365


def _merge_candidates(
    state: Mapping[str, Any],
    nlu: Mapping[str, Any],
    slots: dict[str, Any],
    feedback: dict[str, str],
    skipped: list[str],
) -> set[str]:
    """Merge this turn's candidates (once per turn). Returns the slots that were corrected."""
    intent = state.get("active_intent")
    allowed = set(IDENTITY_SLOTS) | set(task_slot_names(intent))
    optional = optional_slot_names(intent)
    candidates: dict[str, Any] = dict(nlu.get("identity") or {})
    if nlu.get("task_intent") == intent:
        candidates.update(nlu.get("task") or {})
    changed: set[str] = set()
    for name, raw in candidates.items():
        if name not in allowed:
            continue
        if raw == SKIP_VALUE:
            if name in optional:
                slots[name] = None
                if name not in skipped:
                    skipped.append(name)
            continue
        checked = validate_slot(name, raw)
        if not checked.ok:
            assert checked.error is not None
            feedback[name] = checked.error
            continue
        feedback.pop(name, None)
        current = slots.get(name)
        if current is None:
            slots[name] = checked.value
            if name in skipped:
                skipped.remove(name)
        elif current != checked.value and (nlu.get("is_correction") or name == "policy_id"):
            # FR12 correction / FR21 different policy → overwrite with FR29a invalidation.
            slots[name] = checked.value
            changed.add(name)
    for name, message in (nlu.get("date_errors") or {}).items():
        if name in allowed:
            feedback[name] = message
    asked = last_asked_slots(state)
    text = latest_human_text(state)
    for name in optional:
        # FR16: optional slots are asked once; "skip"/"no"/"none", or no answer, records null.
        unanswered = slots.get(name) is None and name not in feedback
        if name in asked and unanswered and (is_skip(text) or name not in candidates):
            slots[name] = None
            if name not in skipped:
                skipped.append(name)
    return changed


def _carried_over(state: Mapping[str, Any]) -> tuple[HumanMessage, dict[str, Any]] | None:
    """The previous message's extraction, if that turn stopped to disambiguate a date (FR12a):
    its unambiguous values (identity, other slots) are merged now instead of being lost."""
    humans = [m for m in state.get("messages") or [] if isinstance(m, HumanMessage)]
    if len(humans) < 2:
        return None
    previous = humans[-2]
    nlu = previous.additional_kwargs.get(NLU)
    if isinstance(nlu, Mapping) and not nlu.get("merged") and nlu.get("date_ambiguity"):
        return previous, dict(nlu)
    return None


def _revalidate(intent: str | None, slots: dict[str, Any], feedback: dict[str, str]) -> None:
    """Re-check every held value (restored tasks, values that aged out, e.g. a past date)."""
    for spec in all_specs(intent):
        value = slots.get(spec.name)
        if value is None:
            continue
        checked = validate_slot(spec.name, value)
        if checked.ok:
            slots[spec.name] = checked.value
        else:
            slots.pop(spec.name)
            assert checked.error is not None
            feedback[spec.name] = checked.error


def _missing(intent: str | None, slots: Mapping[str, Any], skipped: list[str]) -> list[str]:
    missing: list[str] = []
    for spec in sorted(all_specs(intent), key=lambda s: s.ask_order):
        required_missing = spec.required and slots.get(spec.name) is None
        optional_unasked = not spec.required and spec.name not in slots and spec.name not in skipped
        if required_missing or optional_unasked:
            missing.append(spec.name)
    return missing


def _phase(state: Mapping[str, Any], missing: list[str]) -> Phase:
    if any(slot in IDENTITY_SLOTS for slot in missing):
        return "collect"
    read_phase = tool_phase(state)
    if read_phase in ("verify", "lookup"):
        return read_phase
    if missing:
        return "collect"
    if read_phase == "quote":
        quote = state.get("quote") or {}
        if quote.get("effective_date") != (state.get("slots") or {}).get("effective_date"):
            return "quote"
    return "confirm"


def _claim_checks(state: Mapping[str, Any]) -> ClaimChecks | None:
    """FR31 + FR33, only after verification and lookup for this policy."""
    slots = state.get("slots") or {}
    snapshot = state.get("policy_snapshot") or {}
    verified_policy = state.get("verified_policy_id")
    incident = slots.get("incident_date")
    if not (
        state.get("active_intent") == "report_claim"
        and state.get("verified")
        and verified_policy == slots.get("policy_id")
        and snapshot.get("policy_id") == verified_policy
        and incident
    ):
        return None
    existing = state.get("claim_checks")
    if existing and existing.get("incident_date") == incident:
        return cast(ClaimChecks, existing)
    day = date.fromisoformat(incident)
    now = today()
    reason: str | None = None
    if day > now:
        reason = "FUTURE_DATE"
    elif day < now - timedelta(days=CLAIM_WINDOW_DAYS):
        reason = "TOO_OLD"
    elif not policy_in_force_on(
        snapshot.get("status_history") or [], day, snapshot.get("cancellation_effective_date")
    ):
        reason = "NOT_IN_FORCE"
    duplicate: str | None = None
    if reason is None:
        assert verified_policy is not None
        open_claims = find_open_claims(verified_policy, day)
        duplicate = open_claims[0]["claim_id"] if open_claims else None
    return {
        "incident_date": incident,
        "eligible": reason is None,
        "reason": reason,
        "duplicate_claim_id": duplicate,
    }


@graph_node("validate_slots")
def validate_slots(state: Mapping[str, Any]) -> dict[str, Any]:
    intent = state.get("active_intent")
    slots = dict(state.get("slots") or {})
    feedback = dict(state.get("invalid_slot_feedback") or {})
    skipped = list(state.get("slots_skipped") or [])
    update: dict[str, Any] = {"status": "in_progress"}
    nlu = annotation(state, NLU)
    changed: set[str] = set()
    if nlu and not nlu.get("merged"):
        carried = _carried_over(state)
        replacements = []
        if carried is not None:
            message, previous = carried
            changed |= _merge_candidates(state, previous, slots, feedback, skipped)
            kwargs = {**message.additional_kwargs, NLU: {**previous, "merged": True}}
            replacements.append(
                HumanMessage(content=message.content, id=message.id, additional_kwargs=kwargs)
            )
        changed |= _merge_candidates(state, nlu, slots, feedback, skipped)
        update["messages"] = replacements + annotate(state, **{NLU: {**nlu, "merged": True}})
        if state.get("status") == "failed_retryable" and state.get("resume_step") == "read_tool":
            update.update({"error": None, "resume_step": None})  # FR47 read retry
    _revalidate(intent, slots, feedback)
    update.update(invalidation_updates(changed))
    missing = _missing(intent, slots, skipped)
    for name in list(feedback):
        if name in slots and slots[name] is not None:
            feedback.pop(name)
    update.update(
        {
            "slots": slots,
            "slots_skipped": skipped,
            "invalid_slot_feedback": feedback,
            "missing_slots": missing,
        }
    )
    merged_state = {**state, **update}
    update["phase"] = _phase(merged_state, missing)
    checks = _claim_checks(merged_state)
    update["claim_checks"] = checks
    if checks is not None and not checks["eligible"]:
        update["last_outcome"] = "CLAIM_INELIGIBLE"
    return update
