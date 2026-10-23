"""Deterministic extractors: policy numbers and field-aware dates (FR12, FR12a).

Date contract (FR12a):
- every date span in the message becomes a `DateMention`;
- absolute dates: ISO and US-numeric forms are parsed directly (MM/DD[/YY[YY]]); month-name forms
  are parsed with `python-dateutil` using a default anchored to `APP_TODAY` (never the host clock);
- relative dates use the explicit grammar below, resolved against `APP_TODAY`;
- a missing year is resolved per role once the role is known;
- roles are assigned by (1) a label in the same clause, (2) the single date slot the agent last
  asked for, (3) plausibility among the roles relevant to the conversation, (4) the LLM's role
  if its text matches a span;
- if two dates claim one role, or a date has no role, nothing is guessed: an ambiguity is
  reported so the agent can ask.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from dateutil import parser as dateutil_parser

from app.validation.validators import DOB_RANGE_ERROR, DOB_YEAR_ERROR, MAX_AGE, MIN_AGE, age_on

ROLES = ("dob", "incident_date", "effective_date")
PAST_ROLES = frozenset({"incident_date"})
FUTURE_ROLES = frozenset({"effective_date"})
TODAY_ROLES = frozenset({"incident_date", "effective_date"})

# --- policy numbers ------------------------------------------------------------------------------

POLICY_ID_RE = re.compile(r"\bPOL[\s\-_]?(\d{6})(?!\d)", re.IGNORECASE)
BAD_POLICY_ID_RE = re.compile(r"\bPOL[\s\-_]?\d{1,5}\b|\bPOL[\s\-_]?\d{7,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class PolicyIdExtraction:
    values: list[str]
    invalid: list[str]


def extract_policy_ids(text: str) -> PolicyIdExtraction:
    values: list[str] = []
    for match in POLICY_ID_RE.finditer(text):
        normalized = f"POL-{match.group(1)}"
        if normalized not in values:
            values.append(normalized)
    invalid = [m.group(0) for m in BAD_POLICY_ID_RE.finditer(text)]
    return PolicyIdExtraction(values, invalid)


# --- dates ---------------------------------------------------------------------------------------

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}  # fmt: skip
WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5,
    "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
FULL_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip

_MONTH_ALT = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAY_ALT = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_NUM_ALT = r"\d+|" + "|".join(NUMBER_WORDS)
_ORD = r"(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s*(\d{4}|'\d{2}|\d{2}(?![\d/:])))?"

ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
NUMERIC_RE = re.compile(r"(?<![\d/\-])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}|\d{2}))?(?![\d/\-])")
MONTH_DAY_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}}){_ORD}\b(?:,?\s*(\d{{4}}|'\d{{2}})\b)?", re.IGNORECASE
)
DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_ALT})\b\.?(?:,?\s*(\d{{4}}|'\d{{2}})\b)?",
    re.IGNORECASE,
)
ORDINAL_DAY_RE = re.compile(r"\bthe\s+(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)
RELATIVE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ago", re.compile(rf"\b({_NUM_ALT})\s+(day|days|week|weeks)\s+ago\b", re.IGNORECASE)),
    ("in", re.compile(rf"\bin\s+({_NUM_ALT})\s+(day|days|week|weeks)\b", re.IGNORECASE)),
    ("last", re.compile(rf"\b(?:last|this\s+past)\s+({_WEEKDAY_ALT})\b", re.IGNORECASE)),
    ("next", re.compile(rf"\bnext\s+({_WEEKDAY_ALT})\b", re.IGNORECASE)),
    ("today", re.compile(r"\btoday\b", re.IGNORECASE)),
    ("yesterday", re.compile(r"\byesterday\b", re.IGNORECASE)),
    ("tomorrow", re.compile(r"\btomorrow\b", re.IGNORECASE)),
    ("weekday", re.compile(rf"\b({'|'.join(FULL_WEEKDAYS)})\b", re.IGNORECASE)),
)

LABELS: dict[str, re.Pattern[str]] = {
    "dob": re.compile(
        r"\b(born|dob|d\.o\.b\.?|birthday|birth\s*date|date\s+of\s+birth|birth)\b", re.IGNORECASE
    ),
    "incident_date": re.compile(
        r"\b(accident|incident|crash(?:ed)?|happened|hit|rear[- ]ended|collision|collided|"
        r"occurred|damaged|stolen|vandali[sz]ed|loss)\b",
        re.IGNORECASE,
    ),
    "effective_date": re.compile(
        r"\b(cancel\w*|effective|starting|start|as\s+of|end\s+(?:it|my\s+policy|coverage))\b",
        re.IGNORECASE,
    ),
}
# A date the customer explicitly rejects ("the 20th, not yesterday") is not a candidate value.
_NEGATED_BEFORE_RE = re.compile(
    r"\b(?:not|never|instead of|rather than|wasn'?t|isn'?t)\s+(?:on\s+|the\s+)?$", re.IGNORECASE
)
_CLAUSE_SPLIT_RE = re.compile(r"[;!?\n]|\.(?=\s|$)|,|\bbut\b|\band\b|\bwhile\b", re.IGNORECASE)


@dataclass
class DateMention:
    """One date span. Either absolute (`month`/`day`/`year`) or relative (`relative`)."""

    text: str
    start: int
    end: int
    month: int | None = None
    day: int | None = None
    year: int | None = None  # four-digit year as written
    two_digit_year: int | None = None
    relative: date | None = None
    relative_roles: frozenset[str] | None = None
    role: str | None = None
    role_source: str | None = None
    value: date | None = None
    error: str | None = None
    candidate_roles: list[str] = field(default_factory=list)

    @property
    def has_year(self) -> bool:
        return self.year is not None or self.two_digit_year is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "role": self.role,
            "role_source": self.role_source,
            "value": self.value.isoformat() if self.value else None,
            "error": self.error,
        }


def _valid_month_day(month: int, day: int) -> bool:
    try:
        date(2000, month, day)  # leap year accepts Feb 29
    except ValueError:
        return False
    return True


def _year_parts(raw: str | None) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    digits = raw.lstrip("'")
    if len(digits) == 4:
        return int(digits), None
    return None, int(digits)


def _overlaps(start: int, end: int, taken: Sequence[tuple[int, int]]) -> bool:
    return any(start < t_end and t_start < end for t_start, t_end in taken)


def _number(token: str) -> int:
    lowered = token.lower()
    return NUMBER_WORDS[lowered] if lowered in NUMBER_WORDS else int(token)


def _last_weekday(weekday: int, today: date) -> date:
    delta = (today.weekday() - weekday) % 7 or 7
    return today - timedelta(days=delta)


def _next_weekday(weekday: int, today: date) -> date:
    delta = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def _relative(kind: str, match: re.Match[str], today: date) -> tuple[date, frozenset[str]]:
    if kind == "today":
        return today, TODAY_ROLES
    if kind == "yesterday":
        return today - timedelta(days=1), PAST_ROLES
    if kind == "tomorrow":
        return today + timedelta(days=1), FUTURE_ROLES
    if kind in ("ago", "in"):
        amount = _number(match.group(1))
        days = amount * (7 if match.group(2).lower().startswith("week") else 1)
        if kind == "ago":
            return today - timedelta(days=days), PAST_ROLES
        return today + timedelta(days=days), FUTURE_ROLES
    weekday = WEEKDAYS[match.group(1).lower()]
    if kind == "next":
        return _next_weekday(weekday, today), FUTURE_ROLES
    return _last_weekday(weekday, today), PAST_ROLES  # "last", "this past", bare weekday


def _month_day_via_dateutil(month_text: str, day: int, today: date) -> tuple[int, int] | None:
    """Parse "Month Day" with dateutil, anchored to APP_TODAY (never the host clock)."""
    try:
        parsed = dateutil_parser.parse(
            f"{month_text.rstrip('.')} {day}",
            default=datetime(today.year, today.month, 1),
            dayfirst=False,
        )
    except (ValueError, OverflowError):
        return None
    month = MONTHS.get(month_text.lower().rstrip("."))
    if month is None or parsed.month != month or not _valid_month_day(month, day):
        return None
    return month, day


def find_date_mentions(text: str, today: date) -> list[DateMention]:
    """Every date span in `text` (no roles yet), in reading order."""
    found: list[DateMention] = []
    taken: list[tuple[int, int]] = []

    def add(mention: DateMention) -> None:
        if not _overlaps(mention.start, mention.end, taken):
            found.append(mention)
            taken.append((mention.start, mention.end))

    for m in ISO_RE.finditer(text):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_month_day(month, day):
            add(DateMention(m.group(0), m.start(), m.end(), month, day, year))
    for regex, month_group, day_group in ((MONTH_DAY_RE, 1, 2), (DAY_MONTH_RE, 2, 1)):
        for m in regex.finditer(text):
            parsed = _month_day_via_dateutil(m.group(month_group), int(m.group(day_group)), today)
            if parsed is None:
                continue
            full_year, two_digit = _year_parts(m.group(3))
            add(
                DateMention(
                    m.group(0), m.start(), m.end(), parsed[0], parsed[1], full_year, two_digit
                )
            )
    for m in NUMERIC_RE.finditer(text):
        month, day = int(m.group(1)), int(m.group(2))
        if not _valid_month_day(month, day):
            continue
        full_year, two_digit = _year_parts(m.group(3))
        add(DateMention(m.group(0), m.start(), m.end(), month, day, full_year, two_digit))
    for m in ORDINAL_DAY_RE.finditer(text):
        day = int(m.group(1))
        if 1 <= day <= 31:
            add(DateMention(m.group(0), m.start(), m.end(), None, day))
    for kind, regex in RELATIVE_RES:
        for m in regex.finditer(text):
            value, roles = _relative(kind, m, today)
            add(DateMention(m.group(0), m.start(), m.end(), relative=value, relative_roles=roles))
    found.sort(key=lambda d: d.start)
    return [m for m in found if not _NEGATED_BEFORE_RE.search(text[max(0, m.start - 20) : m.start])]


# --- missing-year resolution ------------------------------------------------------------------


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _most_recent(month: int | None, day: int, today: date) -> date | None:
    """Most recent occurrence on or before today (day-only mentions search back by month)."""
    if month is not None:
        for year in (today.year, today.year - 1, today.year - 2, today.year - 3, today.year - 4):
            candidate = _safe_date(year, month, day)
            if candidate is not None and candidate <= today:
                return candidate
        return None
    year, mon = today.year, today.month
    for _ in range(13):
        candidate = _safe_date(year, mon, day)
        if candidate is not None and candidate <= today:
            return candidate
        mon -= 1
        if mon == 0:
            year, mon = year - 1, 12
    return None


def _next_occurrence(month: int | None, day: int, today: date) -> date | None:
    """Next occurrence on or after today."""
    if month is not None:
        for year in (today.year, today.year + 1, today.year + 2, today.year + 3, today.year + 4):
            candidate = _safe_date(year, month, day)
            if candidate is not None and candidate >= today:
                return candidate
        return None
    year, mon = today.year, today.month
    for _ in range(13):
        candidate = _safe_date(year, mon, day)
        if candidate is not None and candidate >= today:
            return candidate
        mon += 1
        if mon == 13:
            year, mon = year + 1, 1
    return None


@dataclass(frozen=True)
class Resolution:
    value: date | None = None
    error: str | None = None
    ambiguous: bool = False


def resolve(mention: DateMention, role: str, today: date) -> Resolution:
    """Resolve `mention` as `role` (missing-year and two-digit-year rules of FR12a)."""
    if mention.relative is not None:
        roles = mention.relative_roles or frozenset()
        if role not in roles:
            return Resolution(error=f"'{mention.text}' can't be used as the {_label(role)}.")
        return Resolution(mention.relative)
    assert mention.day is not None
    if role == "dob":
        return _resolve_dob(mention, today)
    if mention.year is not None:
        assert mention.month is not None
        value = _safe_date(mention.year, mention.month, mention.day)
        return Resolution(value) if value else Resolution(error="That date doesn't exist.")
    if mention.two_digit_year is not None:
        assert mention.month is not None
        value = _safe_date(2000 + mention.two_digit_year, mention.month, mention.day)
        return Resolution(value) if value else Resolution(error="That date doesn't exist.")
    if role == "incident_date":
        return Resolution(_most_recent(mention.month, mention.day, today))
    return Resolution(_next_occurrence(mention.month, mention.day, today))


def _resolve_dob(mention: DateMention, today: date) -> Resolution:
    if not mention.has_year or mention.month is None or mention.day is None:
        return Resolution(error=DOB_YEAR_ERROR)
    if mention.year is not None:
        value = _safe_date(mention.year, mention.month, mention.day)
        return Resolution(value) if value else Resolution(error=DOB_RANGE_ERROR)
    assert mention.two_digit_year is not None
    fits: list[date] = []
    for century in (1900, 2000):
        candidate = _safe_date(century + mention.two_digit_year, mention.month, mention.day)
        if candidate is not None and MIN_AGE <= age_on(candidate, today) <= MAX_AGE:
            fits.append(candidate)
    if len(fits) == 1:
        return Resolution(fits[0])
    if not fits:
        return Resolution(error=DOB_RANGE_ERROR)
    return Resolution(
        ambiguous=True, error="Which century is that year in? Please give all four digits."
    )


def _label(role: str) -> str:
    return {"dob": "date of birth", "incident_date": "incident date"}.get(role, "effective date")


# --- role assignment ----------------------------------------------------------------------------


def _plausible(mention: DateMention, role: str, today: date) -> bool:
    resolution = resolve(mention, role, today)
    if resolution.value is None:
        return False
    value = resolution.value
    if role == "dob":
        return MIN_AGE <= age_on(value, today) <= MAX_AGE
    if role == "incident_date":
        return today - timedelta(days=365) <= value <= today
    return today <= value <= today + timedelta(days=60)


def _clause_bounds(text: str, mentions: Sequence[DateMention]) -> list[tuple[int, int]]:
    """Clause spans, computed with date spans masked so commas inside dates don't split."""
    masked = list(text)
    for m in mentions:
        for i in range(m.start, m.end):
            masked[i] = "x"
    flat = "".join(masked)
    bounds: list[tuple[int, int]] = []
    start = 0
    for sep in _CLAUSE_SPLIT_RE.finditer(flat):
        bounds.append((start, sep.start()))
        start = sep.end()
    bounds.append((start, len(text)))
    return bounds


