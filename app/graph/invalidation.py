"""FR29a invalidation matrix: what an edit or correction to each slot discards."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

# Slot → extra state reset beyond discarding `pending_action` (which every change does).
_MATRIX: dict[str, dict[str, Any]] = {
    "policy_id": {
        "verified": False,
        "verified_policy_id": None,
        "policy_snapshot": None,
        "quote": None,
        "claim_checks": None,
        "duplicate_override": False,
    },
    "dob": {"verified": False},
    "incident_date": {"claim_checks": None, "duplicate_override": False},
    "effective_date": {"quote": None},
    # injuries / estimated_damage_usd: approval requirement is recomputed by prepare_action.
    # reason, incident_location, incident_description, other_party_involved,
    # police_report_number: payload only.
}


def invalidation_updates(changed_slots: Collection[str]) -> dict[str, Any]:
    """State updates for a set of changed slots. Empty input → no updates."""
    if not changed_slots:
        return {}
    updates: dict[str, Any] = {"pending_action": None}
    for slot in changed_slots:
        updates.update(_MATRIX.get(slot, {}))
    return updates
