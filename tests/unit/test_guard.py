"""M6: FR50 fact check and FR51 coverage-language filter."""

from __future__ import annotations

import pytest

from app.responses.guard import check_response, coverage_language, facts_supported

FACTS = {
    "policy": {
        "policy_id": "POL-100001",
        "term_end": "2026-12-31",
        "annual_premium_usd": 1200.0,
        "coverages": [{"type": "COLLISION", "deductible": 500}],
    }
}


@pytest.mark.parametrize(
    "text",
    [
        "Your policy POL-100001 is active.",
        "It runs until December 31, 2026 with a $500 deductible.",
        "The premium is $1,200 and the term ends 2026-12-31.",
        "No identifiers here at all.",
    ],
)
def test_supported_facts(text: str) -> None:
    assert facts_supported(text, FACTS)


@pytest.mark.parametrize(
    "text",
    [
        "Your policy POL-100002 is active.",
        "Claim CLM-2026-000123 is open.",
        "It renews on 2027-01-01.",
        "It renews January 1, 2027.",
        "Your deductible is $250.",
        "Approval APR-ABCDE is pending.",
    ],
)
def test_unsupported_facts(text: str) -> None:
    assert not facts_supported(text, FACTS)


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("This will be covered.", True),
        ("Your claim is going to be paid soon.", True),
        ("We guarantee a fast payout.", True),
        ("You're covered for this.", True),
        ("We'll pay for the repair.", True),
        ("You should be reimbursed in full.", True),
        ("An adjuster will determine eligibility.", False),
        ("Your coverages are Liability and Collision.", False),
        ("Premium paid through 2026-12-31.", False),
    ],
)
def test_coverage_language(text: str, flagged: bool) -> None:
    assert coverage_language(text) is flagged


def test_check_response_names_failed_check() -> None:
    assert check_response("Your policy POL-100001 is active.", FACTS) is None
    assert check_response("Claim CLM-2026-000123.", FACTS) == "facts"
    assert check_response("POL-100001 will be covered.", FACTS) == "coverage"


def test_amount_quoted_verbatim_from_facts_is_grounded() -> None:
    facts = {"slots": {"incident_location": "near the $5 car wash"}}
    assert facts_supported("Location: near the $5 car wash", facts)
    assert not facts_supported("We'll refund $5.", {"slots": {}})
