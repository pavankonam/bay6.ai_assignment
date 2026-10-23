"""M3: deterministic extractors — policy numbers and the FR12a date contract."""

from __future__ import annotations

from datetime import date

import pytest

from app.validation.extractors import (
    extract_dates,
    extract_policy_ids,
    find_date_mentions,
    resolve,
)

TODAY = date(2026, 10, 22)  # a Thursday


# --- policy numbers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("POL-100001", ["POL-100001"]),
        ("my policy is pol100001", ["POL-100001"]),
        ("POL 100001 please", ["POL-100001"]),
        ("pol_100001", ["POL-100001"]),
        ("POL-100001 and POL-100002", ["POL-100001", "POL-100002"]),
        ("POL-100001 POL-100001", ["POL-100001"]),
        ("no policy here", []),
    ],
)
def test_extract_policy_ids(text: str, expected: list[str]) -> None:
    assert extract_policy_ids(text).values == expected


def test_extract_invalid_policy_ids() -> None:
    assert extract_policy_ids("POL-12345").invalid == ["POL-12345"]
    assert extract_policy_ids("POL-1234567").invalid == ["POL-1234567"]
    assert extract_policy_ids("POL-1234567").values == []


# --- relative grammar: every phrase --------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected", "roles"),
    [
        ("today", date(2026, 10, 22), {"incident_date", "effective_date"}),
        ("yesterday", date(2026, 10, 21), {"incident_date"}),
        ("3 days ago", date(2026, 10, 19), {"incident_date"}),
        ("a day ago", date(2026, 10, 21), {"incident_date"}),
        ("two weeks ago", date(2026, 10, 8), {"incident_date"}),
        ("1 week ago", date(2026, 10, 15), {"incident_date"}),
        ("last tuesday", date(2026, 10, 20), {"incident_date"}),
        ("last Thursday", date(2026, 10, 15), {"incident_date"}),  # strictly before today
        ("this past friday", date(2026, 10, 16), {"incident_date"}),
        ("Monday", date(2026, 10, 19), {"incident_date"}),  # bare weekday = last weekday
        ("thursday", date(2026, 10, 15), {"incident_date"}),
        ("tomorrow", date(2026, 10, 23), {"effective_date"}),
        ("next friday", date(2026, 10, 23), {"effective_date"}),
        ("next thursday", date(2026, 10, 29), {"effective_date"}),
        ("in 10 days", date(2026, 11, 1), {"effective_date"}),
        ("in two weeks", date(2026, 11, 5), {"effective_date"}),
    ],
)
def test_relative_grammar(phrase: str, expected: date, roles: set[str]) -> None:
    (mention,) = find_date_mentions(f"it was {phrase} ok", TODAY)
    assert mention.relative == expected
    assert mention.relative_roles == roles
    for role in roles:
        assert resolve(mention, role, TODAY).value == expected
    forbidden = {"incident_date", "effective_date", "dob"} - roles
    for role in forbidden:
        assert resolve(mention, role, TODAY).value is None


# --- absolute formats ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "month", "day", "year", "two_digit"),
    [
        ("2026-10-21", 10, 21, 2026, None),
        ("4/12/1985", 4, 12, 1985, None),
        ("04/12/85", 4, 12, None, 85),
        ("4-12-1985", 4, 12, 1985, None),
        ("April 12 1985", 4, 12, 1985, None),
        ("April 12, 1985", 4, 12, 1985, None),
        ("apr 12th", 4, 12, None, None),
        ("12 April 1985", 4, 12, 1985, None),
        ("the 3rd of November", 11, 3, None, None),
        ("Nov. 1", 11, 1, None, None),
        ("Sept 5 '99", 9, 5, None, 99),
        ("12/30", 12, 30, None, None),
    ],
)
def test_absolute_formats(
    text: str, month: int, day: int, year: int | None, two_digit: int | None
) -> None:
    (mention,) = find_date_mentions(text, TODAY)
    assert (mention.month, mention.day, mention.year, mention.two_digit_year) == (
        month,
        day,
        year,
        two_digit,
    )


