"""M3 integration: understanding, slot filling, verification, suspension, lifecycle (fake LLM)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.nlu.schemas import UnderstandResult
from tests.fixtures.fake_llm import FakeScript
from tests.integration.conftest import Conversation

Convo = Callable[..., Conversation]
SetEnv = Callable[..., None]


def test_flow_a_status_happy_path(convo: Convo, tool_mode: str) -> None:
    c = convo()
    first = c.reply("Hi, can you check if my policy is still active?")
    assert "Hi, I'm the Acme Mutual support assistant." in first  # FR5: greeting on turn 1
    assert "policy number (format POL-123456) and your date of birth" in first
    assert c.tool_names() == []
    answer = c.reply("POL-100001, born April 12 1985")
    assert c.tool_names() == ["verify_policyholder", "get_policy_status"]
    assert "Your policy POL-100001 for the 2022 Toyota Camry is ACTIVE." in answer
    assert "Coverage period: 2026-01-01 to 2026-12-31. Premium paid through 2026-12-31." in answer
    assert (
        "Coverages: Liability, Collision ($500 deductible), Comprehensive ($250 deductible)."
        in answer
    )
    assert "anything else" in answer
    assert "Hi, I'm the Acme Mutual" not in answer  # greeting only on the first turn
    state = c.state
    assert state["status"] == "completed"
    assert state["last_outcome"] == "STATUS_PROVIDED"
    assert state["verified"] is True
    # NFR2: Flow A ≤ 4 calls on the lookup turn in llm mode; only understand + respond otherwise.
    assert c.turn_llm_calls[1] == (4 if tool_mode == "llm" else 2)


def test_flow_a2_identity_first_makes_no_tool_calls(convo: Convo) -> None:
    c = convo()
    reply = c.reply("POL-100001, DOB 4/12/1985")
    assert "Thanks, I've noted your details." in reply
    assert (
        "checking your policy status, reporting a vehicle claim, or requesting a cancellation"
        in reply
    )
    assert c.tool_names() == []  # FR6a
    assert c.state["slots"] == {"policy_id": "POL-100001", "dob": "1985-04-12"}
    assert c.state["verified"] is False
    answer = c.reply("Is it active?")
    assert c.tool_names() == ["verify_policyholder", "get_policy_status"]
    assert "is ACTIVE" in answer
    assert "date of birth" not in answer  # identity is not re-asked


def test_flow_e_intent_switch_and_resume(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    assert c.state["slots"]["incident_date"] == "2026-10-21"
    replies = c.say("Actually, is my policy even active?")
    assert "is ACTIVE" in replies[0]
    assert replies[-1] == "Would you like to continue reporting your claim?"
    state = c.state
    assert state["pending_question"] == {
        "kind": "next_task",
        "context": {"source": "suspended", "intent": "report_claim"},
    }
    assert state["suspended_tasks"][0]["slots"]["incident_date"] == "2026-10-21"
    assert state["verification_attempts"] == 0
    lookups_before = len(c.tool_messages("get_policy_status"))
    reply = c.reply("yes")
    state = c.state
    assert state["active_intent"] == "report_claim"
    assert state["slots"]["incident_date"] == "2026-10-21"
    assert state["suspended_tasks"] == []
    assert len(c.tool_messages("get_policy_status")) == lookups_before + 1  # lookup re-run
    assert "Where did the incident happen?" in reply


def test_flow_e_decline_resume_drops_task(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    c.say("Actually, is my policy even active?")
    reply = c.reply("no")
    assert reply.startswith("Okay.")
    assert c.state["suspended_tasks"] == []
    assert c.state["active_intent"] is None


def test_flow_e_unrelated_reply_drops_task_and_handles_message(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    c.say("Actually, is my policy even active?")
    reply = c.reply("I'd like to cancel my policy instead")
    state = c.state
    assert state["suspended_tasks"] == []
    assert state["active_intent"] == "cancel_policy"
    assert "When should the cancellation take effect" in reply


def test_flow_h_second_request_reuses_identity_only(convo: Convo) -> None:
    """FR5a: after a completed task, the next request asks only for its own slots."""
    c = convo()
    c.say("is my policy active? POL-100001 born 4/12/1985")
    assert c.state["last_outcome"] == "STATUS_PROVIDED"
    reply = c.reply("Now I'd like to cancel the policy.")
    assert "When should the cancellation take effect, and what's the reason?" in reply
    assert "date of birth" not in reply
    assert c.tool_names().count("verify_policyholder") == 1  # identity carried over
    c.say("Nov 1, switching provider")
    quote_args = c.tool_args("get_cancellation_quote")
    assert quote_args == [{"policy_id": "POL-100001", "effective_date": "2026-11-01"}]


def test_two_consecutive_status_checks(convo: Convo) -> None:
    c = convo()
    c.say("is my policy active? POL-100001 born 4/12/1985")
    c.say("can you check the status again?")
    assert c.tool_names() == ["verify_policyholder", "get_policy_status", "get_policy_status"]
    assert c.state["last_outcome"] == "STATUS_PROVIDED"


def test_claim_slots_do_not_leak_into_next_task(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    c.say("let me talk to a person")  # terminal ESCALATED → FR5a reset
    state = c.state
    assert state["slots"] == {"policy_id": "POL-100001", "dob": "1985-04-12"}
    assert state["active_intent"] is None
    c.say("I want to cancel my policy")
    assert set(c.state["slots"]) == {"policy_id", "dob"}


def test_smalltalk_and_out_of_scope_mid_claim_keep_slots(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    before = dict(c.state["slots"])
    reply = c.reply("thanks!")
    assert c.state["last_outcome"] == "SMALLTALK"
    assert "Where did the incident happen?" in reply  # repeats the pending question
    assert c.state["slots"] == before
    reply = c.reply("what's the weather like?")
    assert c.state["last_outcome"] == "OUT_OF_SCOPE_ANSWERED"
    assert "I can help with checking your policy status" in reply
    assert c.state["slots"] == before
    assert c.state["active_intent"] == "report_claim"
    c.say("Main St & 5th Ave, Springfield")
    assert c.state["slots"]["incident_location"] == "Main St & 5th Ave, Springfield"


def test_bare_yes_with_nothing_pending_is_smalltalk(convo: Convo) -> None:
    c = convo()
    c.say("yes")
    assert c.state["last_outcome"] == "SMALLTALK"
    assert c.tool_names() == []


def test_multi_date_message(convo: Convo) -> None:
    c = convo()
    c.say("I had an accident. DOB 4/12/85; the accident was 10/20, policy POL-100001")
    slots = c.state["slots"]
    assert slots["dob"] == "1985-04-12"
    assert slots["incident_date"] == "2026-10-20"
    assert c.state["verified"] is True


def test_ambiguous_dates_trigger_clarification_then_carry_over(convo: Convo) -> None:
    c = convo()
    reply = c.reply("I need to report a claim. POL-100001 4/12/85 10/20 and 10/19")
    assert "I saw 10/20 and 10/19. Which one is your incident date?" in reply
    assert c.tool_names() == []
    c.say("10/20")
    slots = c.state["slots"]
    assert slots["incident_date"] == "2026-10-20"
    assert slots["policy_id"] == "POL-100001"  # carried over from the clarified message
    assert slots["dob"] == "1985-04-12"


def test_yearless_dob_is_reasked(convo: Convo) -> None:
    c = convo()
    c.say("is my policy active?")
    reply = c.reply("POL-100001, born April 12")
    assert "Please include the year of your date of birth." in reply
    assert c.tool_names() == []
    c.say("April 12 1985")
    assert c.tool_names() == ["verify_policyholder", "get_policy_status"]


@pytest.mark.parametrize(
    ("today", "message", "expected"),
    [
        ("2027-01-02", "I had an accident on 12/30", "2026-12-30"),
        ("2027-01-02", "my car was hit on Jan 1", "2027-01-01"),
    ],
)
def test_year_boundary_incident_dates(
    set_env: SetEnv, convo: Convo, today: str, message: str, expected: str
) -> None:
    set_env(APP_TODAY=today)
    c = convo()
    c.say(message)
    assert c.state["slots"]["incident_date"] == expected


@pytest.mark.parametrize(
    ("today", "expected"), [("2026-12-20", "2027-01-05"), ("2026-12-31", "2027-01-05")]
)
def test_year_boundary_effective_dates(
    set_env: SetEnv, convo: Convo, today: str, expected: str
) -> None:
    set_env(APP_TODAY=today)
    c = convo()
    c.say("I want to cancel my policy effective Jan 5 because it's too expensive")
    assert c.state["slots"]["effective_date"] == expected
    assert c.state["slots"]["reason"] == "cost"


def test_wrong_dob_then_correct_dob(convo: Convo) -> None:
    """FR20a: identity cleared after failure; next values accepted; attempts == 1."""
    c = convo()
    c.say("is my policy active?")
    reply = c.reply("POL-100001, born 4/13/1985")
    assert "I couldn't verify those details. Please double-check your policy number" in reply
    assert "POL-100001" not in reply  # FR20: no hint which field was wrong
    state = c.state
    assert "policy_id" not in state["slots"]
    assert "dob" not in state["slots"]
    reply = c.reply("POL-100001, born 4/12/1985")
    assert "is ACTIVE" in reply
    assert c.state["verification_attempts"] == 1
    assert c.state["verified"] is True


def test_unknown_policy_looks_like_wrong_dob(convo: Convo) -> None:
    c = convo()
    c.say("is my policy active?")
    reply = c.reply("POL-000000, born 4/12/1985")
    assert "I couldn't verify those details." in reply


def test_lockout_after_max_verification_attempts(convo: Convo) -> None:
    c = convo()
    c.say("is my policy active?")
    for day in (1, 2):
        c.say(f"POL-100001 dob 1/{day}/1990")
    reply = c.reply("POL-100001 dob 1/3/1990")
    assert "For your security, please call 1-800-555-0199" in reply
    assert c.state["status"] == "locked"
    reply = c.reply("POL-100001 dob 4/12/1985")  # refuses further policy operations (FR19)
    assert "For your security" in reply
    assert c.tool_names().count("verify_policyholder") == 3


def test_different_policy_resets_verification(convo: Convo) -> None:
    """FR21 + FR11: switch after lookup, change the policy, then resume → re-verify + lookup."""
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    c.say("Actually, what is the status of POL-100004 born 11/3/1995?")
    assert c.state["verified_policy_id"] == "POL-100004"
    verify_before = len(c.tool_messages("verify_policyholder"))
    lookups_before = len(c.tool_messages("get_policy_status"))
    reply = c.reply("yes")
    assert "To continue with policy POL-100001, please confirm the date of birth" in reply
    c.say("1985-04-12")
    assert len(c.tool_messages("verify_policyholder")) == verify_before + 1
    assert c.tool_args("verify_policyholder")[-1]["policy_id"] == "POL-100001"
    assert len(c.tool_messages("get_policy_status")) == lookups_before + 1
    assert c.state["verified_policy_id"] == "POL-100001"
    assert c.state["policy_snapshot"]["policy_id"] == "POL-100001"


def test_invalid_policy_format_feedback(convo: Convo) -> None:
    c = convo()
    c.say("is my policy active?")
    reply = c.reply("12345, born 4/12/1985")
    assert "Policy numbers look like POL-123456. Could you check yours?" in reply
    assert c.tool_names() == []


def test_low_confidence_clarifies_then_escalates(convo: Convo) -> None:
    c = convo()
    for _ in range(3):
        reply = c.reply("qwrtzxcvbn")
        assert "Which would you like?" in reply
    reply = c.reply("qwrtzxcvbn")
    assert "1-800-555-0199" in reply
    assert c.state["status"] == "escalated"
    assert c.tool_names() == []


def test_missing_slot_max_clarification_escalates(convo: Convo, fake_llm: FakeScript) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    empty = UnderstandResult(intents=["provide_info"], confidence=0.9)
    fake_llm.push("classify_extract", empty, empty)
    c.say("hmm")
    c.say("hmm")
    assert c.state["clarification_attempts"]["incident_location"] == 3
    fake_llm.push("classify_extract", empty)
    reply = c.reply("hmm")
    assert c.state["status"] == "escalated"
    assert "1-800-555-0199" in reply


def test_handoff_anywhere_escalates(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    reply = c.reply("can I talk to a real person please")
    assert reply == "Of course. You can reach a support specialist at 1-800-555-0199."
    assert c.state["status"] == "escalated"
    assert c.state["last_outcome"] == "ESCALATED"


def test_handoff_at_pending_question(convo: Convo) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    c.say("POL-100001, 1985-04-12")
    c.say("Actually, is my policy even active?")
    reply = c.reply("let me talk to a person")
    assert "1-800-555-0199" in reply
    assert c.state["pending_question"] is None
    assert c.state["status"] == "escalated"


def test_coverage_question_gets_template(convo: Convo, fake_llm: FakeScript) -> None:
    c = convo()
    reply = c.reply("will I be covered?")
    assert reply.endswith(
        "I can't determine coverage or payment. An adjuster will review your claim and "
        "determine eligibility."
    )
    assert fake_llm.calls("respond") == []  # templated, no LLM prose


def test_empty_message(convo: Convo, fake_llm: FakeScript) -> None:
    c = convo()
    reply = c.reply("   ")
    assert reply.endswith("I didn't catch that. How can I help?")
    assert fake_llm.calls() == []


def test_turn_limit(set_env: SetEnv, convo: Convo) -> None:
    set_env(MAX_TURNS="2")
    c = convo()
    c.say("hi")
    c.say("hello")
    reply = c.reply("hello again")
    assert "This conversation has gone on for a while" in reply
    assert c.state["status"] == "escalated"


def test_llm_failure_falls_back_to_rules(convo: Convo, fake_llm: FakeScript) -> None:
    """FR48: understand falls back to keyword intents + deterministic extraction."""
    fake_llm.push("classify_extract", TimeoutError("llm down"), TimeoutError("still down"))
    fake_llm.push("respond", RuntimeError("down"))
    c = convo()
    c.say("check my policy status please")
    assert c.state["active_intent"] == "policy_status"
    reply = c.reply("POL-100001 born 4/12/1985")
    assert "is ACTIVE" in reply  # templated response after respond fallback
    assert c.state["last_outcome"] == "STATUS_PROVIDED"


def test_llm_invalid_output_repaired_then_fallback(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push("classify_extract", {"intents": ["nonsense"]}, {"confidence": 2})
    c = convo()
    reply = c.reply("asdf qwer")
    assert "trouble reaching our systems" in reply  # fallback cannot proceed (FR48)
    assert len(fake_llm.calls("classify_extract")) == 2  # one repair retry


def test_raw_dob_never_sent_to_llm(convo: Convo, fake_llm: FakeScript) -> None:
    c = convo()
    c.say("is my policy active? POL-100001 born 4/12/1985")
    c.say("my birthday is April 12, 1985 by the way")
    c.say("DOB 1985-04-12")
    assert fake_llm.requests
    for request in fake_llm.requests:
        for raw in ("4/12/1985", "April 12, 1985", "1985-04-12"):
            assert raw not in request.payload_text


def test_lookup_result_contains_no_dob_or_name(convo: Convo) -> None:
    c = convo()
    c.say("is my policy active? POL-100001 born 4/12/1985")
    (lookup,) = c.tool_messages("get_policy_status")
    assert "dob" not in lookup.artifact["data"]
    assert "Jane" not in str(lookup.artifact)
    assert "1985" not in str(lookup.content)


def test_lapsed_status_has_lapse_date_and_hint(convo: Convo) -> None:
    c = convo()
    reply = c.reply("status of POL-100002 please, dob 9/30/1990")
    assert "is LAPSED" in reply
    assert "It lapsed on 2026-09-01" in reply
    assert "reinstatement" in reply


def test_cancelled_status_shows_effective_date(convo: Convo) -> None:
    c = convo()
    reply = c.reply("status of POL-100003, dob 1/22/1978")
    assert "is CANCELLED" in reply
    assert "Cancellation effective date: 2026-06-30." in reply


def test_cancel_inactive_policy_is_not_eligible(convo: Convo) -> None:
    c = convo()
    reply = c.reply("I want to cancel POL-100005, dob 7/15/1988")
    assert "already pending cancellation" in reply
    assert c.state["last_outcome"] == "NOT_ELIGIBLE_CANCEL"
    assert "get_cancellation_quote" not in c.tool_names()


def test_llm_call_cap_uses_fallback(set_env: SetEnv, convo: Convo, fake_llm: FakeScript) -> None:
    set_env(MAX_LLM_CALLS_PER_THREAD="1")
    c = convo()
    c.say("is my policy active?")
    calls = len(fake_llm.requests)
    c.say("POL-100001 born 4/12/1985")
    assert len(fake_llm.requests) == calls  # cap reached: rule fallback only
    assert c.state["last_outcome"] == "STATUS_PROVIDED"


def test_llm_scripted_understanding_is_used(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push(
        "classify_extract",
        UnderstandResult(
            intents=["cancel_policy"],
            confidence=0.95,
            task_slots={"cancel_policy": {"reason": "sold_vehicle"}},
        ),
    )
    c = convo()
    c.say("I'm getting rid of my ride")
    assert c.state["active_intent"] == "cancel_policy"
    assert c.state["slots"]["reason"] == "sold_vehicle"


def test_llm_role_for_out_of_grammar_date(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push(
        "classify_extract",
        UnderstandResult(
            intents=["report_claim"],
            confidence=0.9,
            task_slots={"report_claim": {"incident_date": "2026-10-17"}},
        ),
    )
    c = convo()
    c.say("I crashed the weekend before last")
    assert c.state["slots"]["incident_date"] == "2026-10-17"


def test_node_writes_only_schema_keys(convo: Convo) -> None:
    from app.graph.state import STATE_KEYS

    c = convo()
    c.say("I got rear-ended yesterday. POL-100001 born 4/12/1985")
    assert set(c.state) <= STATE_KEYS


def _unused(_: Any) -> None:  # keep helpers imported for type checkers
    return None
