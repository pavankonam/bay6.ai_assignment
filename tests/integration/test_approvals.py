"""M5 integration: human-in-the-loop approval (Section 8.8, FR37–FR44, NFR11, NFR12, NFR14a)."""

from __future__ import annotations

import re
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlmodel import col, select

from app.approvals.models import Approval
from app.approvals.service import approvals_service
from app.concurrency import ThreadLocks
from app.config import utcnow
from app.db import session_scope
from app.graph.builder import CompiledGraph, build_graph
from app.graph.checkpointer import close_checkpointer, open_checkpointer
from app.mock_api.repository import Cancellation, Claim
from app.runtime import ConflictError, MessageResult, NotFoundError, SupportRuntime

APR_RE = re.compile(r"APR-[A-Z2-7]{5}")


@pytest.fixture
def runtime(graph: CompiledGraph) -> SupportRuntime:
    return SupportRuntime(graph, locks=ThreadLocks())


def cancellations() -> list[Cancellation]:
    with session_scope() as session:
        return list(session.exec(select(Cancellation)).all())


def claims(policy_id: str) -> list[Claim]:
    with session_scope() as session:
        return list(session.exec(select(Claim).where(Claim.policy_id == policy_id)).all())


def backdate(approval_id: str, hours: float) -> None:
    created = (utcnow() - timedelta(hours=hours)).isoformat()
    with session_scope() as session:
        session.exec(  # type: ignore[call-overload]
            update(Approval)
            .where(col(Approval.approval_id) == approval_id)
            .values(created_at=created)
        )


def submit_cancellation(
    rt: SupportRuntime, thread_id: str | None = None
) -> tuple[str, MessageResult]:
    tid = thread_id or rt.create_thread()
    rt.post_message(tid, "I want to cancel my insurance.")
    rt.post_message(tid, "POL-100001 / 04/12/1985")
    rt.post_message(tid, "Nov 1, switching provider")
    return tid, rt.post_message(tid, "yes")


def history_texts(rt: SupportRuntime, tid: str) -> list[str]:
    return [m["content"] for m in rt.history(tid)["messages"]]


# --- Flow C --------------------------------------------------------------------------------------


