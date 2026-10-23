"""Canonical payloads, `payload_hash` and write idempotency keys (FR27)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from typing import Any

CLAIM_FIELDS = (
    "policy_id",
    "incident_date",
    "incident_location",
    "incident_description",
    "injuries",
    "other_party_involved",
    "estimated_damage_usd",
    "police_report_number",
)
CANCEL_FIELDS = ("policy_id", "effective_date", "reason")
BUSINESS_FIELDS: dict[str, tuple[str, ...]] = {
    "create_claim": CLAIM_FIELDS,
    "cancel_policy": CANCEL_FIELDS,
}


def _canon(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canon(v) for v in value]
    return str(value)


def business_payload(action_type: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """Only the write tool's business arguments, canonicalized (no control args)."""
    return {name: _canon(values.get(name)) for name in BUSINESS_FIELDS[action_type]}


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(_canon(payload), sort_keys=True, separators=(",", ":"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def idempotency_key(
    thread_id: str, pending_action_id: str, action_type: str, payload: Mapping[str, Any]
) -> str:
    material = thread_id + pending_action_id + action_type + canonical_json(payload)
    return hashlib.sha256(material.encode()).hexdigest()