def _label_role(text: str, mention: DateMention, clauses: Sequence[tuple[int, int]]) -> str | None:
    """Step 1: the nearest role label inside the mention's clause."""
    for c_start, c_end in clauses:
        if c_start <= mention.start < c_end or (c_start <= mention.start and mention.end <= c_end):
            clause = text[c_start:c_end]
            best: tuple[int, str] | None = None
            for role, pattern in LABELS.items():
                for match in pattern.finditer(clause):
                    pos = c_start + match.start()
                    if mention.start <= pos < mention.end:
                        continue
                    distance = min(abs(pos - mention.start), abs(pos - mention.end))
                    if best is None or distance < best[0]:
                        best = (distance, role)
            return best[1] if best else None
    return None


@dataclass
class DateAssignment:
    mentions: list[DateMention]
    ambiguity: dict[str, object] | None = None

    def by_role(self) -> dict[str, DateMention]:
        return {m.role: m for m in self.mentions if m.role is not None}


def assign_roles(
    text: str,
    mentions: Sequence[DateMention],
    *,
    today: date,
    asked_date_slots: Collection[str] = (),
    relevant_roles: Collection[str] = ROLES,
    llm_roles: Iterable[tuple[str, str]] = (),
) -> DateAssignment:
    """Apply FR12a steps 1–4, then resolve values and detect ambiguity."""
    working = [replace(m, candidate_roles=[]) for m in mentions]
    clauses = _clause_bounds(text, working)
    relevant = [r for r in ROLES if r in relevant_roles]
    asked = [r for r in ROLES if r in asked_date_slots]
    llm_lookup = {span.strip().lower(): role for span, role in llm_roles if role in ROLES}
    for mention in working:
        label = _label_role(text, mention, clauses)
        if label is not None:
            mention.role, mention.role_source = label, "label"
            continue
        if len(asked) == 1:
            mention.role, mention.role_source = asked[0], "asked"
            continue
        fits = [r for r in relevant if _plausible(mention, r, today)]
        mention.candidate_roles = fits
        if len(fits) == 1:
            mention.role, mention.role_source = fits[0], "plausibility"
            continue
        llm_role = llm_lookup.get(mention.text.strip().lower())
        if llm_role is not None and (not fits or llm_role in fits):
            mention.role, mention.role_source = llm_role, "llm"
    for mention in working:
        if mention.role is not None:
            resolution = resolve(mention, mention.role, today)
            mention.value, mention.error = resolution.value, resolution.error
    return DateAssignment(working, _ambiguity(working, relevant))


