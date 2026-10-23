"""M9 hardening: multi-intent queue (FR10), message idempotency (FR53a), per-policy lockout
(FR19a), reconciliation (FR43a, incl. crash-before-resume), retention purge (NFR10)."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlmodel import col, select

from app.approvals.models import (
    Approval,
    ProcessedMessage,
    ThreadSideMessage,
    VerificationFailure,
)
from app.approvals.reconcile import reconcile
from app.approvals.service import approvals_service
from app.concurrency import ThreadLocks
from app.config import utcnow
from app.db import session_scope
from app.graph.builder import build_graph
from app.graph.checkpointer import close_checkpointer, open_checkpointer
from app.main import create_app
from app.mock_api.repository import Cancellation, Claim
from app.nlu.schemas import UnderstandResult
from app.observability.metrics import metrics
from app.runtime import SupportRuntime
from app.security import recent_failures
from tests.fixtures.fake_llm import FakeScript
from tests.integration.conftest import Conversation

Convo = Callable[..., Conversation]
SetEnv = Callable[..., None]
ADMIN = {"X-Admin-Token": "change-me"}


def cancellations() -> list[Cancellation]:
    with session_scope() as session:
        return list(session.exec(select(Cancellation)).all())


def claims(policy_id: str) -> list[Claim]:
    with session_scope() as session:
        return list(session.exec(select(Claim).where(Claim.policy_id == policy_id)).all())


# --- FR10 multi-intent queue ---------------------------------------------------------------------

MULTI = "check my status and cancel it November 1 because I'm switching providers"


def test_multi_intent_queue_carries_slots(convo: Convo) -> None:
    c = convo()
    c.say(MULTI)
    state = c.state
    assert state["active_intent"] == "policy_status"
    assert state["intent_queue"] == [
        {
            "intent": "cancel_policy",
            "slots": {"effective_date": "2026-11-01", "reason": "switching_provider"},
        }
    ]
    replies = c.say("POL-100001, born 4/12/1985")
    assert "is ACTIVE" in replies[0]
    assert replies[-1] == (
        "You also mentioned requesting a policy cancellation. Would you like to continue with that?"
    )
    assert c.state["pending_question"] == {
        "kind": "next_task",
        "context": {"source": "queue", "intent": "cancel_policy"},
    }
    summary = c.reply("yes")
    assert "When should the cancellation take effect" not in summary  # neither date nor reason
    assert "Cancelling POL-100001 effective 2026-11-01" in summary
    assert "(reason: switching provider)" in summary
    assert c.state["intent_queue"] == []
    assert c.state["status"] == "awaiting_confirmation"


def test_multi_intent_queue_decline(convo: Convo) -> None:
    c = convo()
    c.say(MULTI)
    c.say("POL-100001, born 4/12/1985")
    reply = c.reply("no thanks")
    assert reply.startswith("Okay.")
    state = c.state
    assert state["intent_queue"] == []
    assert state["active_intent"] is None
    assert "get_cancellation_quote" not in c.tool_names()


def test_multi_intent_from_llm_output(convo: Convo, fake_llm: FakeScript) -> None:
    fake_llm.push(
        "classify_extract",
        UnderstandResult(
            intents=["report_claim", "cancel_policy"],
            confidence=0.95,
            task_slots={
                "report_claim": {"incident_location": "Main St"},
                "cancel_policy": {"reason": "cost"},
            },
        ),
    )
    c = convo()
    c.say("I crashed and after that I want out of my policy, it's too pricey")
    state = c.state
    assert state["active_intent"] == "report_claim"
    assert state["slots"]["incident_location"] == "Main St"
    assert "reason" not in state["slots"]  # other intents' slots go to their queue entry
    assert state["intent_queue"] == [{"intent": "cancel_policy", "slots": {"reason": "cost"}}]


# --- FR53a client_message_id ---------------------------------------------------------------------


def test_retried_confirmation_is_not_reprocessed(runtime: SupportRuntime) -> None:
    tid = runtime.create_thread()
    for text in (
        "I got rear-ended yesterday.",
        "POL-100001, born 4/12/1985",
        "Main St & 5th Ave, Springfield",
        "No injuries, yes another car",
        "about 2500, no report",
    ):
        runtime.post_message(tid, text)
    first = runtime.post_message(tid, "yes", client_message_id="msg-yes-1")
    turns = runtime.snapshot(tid).values["turn_count"]
    retry = runtime.post_message(tid, "yes", client_message_id="msg-yes-1")
    assert retry.to_dict() == first.to_dict()
    assert runtime.snapshot(tid).values["turn_count"] == turns
    assert len(claims("POL-100001")) == 1


def test_retried_slot_filling_message(runtime: SupportRuntime) -> None:
    tid = runtime.create_thread()
    runtime.post_message(tid, "I got rear-ended yesterday.")
    first = runtime.post_message(tid, "POL-100001, born 4/12/1985", client_message_id="m-2")
    retry = runtime.post_message(tid, "POL-100001, born 4/12/1985", client_message_id="m-2")
    assert retry.messages == first.messages
    values = runtime.snapshot(tid).values
    assert values["turn_count"] == 2
    human_turns = [m for m in values["messages"] if m.type == "human"]
    assert len(human_turns) == 2
    with session_scope() as session:
        assert len(session.exec(select(ProcessedMessage)).all()) == 1
    other = runtime.post_message(tid, "Main St", client_message_id="m-3")
    assert other.messages != first.messages


def test_client_message_id_over_api(seeded: None) -> None:
    with TestClient(create_app()) as client:
        tid = client.post("/threads").json()["thread_id"]
        body = {
            "content": "is my policy active? POL-100001 born 4/12/1985",
            "client_message_id": "abc",
        }
        first = client.post(f"/threads/{tid}/messages", json=body).json()
        second = client.post(f"/threads/{tid}/messages", json=body).json()
        assert first == second
        assert metrics.get("turns_total") == 1


# --- FR19a per-policy lockout --------------------------------------------------------------------


def fail_verification(c: Conversation, dob: str) -> str:
    c.say("is my policy active?")
    return c.reply(f"POL-100001, born {dob}")


def test_cross_thread_lockout_non_extension_and_unlock(convo: Convo, seeded: None) -> None:
    attackers = [convo(), convo()]
    guesses = ["1/1/1980", "1/2/1980", "1/3/1980", "1/4/1980", "1/5/1980"]
    fail_verification(attackers[0], guesses[0])
    for dob in guesses[1:3]:
        attackers[0].say(f"POL-100001, born {dob}")  # 3rd failure locks thread A
    assert attackers[0].state["status"] == "locked"
    fail_verification(attackers[1], guesses[3])
    attackers[1].say(f"POL-100001, born {guesses[4]}")
    assert recent_failures("POL-100001") == 5
    assert metrics.total("verification_lockouts_total") == 1

    victim = convo()
    reply = fail_verification(victim, "4/12/1985")  # the correct DOB
    assert "I couldn't verify those details." in reply
    assert "For your security, please call 1-800-555-0199 to continue." in reply
    assert victim.state["verified"] is False
    assert recent_failures("POL-100001") == 5  # attempts while locked are not recorded
    with TestClient(create_app()) as client:
        assert client.post("/admin/policies/POL-100001/unlock").status_code == 401
        response = client.post("/admin/policies/pol100001/unlock", headers=ADMIN)
        assert response.json() == {"policy_id": "POL-100001", "failures_removed": 5}
    reply = victim.reply("POL-100001, born 4/12/1985")
    assert "is ACTIVE" in reply


def test_lockout_window_expires(convo: Convo, set_env: SetEnv) -> None:
    set_env(POLICY_VERIFY_LOCK_FAILURES="1")
    c = convo()
    fail_verification(c, "1/1/1980")
    stale = (utcnow() - timedelta(hours=25)).isoformat()
    with session_scope() as session:
        session.exec(update(VerificationFailure).values(failed_at=stale))  # type: ignore[call-overload]
    assert recent_failures("POL-100001") == 0
    assert "is ACTIVE" in convo().reply("is my policy active? POL-100001 born 4/12/1985")


# --- FR43a reconciliation ------------------------------------------------------------------------


def submit_cancellation(
    rt: SupportRuntime, identity: str = "POL-100001 / 04/12/1985"
) -> tuple[str, str]:
    tid = rt.create_thread()
    rt.post_message(tid, "I want to cancel my insurance.")
    rt.post_message(tid, identity)
    rt.post_message(tid, "Nov 1, switching provider")
    result = rt.post_message(tid, "yes")
    return tid, str(result.pending_approval_id)


def test_crash_between_decision_and_resume_reconciled_on_restart(
    seeded: None, tmp_path: Path
) -> None:
    path = tmp_path / "cp.db"
    saver = open_checkpointer(path)
    first = SupportRuntime(build_graph(saver), locks=ThreadLocks())
    tid, approval_id = submit_cancellation(first)
    assert approvals_service.decide(approval_id, "approve", "marcus", "ok")  # DB updated…
    close_checkpointer(saver)  # …then the process dies before resume_thread()

    saver = open_checkpointer(path)
    second = SupportRuntime(build_graph(saver), locks=ThreadLocks())
    report = reconcile(second)
    assert report.resumed == 1
    assert len(cancellations()) == 1
    assert approvals_service.get(approval_id).resume_status == "done"  # type: ignore[union-attr]
    texts = [m["content"] for m in second.history(tid)["messages"]]
    assert texts[-1].startswith("Your cancellation was approved.")
    assert reconcile(second).resumed == 0  # idempotent
    assert len(cancellations()) == 1
    close_checkpointer(saver)


def test_startup_reconciliation_and_admin_endpoint(seeded: None) -> None:
    with TestClient(create_app()) as client:
        rt: SupportRuntime = client.app.state.runtime  # type: ignore[attr-defined]
        tid, approval_id = submit_cancellation(rt)
        approvals_service.decide(approval_id, "reject", "marcus", "no")
        response = client.post("/admin/approvals/reconcile", headers=ADMIN)
        assert response.json() == {"resumed": 1, "expired": 0, "repaired_interrupts": 0}
        assert rt.snapshot(tid).values["last_outcome"] == "CANCEL_REJECTED"
        # Decided while "down": startup reconciliation (lifespan) resumes it.
        tid2, approval2 = submit_cancellation(rt, "POL-100004 / 11/03/1995")
        approvals_service.decide(approval2, "approve", "marcus", None)
    with TestClient(create_app()) as client:
        rt = client.app.state.runtime  # type: ignore[attr-defined]
        assert rt.snapshot(tid2).values["last_outcome"] == "CANCEL_COMPLETED"
        assert client.post("/admin/approvals/reconcile").status_code == 401


def test_reconcile_expires_and_repairs(seeded: None, tmp_path: Path) -> None:
    saver = open_checkpointer(tmp_path / "cp.db")
    crashing = SupportRuntime(
        build_graph(saver, interrupt_before=["human_approval"]), locks=ThreadLocks()
    )
    tid, approval_id = submit_cancellation(crashing)
    rt = SupportRuntime(build_graph(saver), locks=ThreadLocks())
    report = reconcile(rt)
    assert report.repaired_interrupts == 1
    assert len(rt.snapshot(tid).interrupts) == 1
    stale = (utcnow() - timedelta(hours=49)).isoformat()
    with session_scope() as session:
        session.exec(  # type: ignore[call-overload]
            update(Approval)
            .where(col(Approval.approval_id) == approval_id)
            .values(created_at=stale)
        )
    report = reconcile(rt)
    assert (report.expired, report.resumed) == (1, 1)
    assert rt.snapshot(tid).values["last_outcome"] == "APPROVAL_EXPIRED"
    close_checkpointer(saver)


# --- NFR10 purge ---------------------------------------------------------------------------------


@pytest.fixture
def api(seeded: None) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def test_purge_removes_both_dbs_and_skips_pending(api: TestClient, checkpoint_path: Any) -> None:
    rt: SupportRuntime = api.app.state.runtime  # type: ignore[attr-defined]
    done = rt.create_thread()
    rt.post_message(done, "I want to cancel my insurance.")
    rt.post_message(done, "POL-100004 / 11/03/1995")
    rt.post_message(done, "Nov 1, cost")
    result = rt.post_message(done, "yes")
    rt.post_message(done, "any update? born 11/3/1995")  # side message with a DOB
    rt.decide(str(result.pending_approval_id), "reject", "marcus", None)
    pending, _ = submit_cancellation(rt)
    rt.post_message(pending, "hello", client_message_id="keep-1")

    assert api.post("/admin/purge", params={"older_than_days": 0}).status_code == 401
    report = api.post("/admin/purge", params={"older_than_days": 0}, headers=ADMIN).json()
    assert report["purged"] == [done]
    assert report["skipped"] == [pending]

    assert rt.snapshot(done).values == {}
    assert api.get(f"/threads/{done}").status_code == 404
    assert rt.snapshot(pending).next == ("human_approval",)  # untouched
    with session_scope() as session:
        for model in (Approval, ThreadSideMessage, ProcessedMessage):
            rows = session.exec(select(model).where(model.thread_id == done)).all()
            assert rows == []
        assert session.exec(select(Approval).where(Approval.thread_id == pending)).all()
        assert session.exec(select(Cancellation)).all() == []  # rejected → never written
    assert claims("POL-100006")  # mock business records are kept
    checkpointer: Any = rt.graph.checkpointer
    path = checkpointer.conn.execute("PRAGMA database_list").fetchone()[2]
    raw = Path(path).read_bytes()
    for needle in (b"1995-11-03", b"11/03/1995", b"11/3/1995"):
        assert needle not in raw  # no raw DOB for the purged thread remains
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (done,)
            ).fetchone()[0]
            == 0
        )
    del checkpoint_path


def test_purge_keeps_recent_threads_and_trims_failures(api: TestClient) -> None:
    rt: SupportRuntime = api.app.state.runtime  # type: ignore[attr-defined]
    tid = rt.create_thread()
    rt.post_message(tid, "is my policy active? POL-100001 born 1/1/1980")
    old = (utcnow() - timedelta(days=40)).isoformat()
    with session_scope() as session:
        session.add(VerificationFailure(policy_id="POL-100002", thread_id="x", failed_at=old))
    report = api.post("/admin/purge", params={"older_than_days": 30}, headers=ADMIN).json()
    assert report == {"purged": [], "skipped": [], "verification_failures_removed": 1}
    assert rt.snapshot(tid).values["turn_count"] == 1
