"""Slot schemas per intent (Section 8.5)."""

from __future__ import annotations

from dataclasses import dataclass

IDENTITY_SLOTS: tuple[str, ...] = ("policy_id", "dob")
DATE_SLOTS: frozenset[str] = frozenset({"dob", "incident_date", "effective_date"})


@dataclass(frozen=True)
class SlotSpec:
    name: str
    required: bool
    ask_order: int


IDENTITY_SPECS: tuple[SlotSpec, ...] = (
    SlotSpec("policy_id", True, 1),
    SlotSpec("dob", True, 1),
)

INTENT_SLOTS: dict[str, tuple[SlotSpec, ...]] = {
    "policy_status": (),
    "report_claim": (
        SlotSpec("incident_date", True, 2),
        SlotSpec("incident_location", True, 3),
        SlotSpec("incident_description", True, 3),
        SlotSpec("injuries", True, 4),
        SlotSpec("other_party_involved", True, 4),
        SlotSpec("estimated_damage_usd", False, 5),
        SlotSpec("police_report_number", False, 5),
    ),
    "cancel_policy": (
        SlotSpec("effective_date", True, 2),
        SlotSpec("reason", True, 2),
    ),
}

TASK_DATE_SLOT: dict[str, str] = {
    "report_claim": "incident_date",
    "cancel_policy": "effective_date",
}

CANCEL_REASONS: tuple[str, ...] = (
    "sold_vehicle",
    "switching_provider",
    "cost",
    "no_longer_needed",
    "other",
)

SLOT_LABELS: dict[str, str] = {
    "policy_id": "policy number",
    "dob": "date of birth",
    "incident_date": "incident date",
    "incident_location": "location",
    "incident_description": "description",
    "injuries": "injuries",
    "other_party_involved": "other party involved",
    "estimated_damage_usd": "damage estimate",
    "police_report_number": "police report number",
    "effective_date": "effective date",
    "reason": "reason",
}


def task_specs(intent: str | None) -> tuple[SlotSpec, ...]:
    return INTENT_SLOTS.get(intent or "", ())


def task_slot_names(intent: str | None) -> tuple[str, ...]:
    return tuple(spec.name for spec in task_specs(intent))


def all_specs(intent: str | None) -> tuple[SlotSpec, ...]:
    return IDENTITY_SPECS + task_specs(intent)


def optional_slot_names(intent: str | None) -> frozenset[str]:
    return frozenset(spec.name for spec in task_specs(intent) if not spec.required)


def relevant_date_roles(intent: str | None) -> frozenset[str]:
    """Date roles that can appear in a conversation about `intent` (all three if unknown)."""
    if intent in TASK_DATE_SLOT:
        return frozenset({"dob", TASK_DATE_SLOT[intent]})
    if intent == "policy_status":
        return frozenset({"dob"})
    return DATE_SLOTS