def test_flow_c_approve(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    (message,) = result.messages
    approval_id = APR_RE.search(message).group(0)  # type: ignore[union-attr]
    assert message == (
        "Your request has been sent to a support specialist for approval. "
        f"Reference: {approval_id}. You'll get a response here once it's reviewed. If you need "
        "help with something else in the meantime, please start a new conversation."
    )
    assert result.status == "awaiting_approval"
    assert result.pending_approval_id == approval_id
    snapshot = runtime.snapshot(tid)
    assert snapshot.next == ("human_approval",)
    (pending,) = snapshot.interrupts
    payload = pending.value
    assert payload["approval_id"] == approval_id
    assert payload["action_type"] == "cancel_policy"
    assert payload["customer"] == {
        "policy_id": "POL-100001",
        "holder_name": "Jane Doe",
        "dob_masked": "****-**-12",
    }
    assert payload["payload"] == {
        "policy_id": "POL-100001",
        "effective_date": "2026-11-01",
        "reason": "switching_provider",
    }
    assert payload["risk_flags"] == ["cancellation_requires_review"]
    assert len(re.findall(r"[.!?](\s|$)", payload["summary"])) <= 3
    assert "1985" not in str(payload)
    listed = approvals_service.list_approvals(status="pending")
    assert [a["approval_id"] for a in listed] == [approval_id]
    assert cancellations() == []

    decision = runtime.decide(approval_id, "approve", "marcus", "Verified, processed.")
    assert decision.status == "approved"
    (row,) = cancellations()
    assert row.cancellation_id == "CNL-2026-000001"
    assert row.approval_id == approval_id
    final = (
        "Your cancellation was approved. Policy POL-100001 will be cancelled effective "
        "2026-11-01. Confirmation: CNL-2026-000001. Estimated refund $175.55 within 7–10 "
        'business days. Note from specialist: "Verified, processed."'
    )
    assert decision.messages[0].startswith(final)
    texts = history_texts(runtime, tid)  # FR44: delivered via the thread history
    assert any(t.startswith(final) for t in texts)
    assert sum("sent to a support specialist" in t for t in texts) == 1  # FR38: not repeated
    state = runtime.snapshot(tid).values
    assert state["status"] == "completed"
    assert state["last_outcome"] == "CANCEL_COMPLETED"
    assert approvals_service.get(approval_id).resume_status == "done"  # type: ignore[union-attr]


def test_flow_d_reject(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    approval_id = result.pending_approval_id
    assert approval_id
    decision = runtime.decide(approval_id, "reject", "marcus", "Please call to discuss options.")
    assert cancellations() == []
    (message,) = decision.messages
    assert "was reviewed and not approved" in message
    assert 'Note from specialist: "Please call to discuss options."' in message
    assert "1-800-555-0199" in message
    state = runtime.snapshot(tid).values
    assert state["last_outcome"] == "CANCEL_REJECTED"
    assert state["status"] == "completed"
    assert state["pending_action"] is None


def test_claim_with_approval(runtime: SupportRuntime) -> None:
    tid = runtime.create_thread()
    for text in (
        "I got rear-ended yesterday.",
        "POL-100001, born 4/12/1985",
        "Main St & 5th Ave, Springfield",
        "No injuries, yes another car",
        "skip",
    ):
        runtime.post_message(tid, text)
    result = runtime.post_message(tid, "yes")
    assert result.status == "awaiting_approval"
    assert claims("POL-100001") == []
    decision = runtime.decide(str(result.pending_approval_id), "approve", "marcus", None)
    (claim,) = claims("POL-100001")
    assert claim.approval_id == result.pending_approval_id
    assert decision.messages[0].startswith(
        f"Your claim was approved and has been filed. Claim number: {claim.claim_id}."
    )


# --- messages while pending ----------------------------------------------------------------------


def test_message_while_pending_does_not_invoke_graph(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    before = runtime.snapshot(tid)
    reply = runtime.post_message(tid, "any update? also can you check my claim status")
    assert reply.messages == [
        f"Your request {approval_id} is still pending review. If you need help with something "
        "else, please start a new conversation."
    ]
    assert reply.status == "awaiting_approval"
    after = runtime.snapshot(tid)
    assert (
        after.config["configurable"]["checkpoint_id"]
        == before.config["configurable"]["checkpoint_id"]
    )
    assert after.next == ("human_approval",)
    assert len(after.interrupts) == 1
    texts = history_texts(runtime, tid)
    assert texts[-2] == "any update? also can you check my claim status"
    assert texts[-1].startswith(f"Your request {approval_id} is still pending")
    roles = [m["role"] for m in runtime.history(tid)["messages"]]
    assert roles[-2:] == ["user", "assistant"]


def test_double_decision_conflicts(runtime: SupportRuntime) -> None:
    _, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    runtime.decide(approval_id, "approve", "marcus", None)
    with pytest.raises(ConflictError):
        runtime.decide(approval_id, "reject", "other", None)
    assert approvals_service.get(approval_id).status == "approved"  # type: ignore[union-attr]
    assert len(cancellations()) == 1


def test_unknown_approval(runtime: SupportRuntime) -> None:
    with pytest.raises(NotFoundError):
        runtime.decide("APR-ZZZZZ", "approve", "marcus", None)


def test_history_unknown_thread(runtime: SupportRuntime) -> None:
    with pytest.raises(NotFoundError):
        runtime.history("no-such-thread")


# --- expiry (FR43) -------------------------------------------------------------------------------


def test_flow_g_expiry_on_customer_message_then_restart(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    old_action_id = runtime.snapshot(tid).values["pending_action"]["pending_action_id"]
    backdate(approval_id, 49)
    reply = runtime.post_message(tid, "any update?")
    assert reply.messages == [
        f"Your request {approval_id} expired before it could be reviewed. "
        "Would you like me to submit it again?"
    ]
    approval = approvals_service.get(approval_id)
    assert approval is not None
    assert approval.status == "expired"
    assert approval.resume_status == "done"
    state = runtime.snapshot(tid).values
    assert state["last_outcome"] == "APPROVAL_EXPIRED"
    assert state["pending_question"]["kind"] == "restart_expired"
    assert "still pending" not in " ".join(history_texts(runtime, tid))
    restart = runtime.post_message(tid, "yes")
    state = runtime.snapshot(tid).values
    assert state["active_intent"] == "cancel_policy"
    assert state["status"] == "awaiting_confirmation"
    assert state["pending_action"]["pending_action_id"] != old_action_id
    assert "Estimated refund: $175.55" in restart.messages[-1]
    # A fresh request means a fresh approval (new idempotency key).
    final = runtime.post_message(tid, "yes")
    assert final.pending_approval_id not in (None, approval_id)
    assert cancellations() == []


def test_expiry_then_different_intent(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    backdate(str(result.pending_approval_id), 49)
    runtime.post_message(tid, "any update?")
    reply = runtime.post_message(tid, "what's the status of my policy?")
    assert "is ACTIVE" in reply.messages[-1]
    state = runtime.snapshot(tid).values
    assert state["pending_question"] is None
    assert state["active_intent"] is None


def test_late_reviewer_decision_unsticks_thread(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    backdate(approval_id, 49)
    with pytest.raises(ConflictError):
        runtime.decide(approval_id, "approve", "marcus", "late")
    approval = approvals_service.get(approval_id)
    assert approval is not None
    assert approval.status == "expired"
    assert approval.resume_status == "done"
    assert cancellations() == []
    snapshot = runtime.snapshot(tid)
    assert snapshot.next == ()  # no longer stuck at the interrupt
    texts = history_texts(runtime, tid)
    assert texts[-1] == (
        f"Your request {approval_id} expired before it could be reviewed. "
        "Would you like me to submit it again?"
    )
    reply = runtime.post_message(tid, "yes")
    assert not any("still pending" in m for m in reply.messages)
    assert "Do you want to submit this cancellation request?" in reply.messages[-1]


def test_read_paths_display_expiry_without_persisting(runtime: SupportRuntime) -> None:
    _, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    backdate(approval_id, 49)
    shown = approvals_service.list_approvals(status="expired")
    assert [a["approval_id"] for a in shown] == [approval_id]
    assert approvals_service.list_approvals(status="pending") == []
    assert approvals_service.get(approval_id).status == "pending"  # type: ignore[union-attr]


# --- durability (NFR11) and idempotent resume (NFR12) --------------------------------------------


def test_restart_during_approval(seeded: None, tmp_path: Path) -> None:
    path = tmp_path / "durable.db"
    saver = open_checkpointer(path)
    first = SupportRuntime(build_graph(saver), locks=ThreadLocks())
    tid, result = submit_cancellation(first)
    close_checkpointer(saver)  # "kill" the process

    saver = open_checkpointer(path)
    second = SupportRuntime(build_graph(saver), locks=ThreadLocks())
    assert second.snapshot(tid).next == ("human_approval",)
    second.decide(str(result.pending_approval_id), "approve", "marcus", None)
    assert len(cancellations()) == 1
    assert second.snapshot(tid).values["last_outcome"] == "CANCEL_COMPLETED"
    close_checkpointer(saver)


def test_crash_between_announce_and_interrupt(seeded: None, tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    saver = open_checkpointer(path)
    crashing = SupportRuntime(
        build_graph(saver, interrupt_before=["human_approval"]), locks=ThreadLocks()
    )
    tid, result = submit_cancellation(crashing)
    snapshot = crashing.snapshot(tid)
    assert snapshot.next == ("human_approval",)
    assert snapshot.interrupts == ()  # announce checkpointed, interrupt never ran
    approval_id = str(result.pending_approval_id)
    close_checkpointer(saver)

    saver = open_checkpointer(path)
    restarted = SupportRuntime(build_graph(saver), locks=ThreadLocks())
    reply = restarted.post_message(tid, "hello?")
    assert reply.messages == [restarted_pending(approval_id)]
    snapshot = restarted.snapshot(tid)
    assert snapshot.next == ("human_approval",)
    assert len(snapshot.interrupts) == 1
    texts = history_texts(restarted, tid)
    assert sum("sent to a support specialist" in t for t in texts) == 1
    restarted.decide(approval_id, "approve", "marcus", None)
    assert len(cancellations()) == 1
    close_checkpointer(saver)


def restarted_pending(approval_id: str) -> str:
    return (
        f"Your request {approval_id} is still pending review. If you need help with something "
        "else, please start a new conversation."
    )


def test_resuming_twice_writes_once(runtime: SupportRuntime) -> None:
    tid, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    approvals_service.decide(approval_id, "approve", "marcus", None)
    approval = approvals_service.get(approval_id)
    assert approval is not None
    runtime.resume_thread(approval)
    runtime.resume_thread(approval)
    assert len(cancellations()) == 1
    del tid


def test_customer_message_after_crashed_resume_completes_it(runtime: SupportRuntime) -> None:
    """Step 3b: decided in the DB but the resume never ran → the next message resumes it."""
    tid, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    approvals_service.decide(approval_id, "approve", "marcus", "ok")  # crash before resume
    reply = runtime.post_message(tid, "any news?")
    assert reply.messages[0].startswith("Your cancellation was approved.")
    assert len(cancellations()) == 1
    assert approvals_service.get(approval_id).resume_status == "done"  # type: ignore[union-attr]


# --- concurrency (NFR14a) ------------------------------------------------------------------------


@pytest.fixture
def barrier() -> threading.Barrier:
    return threading.Barrier(2, timeout=10)


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_decision_and_message(
    runtime: SupportRuntime, barrier: threading.Barrier, attempt: int
) -> None:
    tid, result = submit_cancellation(runtime)
    approval_id = str(result.pending_approval_id)
    outcomes: dict[str, object] = {}

    def customer() -> None:
        barrier.wait()
        outcomes["message"] = runtime.post_message(tid, "any update?")

    def reviewer() -> None:
        barrier.wait()
        outcomes["decision"] = runtime.decide(approval_id, "approve", "marcus", None)

    threads = [threading.Thread(target=customer), threading.Thread(target=reviewer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert set(outcomes) == {"message", "decision"}
    assert len(cancellations()) == 1
    texts = history_texts(runtime, tid)
    assert sum(t.startswith("Your cancellation was approved.") for t in texts) == 1
    assert sum("sent to a support specialist" in t for t in texts) == 1
    message = outcomes["message"]
    assert isinstance(message, MessageResult)
    if message.side:  # customer first: status reply, then the decision resumed the thread
        assert "still pending" in message.messages[0]
    assert runtime.snapshot(tid).values["last_outcome"] in {
        "CANCEL_COMPLETED",
        None,
        "SMALLTALK",
        "OUT_OF_SCOPE_ANSWERED",
    }
    del attempt
