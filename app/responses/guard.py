"""Post-checks for LLM-written replies (FR50, FR51).

(a) Fact check: every reference ID, date and dollar amount in the reply must appear in the facts.
(b) Coverage/payment-language filter: no statement or implication that something will be covered
    or paid. Either failure → the caller uses the outcome's template instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.observability.metrics import metrics

REFERENCE_ID_RE = re.compile(r"\b(?:POL-\d{6}|CLM-\d{4}-\d{6}|CNL-\d{4}-\d{6}|APR-[A-Z2-7]{5})\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\s+(\d{1,2}),?\s+(\d{4})\b"
)
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
COVERAGE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(will|is going to|should|would)\b.{0,40}\b(be )?(covered|paid|reimbursed|"
        r"approved for payment)\b",
        r"\bguarantee[sd]?\b",
        r"\byou(?:'re| are) covered for this\b",
        r"\bwe(?:'ll| will) pay\b",
        r"\bfully covered\b",
        r"\byou(?:'ll| will) (?:get|receive) (?:paid|reimbursed|a payout)\b",
    )
)


def _flatten(value: Any, out: set[str]) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _flatten(item, out)
    elif isinstance(value, list | tuple):
        for item in value:
            _flatten(item, out)
    elif value is not None:
        out.add(str(value))


def _fact_numbers(values: set[str]) -> set[float]:
    numbers: set[float] = set()
    for text in values:
        try:
            numbers.add(round(float(text), 2))
        except ValueError:
            continue
    return numbers


def facts_supported(text: str, facts: Mapping[str, Any]) -> bool:
    values: set[str] = set()
    _flatten(facts, values)
    blob = " ".join(values) + " " + json.dumps(facts, default=str)
    for ref in REFERENCE_ID_RE.findall(text):
        if ref not in blob:
            return False
    dates = set(ISO_DATE_RE.findall(text))
    for month, day, year in LONG_DATE_RE.findall(text):
        dates.add(datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat())
    if any(d not in blob for d in dates):
        return False
    numbers = _fact_numbers(values)
    for match in MONEY_RE.finditer(text):
        amount = round(float(match.group(1).replace(",", "")), 2)
        # Grounded if it is a numeric fact, or appears verbatim in fact text (customer wording).
        if amount not in numbers and match.group(0) not in blob:
            return False
    return True


def coverage_language(text: str) -> bool:
    return any(p.search(text) for p in COVERAGE_PATTERNS)


def check_response(text: str, facts: Mapping[str, Any]) -> str | None:
    """None if the reply passes both checks, else the name of the failed check."""
    if not facts_supported(text, facts):
        metrics.inc("response_guard_fallbacks_total", check="facts")
        return "facts"
    if coverage_language(text):
        metrics.inc("response_guard_fallbacks_total", check="coverage")
        return "coverage"
    return None