def test_ambiguous_numeric_is_us_format() -> None:
    (mention,) = find_date_mentions("04/05/1985", TODAY)
    assert (mention.month, mention.day) == (4, 5)


def test_invalid_dates_are_ignored() -> None:
    assert find_date_mentions("2/30/2026 and 13/01/2026 and Feb 30", TODAY) == []


def test_ordinal_day_only() -> None:
    (mention,) = find_date_mentions("actually the date was the 20th", TODAY)
    assert mention.month is None
    assert mention.day == 20
    assert resolve(mention, "incident_date", TODAY).value == date(2026, 10, 20)
    assert resolve(mention, "effective_date", TODAY).value == date(2026, 11, 20)
    assert resolve(mention, "dob", TODAY).error


def test_amounts_and_policy_numbers_are_not_dates() -> None:
    assert find_date_mentions("about $2.5k, POL-100001, 2500 dollars", TODAY) == []


# --- missing-year resolution and year boundaries -------------------------------------------------


@pytest.mark.parametrize(
    ("today", "text", "role", "expected"),
    [
        (date(2027, 1, 2), "12/30", "incident_date", date(2026, 12, 30)),
        (date(2027, 1, 2), "Jan 1", "incident_date", date(2027, 1, 1)),
        (date(2026, 12, 20), "Jan 5", "effective_date", date(2027, 1, 5)),
        (date(2026, 12, 20), "Dec 20", "effective_date", date(2026, 12, 20)),
        (date(2026, 12, 20), "Dec 21", "incident_date", date(2025, 12, 21)),
        (date(2026, 12, 31), "1/2", "effective_date", date(2027, 1, 2)),
        (date(2027, 1, 1), "12/31", "incident_date", date(2026, 12, 31)),
        (date(2027, 1, 3), "the 30th", "incident_date", date(2026, 12, 30)),
        (date(2026, 12, 28), "the 2nd", "effective_date", date(2027, 1, 2)),
        (TODAY, "Nov 1", "effective_date", date(2026, 11, 1)),
        (TODAY, "Nov 1", "incident_date", date(2025, 11, 1)),
    ],
)
def test_missing_year_rules(today: date, text: str, role: str, expected: date) -> None:
    (mention,) = find_date_mentions(text, today)
    assert resolve(mention, role, today).value == expected


@pytest.mark.parametrize(
    ("text", "role", "expected"),
    [
        ("4/12/85", "dob", date(1985, 4, 12)),
        ("4/12/05", "dob", date(2005, 4, 12)),  # age 21
        ("4/12/26", "incident_date", date(2026, 4, 12)),
        ("11/1/26", "effective_date", date(2026, 11, 1)),
    ],
)
def test_two_digit_years(text: str, role: str, expected: date) -> None:
    (mention,) = find_date_mentions(text, TODAY)
    assert resolve(mention, role, TODAY).value == expected


def test_two_digit_dob_with_no_fitting_century() -> None:
    (mention,) = find_date_mentions("1/1/12", TODAY)  # 2012 → 14, 1912 → 114
    assert resolve(mention, "dob", TODAY).value is None
    assert resolve(mention, "dob", TODAY).error


def test_yearless_dob_is_rejected() -> None:
    assignment = extract_dates("I was born April 12", today=TODAY)
    (mention,) = assignment.mentions
    assert mention.role == "dob"
    assert mention.value is None
    assert mention.error == "Please include the year of your date of birth."


# --- role assignment -----------------------------------------------------------------------------


def test_multiple_dates_assigned_by_label() -> None:
    assignment = extract_dates("DOB 4/12/85; the accident was 10/20", today=TODAY)
    roles = assignment.by_role()
    assert roles["dob"].value == date(1985, 4, 12)
    assert roles["incident_date"].value == date(2026, 10, 20)
    assert assignment.ambiguity is None


