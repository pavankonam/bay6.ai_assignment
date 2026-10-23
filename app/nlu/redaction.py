"""DOB redaction before any LLM call (NFR8a)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

DOB_PLACEHOLDER = "[DOB]"


def _surface_forms(dob: date) -> list[str]:
    """Common renderings of a known DOB (ISO, US numeric, month names)."""
    month_full = dob.strftime("%B")
    month_abbr = dob.strftime("%b")
    forms = {
        dob.isoformat(),
        f"{dob.month}/{dob.day}/{dob.year}",
        f"{dob.month:02d}/{dob.day:02d}/{dob.year}",
        f"{dob.month}-{dob.day}-{dob.year}",
        f"{dob.month:02d}-{dob.day:02d}-{dob.year}",
        f"{dob.month}/{dob.day}/{dob.year % 100:02d}",
        f"{dob.month:02d}/{dob.day:02d}/{dob.year % 100:02d}",
        f"{month_full} {dob.day} {dob.year}",
        f"{month_full} {dob.day}, {dob.year}",
        f"{month_abbr} {dob.day} {dob.year}",
        f"{month_abbr} {dob.day}, {dob.year}",
        f"{dob.day} {month_full} {dob.year}",
    }
    return sorted(forms, key=len, reverse=True)


def redact_known_dob(text: str, dob: date | None) -> str:
    if dob is None or not text:
        return text
    for form in _surface_forms(dob):
        text = re.sub(re.escape(form), DOB_PLACEHOLDER, text, flags=re.IGNORECASE)
    return text


def redactor_for(state: Mapping[str, Any]) -> Callable[[str], str]:
    """A redactor bound to the DOB known in `state` (if any)."""
    raw = (state.get("slots") or {}).get("dob")
    known = date.fromisoformat(raw) if isinstance(raw, str) else None

    def redact(text: str) -> str:
        return redact_known_dob(text, known)

    return redact
