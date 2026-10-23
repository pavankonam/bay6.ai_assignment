"""M6 integration: error handling, retries with consent, unknown outcomes, guards, wrappers."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from typing import Any

import pytest
from sqlmodel import select

from app.db import session_scope
from app.mock_api.repository import Cancellation, Claim
from app.mock_api.service import (
    MockCancellationService,
    MockClaimService,
    MockPolicyService,
    MockWriteLookupService,
    Services,
    set_services,
)
from app.observability.metrics import metrics
from app.runtime import SupportRuntime
from app.tools import errors
from app.tools.errors import ToolError
from tests.fixtures.fake_llm import FakeScript
from tests.integration.conftest import Conversation

Convo = Callable[..., Conversation]
SetEnv = Callable[..., None]


def cancellations(policy_id: str | None = None) -> list[Cancellation]:
    with session_scope() as session:
        stmt = select(Cancellation)
        if policy_id:
            stmt = stmt.where(Cancellation.policy_id == policy_id)
        return list(session.exec(stmt).all())


def claims(policy_id: str) -> list[Claim]:
    with session_scope() as session:
        return list(session.exec(select(Claim).where(Claim.policy_id == policy_id)).all())


# --- Flow F: read failures -----------------------------------------------------------------------


def test_flow_f_quote_timeout_then_correction(set_env: SetEnv, convo: Convo) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="1.0", MOCK_FAULT_TOOLS="get_cancellation_quote")
    c = convo()
    c.say("I want to cancel my insurance.")
    c.say("POL-100001 / 04/12/1985")
    reply = c.reply("Nov 1, switching provider")
    assert reply == (
        "I'm having trouble reaching our systems right now. Please try again in a few minutes, "
        "or call 1-800-555-0199."
    )
    state = c.state
    assert state["status"] == "failed_retryable"
    assert state["resume_step"] == "read_tool"
    assert state["last_outcome"] == "SYSTEM_UNAVAILABLE"
    assert state["error"]["attempts"] == 3  # 1 + TOOL_MAX_RETRIES, all inside the wrapper
    assert state["slots"]["effective_date"] == "2026-11-01"  # slots kept
    set_env(MOCK_FAILURE_RATE="0.0")
    summary = c.reply("actually make it Nov 5")
    assert c.tool_args("get_cancellation_quote")[-1] == {
        "policy_id": "POL-100001",
        "effective_date": "2026-11-05",
    }
    assert "effective 2026-11-05" in summary
    state = c.state
    assert state["status"] == "awaiting_confirmation"
    assert state["error"] is None
    assert state["resume_step"] is None


def test_read_failure_plain_retry_does_not_reask(set_env: SetEnv, convo: Convo) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="1.0", MOCK_FAULT_TOOLS="get_policy_status")
    c = convo()
    c.say("is my policy active? POL-100001 born 4/12/1985")
    assert c.state["status"] == "failed_retryable"
    set_env(MOCK_FAILURE_RATE="0.0")
    reply = c.reply("ok, try again please")
    assert "is ACTIVE" in reply
    assert "date of birth" not in reply


def test_smalltalk_during_read_failure_keeps_failure(set_env: SetEnv, convo: Convo) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="1.0", MOCK_FAULT_TOOLS="get_policy_status")
    c = convo()
    c.say("is my policy active? POL-100001 born 4/12/1985")
    c.say("thanks")
    assert c.state["status"] == "failed_retryable"
    assert c.state["resume_step"] == "read_tool"


def test_lookup_failure_then_policy_correction_reverifies(set_env: SetEnv, convo: Convo) -> None:
    set_env(MOCK_FAULTS="true")
    c = convo()
    c.say("I want to cancel my insurance.")
    reply = c.reply("POL-999500 / 01/01/2000")
    assert "trouble reaching our systems" in reply
    assert c.state["verified_policy_id"] == "POL-999500"
    reply = c.reply("sorry, actually it's POL-100001, born 4/12/1985")
    assert c.tool_args("verify_policyholder")[-1] == {
        "policy_id": "POL-100001",
        "dob": "1985-04-12",
    }
    assert c.state["verified_policy_id"] == "POL-100001"
    assert "When should the cancellation take effect" in reply


def test_new_intent_during_read_failure_suspends(set_env: SetEnv, convo: Convo) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="1.0", MOCK_FAULT_TOOLS="get_cancellation_quote")
    c = convo()
    c.say("I want to cancel my insurance.")
    c.say("POL-100001 / 04/12/1985")
    c.say("Nov 1, switching provider")
    set_env(MOCK_FAILURE_RATE="0.0")
    replies = c.say("actually, is my policy still active?")
    state = c.state
    assert "is ACTIVE" in replies[0]
    assert state["suspended_tasks"][0]["intent"] == "cancel_policy"
    assert state["suspended_tasks"][0]["slots"]["effective_date"] == "2026-11-01"
    assert state["status"] != "failed_retryable"


# --- Flow F2: approved write fails (POL-999501) --------------------------------------------------


def approved_write_failure(rt: SupportRuntime) -> tuple[str, list[str]]:
    tid = rt.create_thread()
    rt.post_message(tid, "I want to cancel my insurance.")
    rt.post_message(tid, "POL-999501 / 01/01/2000")
    rt.post_message(tid, "Nov 1, switching provider")
    result = rt.post_message(tid, "yes")
    decision = rt.decide(str(result.pending_approval_id), "approve", "marcus", "ok")
    return tid, decision.messages


FAILED_TEXT = (
    "Your request was approved, but we couldn't complete it right now. Reply 'retry' to try "
    "again, or 'no' to withdraw it. You can also call 1-800-555-0199."
)


def test_flow_f2_message_and_state(set_env: SetEnv, runtime: SupportRuntime) -> None:
    set_env(MOCK_FAULTS="true")
    tid, messages = approved_write_failure(runtime)
    assert messages == [FAILED_TEXT]
    state = runtime.snapshot(tid).values
    assert state["pending_question"] == {
        "kind": "retry_write",
        "context": {"reask_count": 0, "outcome_unknown": False},
    }
    assert state["status"] == "failed_retryable"
    assert state["resume_step"] == "execute_write"
    assert state["pending_action"]["customer_confirmed"] is True
    assert state["approval"]["status"] == "approved"
    assert state["suspended_tasks"] == []  # never suspended (FR47)
    assert cancellations("POL-999501") == []


@pytest.mark.parametrize("reply", ["retry", "yes"])
def test_flow_f2_retry_writes_exactly_once(
    set_env: SetEnv, runtime: SupportRuntime, reply: str
) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAULT_CLEAR_AFTER="3")
    tid, _ = approved_write_failure(runtime)
    key = runtime.snapshot(tid).values["pending_action"]["idempotency_key"]
    result = runtime.post_message(tid, reply)
    (row,) = cancellations("POL-999501")
    assert row.idempotency_key == key  # original key, no new confirmation or approval
    assert result.messages[0].startswith("Your cancellation was approved.")
    state = runtime.snapshot(tid).values
    assert state["last_outcome"] == "CANCEL_COMPLETED"
    assert state["error"] is None


@pytest.mark.parametrize("reply", ["no", "stop", "cancel that", "wait", "withdraw"])
def test_flow_f2_withdraw(set_env: SetEnv, runtime: SupportRuntime, reply: str) -> None:
    set_env(MOCK_FAULTS="true")
    tid, _ = approved_write_failure(runtime)
    result = runtime.post_message(tid, reply)
    assert result.messages == [
        "Okay, I've withdrawn your request, so nothing was submitted. If you need anything "
        "else, please call 1-800-555-0199."
    ]
    assert cancellations("POL-999501") == []
    assert runtime.snapshot(tid).values["last_outcome"] == "WRITE_WITHDRAWN"


def test_flow_f2_unrelated_twice_withdraws(set_env: SetEnv, runtime: SupportRuntime) -> None:
    set_env(MOCK_FAULTS="true")
    tid, _ = approved_write_failure(runtime)
    first = runtime.post_message(tid, "what's the weather?")
    assert first.messages == [
        "Before we continue: should I retry your approved request? Reply 'retry' or 'no'."
    ]
    assert runtime.snapshot(tid).values["pending_question"]["context"]["reask_count"] == 1
    second = runtime.post_message(tid, "tell me a joke")
    assert second.messages[0].startswith("Okay, I've withdrawn your request")
    assert cancellations("POL-999501") == []


def test_flow_f2_new_intent_is_not_a_retry(set_env: SetEnv, runtime: SupportRuntime) -> None:
    set_env(MOCK_FAULTS="true")
    tid, _ = approved_write_failure(runtime)
    result = runtime.post_message(tid, "can you check my policy status?")
    assert "should I retry your approved request" in result.messages[0]
    assert cancellations("POL-999501") == []
    assert runtime.snapshot(tid).values["pending_question"]["kind"] == "retry_write"


def test_flow_f2_handoff_reports_truthful_status(set_env: SetEnv, runtime: SupportRuntime) -> None:
    set_env(MOCK_FAULTS="true")
    tid, _ = approved_write_failure(runtime)
    result = runtime.post_message(tid, "I want to talk to a person")
    assert result.messages[0].startswith("Okay, I've withdrawn your request")
    assert result.messages[1] == "Of course. You can reach a support specialist at 1-800-555-0199."
    assert runtime.snapshot(tid).values["status"] == "escalated"
    assert cancellations("POL-999501") == []


def test_claim_write_failure_without_approval_wording(set_env: SetEnv, convo: Convo) -> None:
    set_env(MOCK_FAULTS="true")
    c = convo()
    for text in (
        "I got rear-ended yesterday.",
        "POL-999501, born 1/1/2000",
        "Main St & 5th Ave",
        "No injuries, yes another car",
        "about 2500, no report",
    ):
        c.say(text)
    reply = c.reply("yes")
    assert reply.startswith("Your request was confirmed, but we couldn't complete it right now.")
    assert claims("POL-999501") == []


# --- FR47a: unknown write outcome ----------------------------------------------------------------


def test_commit_then_timeout_reports_real_confirmation(
    set_env: SetEnv, runtime: SupportRuntime
) -> None:
    set_env(MOCK_FAULTS="true")
    tid = runtime.create_thread()
    runtime.post_message(tid, "I want to cancel my insurance.")
    runtime.post_message(tid, "POL-999502 / 01/01/2000")
    runtime.post_message(tid, "Nov 1, switching provider")
    result = runtime.post_message(tid, "yes")
    decision = runtime.decide(str(result.pending_approval_id), "approve", "marcus", None)
    (row,) = cancellations("POL-999502")
    assert f"Confirmation: {row.cancellation_id}." in decision.messages[0]
    assert "couldn't" not in decision.messages[0]
    assert metrics.get("write_outcome_lookups_total", result="found") == 1


def unknown_outcome(set_env: SetEnv, rt: SupportRuntime) -> tuple[str, list[str]]:
    set_env(MOCK_FAULTS="true", MOCK_FAULT_LOOKUP="true")
    tid = rt.create_thread()
    rt.post_message(tid, "I want to cancel my insurance.")
    rt.post_message(tid, "POL-999502 / 01/01/2000")
    rt.post_message(tid, "Nov 1, switching provider")
    result = rt.post_message(tid, "yes")
    decision = rt.decide(str(result.pending_approval_id), "approve", "marcus", None)
    return tid, decision.messages


def test_unknown_outcome_offer_then_no_escalates(set_env: SetEnv, runtime: SupportRuntime) -> None:
    tid, messages = unknown_outcome(set_env, runtime)
    assert messages[0].startswith("We couldn't confirm whether your approved request went through.")
    assert runtime.snapshot(tid).values["pending_question"]["context"]["outcome_unknown"] is True
    result = runtime.post_message(tid, "no")
    text = " ".join(result.messages)
    assert "withdrawn" not in text  # never claims a withdrawal it can't verify
    assert "A specialist will confirm the status of your approved request" in text
    state = runtime.snapshot(tid).values
    assert state["status"] == "escalated"
    assert state["last_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert len(cancellations("POL-999502")) == 1  # it actually committed


def test_no_after_unknown_reports_success_when_record_found(
    set_env: SetEnv, runtime: SupportRuntime
) -> None:
    tid, _ = unknown_outcome(set_env, runtime)
    set_env(MOCK_FAULT_LOOKUP="false")  # the lookup service recovers
    result = runtime.post_message(tid, "no")
    (row,) = cancellations("POL-999502")
    assert result.messages[0].startswith("Your cancellation was approved.")
    assert row.cancellation_id in result.messages[0]
    assert "withdrawn" not in " ".join(result.messages)
    assert runtime.snapshot(tid).values["last_outcome"] == "CANCEL_COMPLETED"


# --- FR47b: internal errors escalate -------------------------------------------------------------


class _NotFoundPolicies(MockPolicyService):
    def get_status(self, policy_id: str) -> dict[str, Any]:
        raise ToolError(errors.NOT_FOUND, "vanished")


class _FailingClaims(MockClaimService):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def create_claim(self, **kwargs: Any) -> dict[str, Any]:
        raise ToolError(self.code, "internal")


class _FailingCancellations(MockCancellationService):
    def cancel_policy(self, **kwargs: Any) -> dict[str, Any]:
        raise ToolError(errors.APPROVAL_REQUIRED, "missing approval")


def _services(**overrides: Any) -> Services:
    base = {
        "policies": MockPolicyService(),
        "claims": MockClaimService(),
        "cancellations": MockCancellationService(),
        "writes": MockWriteLookupService(),
    }
    base.update(overrides)
    return Services(**base)


def claim_confirm(c: Conversation) -> str:
    for text in (
        "I got rear-ended yesterday.",
        "POL-100001, born 4/12/1985",
        "Main St & 5th Ave",
        "No injuries, yes another car",
        "about 2500, no report",
    ):
        c.say(text)
    return c.reply("yes")


def assert_escalated(state: dict[str, Any], reply: str) -> None:
    assert "Something went wrong on our side" in reply
    assert state["status"] == "escalated"
    assert state["status"] != "failed_retryable"
    assert state["last_outcome"] == "ESCALATED"
    assert state["pending_action"] is None
    assert metrics.get("escalations_total", reason="internal_error") >= 1


@pytest.mark.parametrize("code", [errors.CONFLICT, "SOMETHING_UNEXPECTED"])
def test_internal_write_errors_escalate(convo: Convo, code: str) -> None:
    set_services(_services(claims=_FailingClaims(code)))
    c = convo()
    reply = claim_confirm(c)
    assert_escalated(c.state, reply)


def test_not_found_after_verification_escalates(convo: Convo) -> None:
    set_services(_services(policies=_NotFoundPolicies()))
    c = convo()
    reply = c.reply("is my policy active? POL-100001 born 4/12/1985")
    assert_escalated(c.state, reply)
    assert "POL-100001" not in reply  # nothing enumerated


def test_approval_required_escalates(runtime: SupportRuntime) -> None:
    set_services(_services(cancellations=_FailingCancellations()))
    tid = runtime.create_thread()
    runtime.post_message(tid, "I want to cancel my insurance.")
    runtime.post_message(tid, "POL-100001 / 04/12/1985")
    runtime.post_message(tid, "Nov 1, switching provider")
    result = runtime.post_message(tid, "yes")
    decision = runtime.decide(str(result.pending_approval_id), "approve", "marcus", None)
    assert_escalated(dict(runtime.snapshot(tid).values), " ".join(decision.messages))


def test_unauthorized_write_escalates(convo: Convo, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.errors import UnauthorizedWriteError

    module = importlib.import_module("app.graph.nodes.execute_write_action")

    def refuse(state: Any) -> None:
        raise UnauthorizedWriteError("forged")

    monkeypatch.setattr(module, "check_write_guard", refuse)
    c = convo()
    reply = claim_confirm(c)
    assert_escalated(c.state, reply)
    assert metrics.total("unauthorized_write_blocked_total") == 1
    assert claims("POL-100001") == []


# --- response guard (FR50, FR51) -----------------------------------------------------------------


def test_hallucinated_reference_falls_back_to_template(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push("respond", "Your policy POL-100001 is ACTIVE and claim CLM-2026-999999 is open.")
    c = convo()
    reply = c.reply("is my policy active? POL-100001 born 4/12/1985")
    assert "CLM-2026-999999" not in reply
    assert "Your policy POL-100001 for the 2022 Toyota Camry is ACTIVE." in reply
    assert metrics.get("response_guard_fallbacks_total", check="facts") == 1


def test_hallucinated_amount_and_date_fall_back(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push(
        "respond",
        "Your policy is ACTIVE until March 3, 2030 with a $99 deductible.",
        "Your policy POL-100001 is ACTIVE, paid through 2026-12-31.",
    )
    c = convo()
    first = c.reply("is my policy active? POL-100001 born 4/12/1985")
    assert "2030" not in first
    assert "$99" not in first
    second = c.reply("what's my status again?")
    assert second == "Your policy POL-100001 is ACTIVE, paid through 2026-12-31."


def test_coverage_language_is_filtered(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push(
        "respond", "Good news: your policy POL-100001 is active, so this will be covered!"
    )
    c = convo()
    reply = c.reply("is my policy active? POL-100001 born 4/12/1985")
    assert "covered!" not in reply
    assert metrics.get("response_guard_fallbacks_total", check="coverage") == 1


@pytest.mark.parametrize("question", ["will I be covered?", "Will this be paid?"])
def test_coverage_questions_mid_claim(convo: Convo, question: str) -> None:
    c = convo()
    c.say("I got rear-ended yesterday.")
    reply = c.reply(question)
    assert reply.startswith(
        "I can't determine coverage or payment. An adjuster will review your claim and "
        "determine eligibility."
    )
    assert c.state["active_intent"] == "report_claim"


# --- invocation wrappers (NFR13, NFR14) ----------------------------------------------------------


def test_recursion_limit_escalates(set_env: SetEnv, runtime: SupportRuntime) -> None:
    set_env(RECURSION_LIMIT="3")
    tid = runtime.create_thread()
    result = runtime.post_message(tid, "is my policy active? POL-100001 born 4/12/1985")
    assert result.messages == [
        "Something went wrong on our side while handling your request. Please call "
        "1-800-555-0199 and a support specialist will help you."
    ]
    assert result.status == "escalated"
    assert metrics.get("escalations_total", reason="recursion_limit") == 1


def test_unhandled_exception_keeps_status(
    runtime: SupportRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = runtime.create_thread()
    runtime.post_message(tid, "I got rear-ended yesterday.")
    assert runtime.snapshot(tid).values["status"] == "awaiting_info"
    module = importlib.import_module("app.graph.nodes.understand")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database exploded: secret details")

    monkeypatch.setattr(module, "structured_call", boom)
    result = runtime.post_message(tid, "POL-100001, born 4/12/1985")
    assert result.messages == [
        "I'm having trouble reaching our systems right now. Please try again in a few minutes, "
        "or call 1-800-555-0199."
    ]
    assert "secret" not in " ".join(result.messages)  # FR49
    state = runtime.snapshot(tid).values
    assert state["status"] == "awaiting_info"  # unchanged
    assert state["last_outcome"] == "SYSTEM_UNAVAILABLE"
    assert metrics.get("escalations_total", reason="unhandled") == 0
    monkeypatch.undo()
    result = runtime.post_message(tid, "POL-100001, born 4/12/1985")
    assert "Thanks, you're verified." in result.messages[0]  # processed normally


# --- fault-injection soak ------------------------------------------------------------------------

INTERNAL_RE = re.compile(r"Traceback|Exception|ToolError|[0-9a-f]{32}|SERVICE_UNAVAILABLE|TIMEOUT")


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_random_faults_never_crash_or_leak(set_env: SetEnv, convo: Convo, seed: int) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="0.5", MOCK_SEED=str(seed))
    c = convo()
    script = [
        "I got rear-ended yesterday.",
        "POL-100001, born 4/12/1985",
        "please try again",
        "Main St & 5th Ave",
        "No injuries, yes another car",
        "about 2500, no report",
        "yes",
        "please try again",
        "retry",
        "no",
    ]
    for text in script:
        for reply in c.say(text):
            assert not INTERNAL_RE.search(reply), reply
    assert len(claims("POL-100001")) <= 1