def _ambiguity(
    mentions: Sequence[DateMention], relevant: Sequence[str]
) -> dict[str, object] | None:
    roles: dict[str, list[DateMention]] = {}
    for mention in mentions:
        if mention.role is not None:
            roles.setdefault(mention.role, []).append(mention)
    for role, claimed in roles.items():
        if len(claimed) > 1:
            return {"kind": "same_role", "role": role, "dates": [m.text for m in claimed]}
    unassigned = [m for m in mentions if m.role is None]
    if unassigned:
        options = unassigned[0].candidate_roles or list(relevant)
        return {
            "kind": "no_role",
            "role": None,
            "dates": [m.text for m in unassigned],
            "options": options,
        }
    return None


def extract_dates(
    text: str,
    *,
    today: date,
    asked_date_slots: Collection[str] = (),
    relevant_roles: Collection[str] = ROLES,
    llm_roles: Iterable[tuple[str, str]] = (),
) -> DateAssignment:
    return assign_roles(
        text,
        find_date_mentions(text, today),
        today=today,
        asked_date_slots=asked_date_slots,
        relevant_roles=relevant_roles,
        llm_roles=llm_roles,
    )


def dob_spans(text: str, today: date, known_dob: date | None = None) -> list[tuple[int, int]]:
    """Spans the deterministic extractor recognizes as the DOB (for NFR8a redaction)."""
    assignment = extract_dates(text, today=today)
    spans: list[tuple[int, int]] = []
    for mention in assignment.mentions:
        is_dob_role = mention.role == "dob"
        matches_known = False
        if known_dob is not None and mention.relative is None:
            matches_known = resolve(mention, "dob", today).value == known_dob
        if is_dob_role or matches_known or "dob" in mention.candidate_roles:
            spans.append((mention.start, mention.end))
    return spans