def test_label_in_same_clause_wins_over_plausibility() -> None:
    assignment = extract_dates("cancel it November 1 because I'm switching providers", today=TODAY)
    assert assignment.mentions[0].role == "effective_date"
    assert assignment.mentions[0].role_source == "label"
    assert assignment.mentions[0].value == date(2026, 11, 1)


def test_rear_ended_label() -> None:
    assignment = extract_dates("I got rear-ended yesterday.", today=TODAY)
    assert assignment.by_role()["incident_date"].value == date(2026, 10, 21)


def test_asked_slot_assignment() -> None:
    assignment = extract_dates(
        "Nov 1, switching provider", today=TODAY, asked_date_slots=["effective_date"]
    )
    mention = assignment.mentions[0]
    assert (mention.role, mention.role_source, mention.value) == (
        "effective_date",
        "asked",
        date(2026, 11, 1),
    )


def test_asked_slot_ignored_when_two_date_slots_asked() -> None:
    assignment = extract_dates("4/12/1985", today=TODAY, asked_date_slots=["dob", "incident_date"])
    assert assignment.mentions[0].role_source == "plausibility"


def test_plausibility_single_role() -> None:
    assignment = extract_dates("POL-100001 4/12/1985", today=TODAY)
    assert assignment.mentions[0].role == "dob"
    assert assignment.mentions[0].role_source == "plausibility"


def test_plausibility_limited_to_relevant_roles() -> None:
    # "Nov 1" fits both incident (2025-11-01) and effective (2026-11-01) in general,
    # but in a cancellation conversation only effective_date is relevant.
    cancel = extract_dates("Nov 1", today=TODAY, relevant_roles={"dob", "effective_date"})
    assert cancel.mentions[0].role == "effective_date"
    unknown = extract_dates("Nov 1", today=TODAY)
    assert unknown.mentions[0].role is None
    assert unknown.ambiguity is not None
    assert unknown.ambiguity["kind"] == "no_role"


def test_llm_role_accepted_only_for_matching_span() -> None:
    accepted = extract_dates("Nov 1", today=TODAY, llm_roles=[("Nov 1", "effective_date")])
    assert accepted.mentions[0].role == "effective_date"
    assert accepted.mentions[0].role_source == "llm"
    rejected = extract_dates("Nov 1", today=TODAY, llm_roles=[("November 2", "effective_date")])
    assert rejected.mentions[0].role is None
    implausible = extract_dates("Nov 1", today=TODAY, llm_roles=[("Nov 1", "dob")])
    assert implausible.mentions[0].role is None


def test_two_dates_same_role_is_ambiguous() -> None:
    assignment = extract_dates(
        "the accident was 10/20 or maybe the accident was 10/19", today=TODAY
    )
    assert assignment.ambiguity == {
        "kind": "same_role",
        "role": "incident_date",
        "dates": ["10/20", "10/19"],
    }


def test_ambiguous_pair_names_both_dates() -> None:
    assignment = extract_dates(
        "4/12/85 and 10/20",
        today=TODAY,
        relevant_roles={"dob", "incident_date"},
        asked_date_slots=["dob"],
    )
    # Both claim dob via the asked slot: never guess.
    assert assignment.ambiguity is not None
    assert assignment.ambiguity["dates"] == ["4/12/85", "10/20"]
    assert assignment.ambiguity["role"] == "dob"


def test_dates_with_commas_inside_are_one_clause() -> None:
    assignment = extract_dates("born April 12, 1985, the accident happened Oct 20", today=TODAY)
    roles = assignment.by_role()
    assert roles["dob"].value == date(1985, 4, 12)
    assert roles["incident_date"].value == date(2026, 10, 20)


def test_mention_to_dict() -> None:
    assignment = extract_dates("born 4/12/1985", today=TODAY)
    assert assignment.mentions[0].to_dict() == {
        "text": "4/12/1985",
        "role": "dob",
        "role_source": "label",
        "value": "1985-04-12",
        "error": None,
    }
