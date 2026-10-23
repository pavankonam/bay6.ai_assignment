"""M3: validators, FR29a invalidation matrix, FR5a lifecycle, rule-based NLU, redaction."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.graph.invalidation import invalidation_updates
from app.graph.lifecycle import TASK_RESET, reset_task_state
from app.graph.state import TERMINAL_OUTCOMES
from app.nlu import rules
from app.nlu.redaction import redact_dob
from app.validation import validators as v

# --- validators ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("pol100001", "POL-100001"), ("POL 100001", "POL-100001"), ("POL-100001", "POL-100001")],
)
def test_policy_id_normalization(raw: str, expected: str) -> None:
    assert v.validate_policy_id(raw).value == expected


@pytest.mark.parametrize("raw", ["12345", "POL-12345", "ABC-100001", None])
def test_policy_id_invalid(raw: Any) -> None:
    assert v.validate_policy_id(raw).error == v.POLICY_ID_ERROR


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("1985-04-12", True),
        ("2010-10-22", True),
        ("2010-10-23", False),
        ("1916-10-22", True),
        ("1915-10-21", False),
        ("bad", False),
        (date(1985, 4, 12), True),
    ],
)
def test_dob_age_window(raw: Any, ok: bool) -> None:
    assert v.validate_dob(raw).ok is ok


def test_incident_date_rules() -> None:
    assert v.validate_incident_date("2026-10-22").ok
    assert v.validate_incident_date("2026-10-23").error == v.INCIDENT_FUTURE_ERROR
    assert v.validate_incident_date("2025-01-01").ok  # TOO_OLD is decided by FR31 after verify
    assert not v.validate_incident_date("garbage").ok


def test_effective_date_window() -> None:
    assert v.validate_effective_date("2026-10-22").ok
    assert v.validate_effective_date("2026-12-21").ok
    assert v.validate_effective_date("2026-10-21").error == v.EFFECTIVE_PAST_ERROR
    error = v.validate_effective_date("2026-12-22").error
    assert error is not None
    assert "60 days" in error
    assert not v.validate_effective_date(None).ok


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$2.5k", 2500.0),
        ("2500", 2500.0),
        ("about 2,500", 2500.0),
        (1200, 1200.0),
        ("3 thousand", 3000.0),
        ("$10,000.50", 10000.5),
        ("1000000", 1000000.0),
    ],
)
def test_damage_parsing(raw: Any, expected: float) -> None:
    assert v.validate_damage(raw).value == expected


@pytest.mark.parametrize("raw", ["0", "-5", "1000001", "lots", True, None])
def test_damage_invalid(raw: Any) -> None:
    assert not v.validate_damage(raw).ok


def test_text_bool_police_reason_validators() -> None:
    assert v.validate_location("Main St").value == "Main St"
    assert not v.validate_location("ab").ok
    assert not v.validate_location("x" * 201).ok
    assert v.validate_description("  Rear-ended   at a light ").value == "Rear-ended at a light"
    assert not v.validate_description("bump").ok
    assert v.validate_bool("yes").value is True
    assert v.validate_bool(False).value is False
    assert not v.validate_bool("maybe").ok
    assert v.validate_police_report("pr-2026-77").value == "PR-2026-77"
    assert not v.validate_police_report("no!").ok
    assert v.validate_reason("switching provider").value == "switching_provider"
    assert v.validate_reason("sold my car").value == "sold_vehicle"
    assert v.validate_reason("too expensive").value == "cost"
    assert v.validate_reason("I don't need it anymore").value == "no_longer_needed"
    assert v.validate_reason("other").value == "other"
    assert not v.validate_reason("banana").ok
    assert not v.validate_reason(None).ok


def test_validate_slot_dispatch() -> None:
    assert v.validate_slot("policy_id", "pol 100001").value == "POL-100001"
    assert set(v.VALIDATORS) == {
        "policy_id",
        "dob",
        "incident_date",
        "incident_location",
        "incident_description",
        "injuries",
        "other_party_involved",
        "estimated_damage_usd",
        "police_report_number",
        "effective_date",
        "reason",
    }


# --- FR29a invalidation matrix -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        (
            "policy_id",
            {
                "pending_action": None,
                "verified": False,
                "verified_policy_id": None,
                "policy_snapshot": None,
                "quote": None,
                "claim_checks": None,
                "duplicate_override": False,
            },
        ),
        ("dob", {"pending_action": None, "verified": False}),
        (
            "incident_date",
            {"pending_action": None, "claim_checks": None, "duplicate_override": False},
        ),
        ("effective_date", {"pending_action": None, "quote": None}),
        ("injuries", {"pending_action": None}),
        ("estimated_damage_usd", {"pending_action": None}),
        ("reason", {"pending_action": None}),
        ("incident_location", {"pending_action": None}),
        ("incident_description", {"pending_action": None}),
        ("other_party_involved", {"pending_action": None}),
        ("police_report_number", {"pending_action": None}),
    ],
)
def test_invalidation_matrix(slot: str, expected: dict[str, Any]) -> None:
    assert invalidation_updates({slot}) == expected


def test_invalidation_nothing_changed() -> None:
    assert invalidation_updates(set()) == {}


# --- FR5a lifecycle ------------------------------------------------------------------------------

FULL_TASK: dict[str, Any] = {
    "active_intent": "report_claim",
    "slots": {"policy_id": "POL-100001", "dob": "1985-04-12", "incident_date": "2026-10-21"},
    "verified": True,
    "verified_policy_id": "POL-100001",
    "verification_attempts": 1,
    "intent_queue": [{"intent": "cancel_policy", "slots": {}}],
    "suspended_tasks": [{"intent": "report_claim", "slots": {}, "policy_id": "POL-100001"}],
    "pending_action": {"pending_action_id": "x"},
    "quote": {"x": 1},
}


@pytest.mark.parametrize("outcome", sorted(TERMINAL_OUTCOMES))
def test_reset_task_state_terminal(outcome: str) -> None:
    update = reset_task_state({**FULL_TASK, "last_outcome": outcome})
    assert update["slots"] == {"policy_id": "POL-100001", "dob": "1985-04-12"}
    for key, value in TASK_RESET.items():
        assert update[key] == value
    for kept in (
        "verified",
        "verified_policy_id",
        "verification_attempts",
        "intent_queue",
        "suspended_tasks",
        "messages",
    ):
        assert kept not in update


@pytest.mark.parametrize(
    "outcome", ["OUT_OF_SCOPE_ANSWERED", "SMALLTALK", "SYSTEM_UNAVAILABLE", None]
)
def test_reset_task_state_non_terminal_is_noop(outcome: str | None) -> None:
    assert reset_task_state({**FULL_TASK, "last_outcome": outcome}) == {}


# --- rule-based NLU ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    ["yes", "Y", "yep", "Yeah!", "confirm", "submit", "ok", "okay.", "go ahead", "sounds good"],
)
def test_bare_affirmatives(reply: str) -> None:
    assert rules.bare_yes_no(reply) == "yes"


@pytest.mark.parametrize("reply", ["no", "N", "nope", "don't", "cancel"])
def test_bare_negatives(reply: str) -> None:
    assert rules.bare_yes_no(reply) == "no"


@pytest.mark.parametrize(
    "reply",
    [
        "yes, but change the location to Main St",
        "ok, actually the date was the 20th",
        "confirm but date is 10/19",
        "sure thing buddy",
        "retry",
    ],
)
def test_non_bare_replies_are_not_regex_matched(reply: str) -> None:
    assert rules.bare_yes_no(reply) is None


def test_retry_vocabulary() -> None:
    assert rules.bare_yes_no("retry", retry=True) == "yes"
    for reply in ("stop", "cancel that", "wait", "withdraw"):
        assert rules.bare_yes_no(reply, retry=True) == "no"


@pytest.mark.parametrize(
    "text",
    [
        "let me talk to a person",
        "I want a human",
        "get me an agent",
        "representative",
        "is there someone real",
        "can you call me",
    ],
)
def test_handoff_keywords(text: str) -> None:
    assert rules.is_handoff(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("will I be covered?", True),
        ("Will this be paid?", True),
        ("am I covered", True),
        ("does my policy cover this?", True),
        ("what's my coverage period", False),
        ("I want to file a claim", False),
    ],
)
def test_coverage_questions(text: str, expected: bool) -> None:
    assert rules.is_coverage_question(text) is expected


def test_keyword_intents_strict_and_loose() -> None:
    assert rules.keyword_intents("check my status and cancel it", strict=True) == [
        "policy_status",
        "cancel_policy",
    ]
    assert rules.keyword_intents("I got rear-ended", strict=True) == []
    assert rules.keyword_intents("I got rear-ended", strict=False) == ["report_claim"]


def test_rule_understand_strict_fallback() -> None:
    result = rules.rule_understand("I had an accident", rules.RuleContext(), strict=True)
    assert result.intents == ["report_claim"]
    assert result.task_slots == {}
    unknown = rules.rule_understand("Main St & 5th", rules.RuleContext(), strict=True)
    assert unknown.confidence == 0.0
    in_task = rules.rule_understand(
        "Main St & 5th", rules.RuleContext(active_intent="report_claim"), strict=True
    )
    assert in_task.intents == ["provide_info"]


def test_rule_understand_heuristic_slots() -> None:
    ctx = rules.RuleContext(
        active_intent="report_claim", asked_slots=["injuries", "other_party_involved"]
    )
    result = rules.rule_understand("No injuries, yes another car", ctx, strict=False)
    assert result.intents == ["provide_info"]
    assert result.task_slots["report_claim"] == {"injuries": False, "other_party_involved": True}
    ctx = rules.RuleContext(
        active_intent="report_claim", asked_slots=["estimated_damage_usd", "police_report_number"]
    )
    result = rules.rule_understand("about 2500, no report", ctx, strict=False)
    assert result.task_slots["report_claim"] == {
        "estimated_damage_usd": 2500.0,
        "police_report_number": "skip",
    }
    result = rules.rule_understand("skip", ctx, strict=False)
    assert result.task_slots["report_claim"] == {
        "estimated_damage_usd": "skip",
        "police_report_number": "skip",
    }
    result = rules.rule_understand("$12k, report number PR-2026-1", ctx, strict=False)
    assert result.task_slots["report_claim"] == {
        "estimated_damage_usd": 12000.0,
        "police_report_number": "PR-2026-1",
    }


def test_rule_understand_heuristic_bare_bools_and_solo() -> None:
    ctx = rules.RuleContext(
        active_intent="report_claim", asked_slots=["injuries", "other_party_involved"]
    )
    assert rules.rule_understand("no, no", ctx, strict=False).task_slots["report_claim"] == {
        "injuries": False,
        "other_party_involved": False,
    }
    solo = rules.rule_understand("nobody hurt, just me", ctx, strict=False)
    assert solo.task_slots["report_claim"] == {"injuries": False, "other_party_involved": False}


def test_rule_understand_heuristic_cancel_reason_and_classes() -> None:
    ctx = rules.RuleContext(active_intent="cancel_policy", asked_slots=["effective_date", "reason"])
    result = rules.rule_understand("Nov 1, switching provider", ctx, strict=False)
    assert result.task_slots == {"cancel_policy": {"reason": "switching_provider"}}
    assert rules.rule_understand("thanks!", rules.RuleContext(), strict=False).intents == [
        "smalltalk",
    ]
    assert rules.rule_understand(
        "home insurance quote please", rules.RuleContext(), strict=False
    ).intents == ["out_of_scope"]
    assert rules.rule_understand(
        "will I be covered?", rules.RuleContext(), strict=False
    ).intents == ["out_of_scope"]
    gibberish = rules.rule_understand("xqzvbnmw", rules.RuleContext(), strict=False)
    assert gibberish.confidence < 0.6
    location = rules.rule_understand(
        "at Main St & 5th Ave",
        rules.RuleContext(active_intent="report_claim", asked_slots=["incident_location"]),
        strict=False,
    )
    assert location.task_slots["report_claim"]["incident_location"] == "Main St & 5th Ave"
    correction = rules.rule_understand(
        "actually it was the 20th", rules.RuleContext(active_intent="report_claim"), strict=False
    )
    assert correction.is_correction


# --- redaction (NFR8a) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "born 4/12/1985",
        "DOB 04/12/85",
        "my birthday is April 12, 1985",
        "1985-04-12",
        "POL-100001, 12 April 1985",
        "dob: apr 12 1985",
    ],
)
def test_redact_dob_by_extractor(text: str) -> None:
    redacted = redact_dob(text)
    assert "[DOB]" in redacted
    assert "1985" not in redacted


def test_redact_keeps_other_dates_and_policy() -> None:
    assert redact_dob("POL-100001 the accident was 10/20") == "POL-100001 the accident was 10/20"
    assert redact_dob("DOB 4/12/85; the accident was 10/20") == (
        "DOB [DOB]; the accident was 10/20"
    )
