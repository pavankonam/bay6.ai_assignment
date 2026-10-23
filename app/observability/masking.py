"""PII masking for logs, traces and reviewer payloads (NFR8, NFR9)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

POLICY_ID_RE = re.compile(r"\bPOL-(\d{3})(\d{3})\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

DOB_KEYS = frozenset({"dob", "date_of_birth"})
NAME_KEYS = frozenset({"holder_name", "name", "holder_first_name", "holder_last_name"})
POLICY_KEYS = frozenset({"policy_id", "verified_policy_id"})
SECRET_KEYS = frozenset(
    {"anthropic_api_key", "reviewer_token", "admin_token", "langsmith_api_key", "x-admin-token"}
)


def mask_dob(value: date | str | None) -> str:
    """`1985-04-12` → `****-**-12` (day only)."""
    if value is None:
        return ""
    text = value.isoformat() if isinstance(value, date) else str(value)
    match = ISO_DATE_RE.search(text)
    if match:
        return f"****-**-{match.group(3)}"
    return "****-**-**"


def mask_name(name: str | None) -> str:
    """`Jane Doe` → `J*** D**`."""
    if not name:
        return ""
    return " ".join(part[0] + "*" * (len(part) - 1) for part in name.split() if part)


def mask_policy_id(policy_id: str | None) -> str:
    """`POL-100001` → `POL-***001`."""
    if not policy_id:
        return ""
    match = POLICY_ID_RE.fullmatch(policy_id)
    if match:
        return f"POL-***{match.group(2)}"
    return "POL-******"


def mask_policy_ids_in_text(text: str) -> str:
    return POLICY_ID_RE.sub(lambda m: f"POL-***{m.group(2)}", text)


def mask_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in SECRET_KEYS:
        return "***"
    if lowered in DOB_KEYS:
        return mask_dob(value) if isinstance(value, date | str) else value
    if lowered in NAME_KEYS and isinstance(value, str):
        return mask_name(value)
    if lowered in POLICY_KEYS and isinstance(value, str):
        return mask_policy_id(value)
    return mask_structure(value)


def mask_structure(value: Any) -> Any:
    """Recursively mask a log/trace payload."""
    if isinstance(value, Mapping):
        return {str(k): mask_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_structure(v) for v in value]
    if isinstance(value, str):
        return mask_policy_ids_in_text(value)
    return value
