"""Deterministic slot validators (FR13, Section 8.5). Invalid values are dropped with a reason."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.config import today
from app.validation.slots import CANCEL_REASONS

POLICY_ID_INPUT_RE = re.compile(r"^\s*POL[\s\-_]?(\d{6})\s*$", re.IGNORECASE)
POLICE_REPORT_RE = re.compile(r"^[A-Za-z0-9-]{3,30}$")
AMOUNT_RE = re.compile(
    r"^\s*(?:about|around|roughly|approximately|approx\.?|~)?\s*(?:usd)?\s*\$?\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|grand)?\s*(?:usd|dollars|bucks)?\s*$",
    re.IGNORECASE,
)
SKIP_VALUE = "skip"

POLICY_ID_ERROR = "Policy numbers look like POL-123456. Could you check yours?"
DOB_YEAR_ERROR = "Please include the year of your date of birth."
DOB_RANGE_ERROR = "That date of birth doesn't look right. Could you check it?"
INCIDENT_FUTURE_ERROR = "The incident date can't be in the future."
EFFECTIVE_PAST_ERROR = "Cancellations can only take effect today or later."
DATE_FORMAT_ERROR = "I couldn't read that date. Could you give it as MM/DD/YYYY?"
CANCELLATION_WINDOW_DAYS = 60
MIN_AGE, MAX_AGE = 16, 110

REASON_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"\bsold\b|\bselling\b|\bsell\b|got rid of", "sold_vehicle"),
    (
        r"switch|another (provider|insurer|company)|new (provider|insurer|company)|"
        r"better (rate|deal|offer)|found (a|another)|moving to|going with",
        "switching_provider",
    ),
    (r"expensive|cost|price|afford|cheaper|too much|premium", "cost"),
    (
        r"no longer need|don'?t need|not need|no need|not driving|don'?t drive|"
        r"stopped driving|moving abroad|no longer",
        "no_longer_needed",
    ),
    (r"\bother\b", "other"),
)


@dataclass(frozen=True)
class Checked:
    value: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def normalize_policy_id(raw: str) -> str | None:
    match = POLICY_ID_INPUT_RE.match(raw)
    return f"POL-{match.group(1)}" if match else None


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _as_date(raw: Any) -> date | None:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


def validate_policy_id(raw: Any) -> Checked:
    normalized = normalize_policy_id(str(raw)) if raw is not None else None
    return Checked(normalized) if normalized else Checked(error=POLICY_ID_ERROR)


def validate_dob(raw: Any) -> Checked:
    value = _as_date(raw)
    if value is None:
        return Checked(error=DATE_FORMAT_ERROR)
    if not MIN_AGE <= age_on(value, today()) <= MAX_AGE:
        return Checked(error=DOB_RANGE_ERROR)
    return Checked(value.isoformat())


def validate_incident_date(raw: Any) -> Checked:
    """≤ today. Dates > 365 days ago are kept and blocked by FR31 (TOO_OLD) after verification."""
    value = _as_date(raw)
    if value is None:
        return Checked(error=DATE_FORMAT_ERROR)
    if value > today():
        return Checked(error=INCIDENT_FUTURE_ERROR)
    return Checked(value.isoformat())


def validate_effective_date(raw: Any) -> Checked:
    value = _as_date(raw)
    if value is None:
        return Checked(error=DATE_FORMAT_ERROR)
    now = today()
    if value < now:
        return Checked(error=EFFECTIVE_PAST_ERROR)
    latest = now + timedelta(days=CANCELLATION_WINDOW_DAYS)
    if value > latest:
        return Checked(
            error=(
                f"Cancellations can be scheduled up to {CANCELLATION_WINDOW_DAYS} days ahead "
                f"(no later than {latest.isoformat()}). What date would you like?"
            )
        )
    return Checked(value.isoformat())


def _text(raw: Any, low: int, high: int, label: str) -> Checked:
    text = " ".join(str(raw).split()) if raw is not None else ""
    if not low <= len(text) <= high:
        return Checked(error=f"Please describe the {label} in {low}–{high} characters.")
    return Checked(text)


def validate_location(raw: Any) -> Checked:
    return _text(raw, 3, 200, "location")


def validate_description(raw: Any) -> Checked:
    return _text(raw, 5, 1000, "incident")


def parse_bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"yes", "y", "true", "yeah", "yep"}:
            return True
        if lowered in {"no", "n", "false", "nope", "none"}:
            return False
    return None


def validate_bool(raw: Any) -> Checked:
    value = parse_bool(raw)
    if value is None:
        return Checked(error="Please answer yes or no.")
    return Checked(value)


def parse_amount(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if not isinstance(raw, str):
        return None
    match = AMOUNT_RE.match(raw)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    if match.group(2):
        number *= 1000
    return number


def validate_damage(raw: Any) -> Checked:
    value = parse_amount(raw)
    if value is None or not 0 < value <= 1_000_000:
        return Checked(
            error=(
                "Please give the damage estimate as a dollar amount between $1 and "
                '$1,000,000 (for example, 2500), or say "skip".'
            )
        )
    return Checked(round(value, 2))


def validate_police_report(raw: Any) -> Checked:
    text = str(raw).strip() if raw is not None else ""
    if not POLICE_REPORT_RE.match(text):
        return Checked(
            error=(
                'Police report numbers are 3–30 letters, digits or dashes. You can also say "skip".'
            )
        )
    return Checked(text.upper())


def map_reason(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    normalized = text.replace(" ", "_").replace("-", "_")
    if normalized in CANCEL_REASONS:
        return normalized
    for pattern, reason in REASON_KEYWORDS:
        if re.search(pattern, text):
            return reason
    return None


def validate_reason(raw: Any) -> Checked:
    value = map_reason(raw)
    if value is None:
        return Checked(
            error=(
                "Please choose a reason: sold vehicle, switching provider, cost, "
                "no longer needed, or other."
            )
        )
    return Checked(value)


VALIDATORS = {
    "policy_id": validate_policy_id,
    "dob": validate_dob,
    "incident_date": validate_incident_date,
    "incident_location": validate_location,
    "incident_description": validate_description,
    "injuries": validate_bool,
    "other_party_involved": validate_bool,
    "estimated_damage_usd": validate_damage,
    "police_report_number": validate_police_report,
    "effective_date": validate_effective_date,
    "reason": validate_reason,
}


def validate_slot(name: str, raw: Any) -> Checked:
    return VALIDATORS[name](raw)
