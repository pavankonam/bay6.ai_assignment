"""M7: REST API (FR53–FR56, FR19b, NFR17, §9.3)."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlmodel import col

from app.approvals.models import Approval
from app.config import utcnow
from app.db import session_scope
from app.main import create_app

SetEnv = Callable[..., None]
REVIEWER = {"X-Reviewer-Token": "change-me"}
ADMIN = {"X-Admin-Token": "change-me"}


@pytest.fixture
def client(seeded: None) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def new_thread(client: TestClient) -> str:
    response = client.post("/threads")
    assert response.status_code == 200
    thread_id = response.json()["thread_id"]
    assert uuid.UUID(thread_id).version == 4
    return thread_id


def send(client: TestClient, thread_id: str, content: str) -> dict[str, Any]:
    response = client.post(f"/threads/{thread_id}/messages", json={"content": content})
    assert response.status_code == 200, response.text
    return response.json()


def submit_cancellation(client: TestClient) -> tuple[str, str]:
    tid = new_thread(client)
    send(client, tid, "I want to cancel my insurance.")
    send(client, tid, "POL-100001 / 04/12/1985")
    send(client, tid, "Nov 1, switching provider")
    body = send(client, tid, "yes")
    assert body["status"] == "awaiting_approval"
    return tid, body["pending_approval_id"]


def test_flow_a_over_api(client: TestClient) -> None:
    tid = new_thread(client)
    first = send(client, tid, "Hi, can you check if my policy is still active?")
    assert first["status"] == "awaiting_info"
    assert first["pending_approval_id"] is None
    body = send(client, tid, "POL-100001, born April 12 1985")
    assert body["status"] == "completed"
    (message,) = body["messages"]  # no tool calls / ToolMessages returned (FR53)
    assert "is ACTIVE" in message


def test_message_length_limit(client: TestClient) -> None:
    tid = new_thread(client)
    ok = client.post(f"/threads/{tid}/messages", json={"content": "x" * 2000})
    assert ok.status_code == 200
    too_long = client.post(f"/threads/{tid}/messages", json={"content": "x" * 2001})
    assert too_long.status_code == 422


def test_client_message_id_is_accepted(client: TestClient) -> None:
    tid = new_thread(client)
    response = client.post(
        f"/threads/{tid}/messages", json={"content": "hi", "client_message_id": "m-1"}
    )
    assert response.status_code == 200


def test_new_thread_id_starts_fresh_and_unknown_history_404(client: TestClient) -> None:
    assert client.get(f"/threads/{uuid.uuid4()}").status_code == 404
    tid = str(uuid.uuid4())
    body = send(client, tid, "hello")  # FR2: a new thread_id starts a fresh state
    assert body["messages"]


def test_history_is_customer_visible_masked_and_ordered(client: TestClient) -> None:
    tid, approval_id = submit_cancellation(client)
    send(client, tid, "any update?")
    history = client.get(f"/threads/{tid}").json()
    assert history["status"] == "awaiting_approval"
    assert history["pending_approval_id"] == approval_id
    messages = history["messages"]
    stamps = [m["created_at"] for m in messages]
    assert stamps == sorted(stamps)
    assert messages[-1]["source"] == "side"
    assert messages[-1]["content"].startswith(f"Your request {approval_id} is still pending")
    assert all("04/12/1985" not in m["content"] for m in messages)
    assert any("[DOB]" in m["content"] for m in messages if m["role"] == "user")
    assert {m["role"] for m in messages} == {"user", "assistant"}


def test_state_endpoint_requires_admin(client: TestClient) -> None:
    tid = new_thread(client)
    send(client, tid, "hello")
    assert client.get(f"/threads/{tid}/state").status_code == 401
    assert (
        client.get(f"/threads/{tid}/state", headers={"X-Admin-Token": "wrong"}).status_code == 401
    )
    response = client.get(f"/threads/{tid}/state", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["values"]["turn_count"] == 1
    assert client.get(f"/threads/{uuid.uuid4()}/state", headers=ADMIN).status_code == 404


def test_reviewer_endpoints_require_token(client: TestClient) -> None:
    assert client.get("/approvals").status_code == 401
    assert client.get("/approvals", headers={"X-Reviewer-Token": "nope"}).status_code == 401
    assert (
        client.post(
            "/approvals/APR-AAAAA/decision", json={"decision": "approve", "reviewer_id": "m"}
        ).status_code
        == 401
    )


def test_approval_flow_over_api(client: TestClient) -> None:
    tid, approval_id = submit_cancellation(client)
    pending = client.get("/approvals", params={"status": "pending"}, headers=REVIEWER).json()
    assert [a["approval_id"] for a in pending["approvals"]] == [approval_id]
    assert pending["approvals"][0]["customer"]["dob_masked"] == "****-**-12"
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reviewer_id": "marcus", "comment": "Verified, processed."},
        headers=REVIEWER,
    )
    assert decision.status_code == 200
    assert decision.json() == {
        "approval_id": approval_id,
        "status": "approved",
        "thread_status": "completed",
    }
    again = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "reject", "reviewer_id": "marcus"},
        headers=REVIEWER,
    )
    assert again.status_code == 409
    history = client.get(f"/threads/{tid}").json()["messages"]
    assert history[-1]["content"].startswith("Your cancellation was approved.")  # FR44
    approved = client.get("/approvals", params={"status": "approved"}, headers=REVIEWER).json()
    assert [a["approval_id"] for a in approved["approvals"]] == [approval_id]


def test_decision_validation_and_404(client: TestClient) -> None:
    bad = client.post(
        "/approvals/APR-AAAAA/decision",
        json={"decision": "maybe", "reviewer_id": "m"},
        headers=REVIEWER,
    )
    assert bad.status_code == 422
    missing = client.post(
        "/approvals/APR-AAAAA/decision",
        json={"decision": "approve", "reviewer_id": "m"},
        headers=REVIEWER,
    )
    assert missing.status_code == 404
    assert client.get("/approvals", params={"status": "weird"}, headers=REVIEWER).status_code == 422


def test_late_decision_409_unsticks_thread(client: TestClient) -> None:
    tid, approval_id = submit_cancellation(client)
    created = (utcnow() - timedelta(hours=49)).isoformat()
    with session_scope() as session:
        session.exec(  # type: ignore[call-overload]
            update(Approval)
            .where(col(Approval.approval_id) == approval_id)
            .values(created_at=created)
        )
    response = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reviewer_id": "marcus"},
        headers=REVIEWER,
    )
    assert response.status_code == 409
    history = client.get(f"/threads/{tid}").json()["messages"]
    assert history[-1]["content"].startswith(f"Your request {approval_id} expired")
    body = send(client, tid, "yes")
    assert "still pending" not in " ".join(body["messages"])
    assert body["status"] == "awaiting_confirmation"


def test_rate_limit(set_env: SetEnv, seeded: None) -> None:
    set_env(RATE_LIMIT_PER_MINUTE="3")
    with TestClient(create_app()) as client:
        codes = [client.post("/threads").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        assert client.get("/healthz").status_code == 200  # only the customer endpoints


def test_healthz_and_metrics(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    tid = new_thread(client)
    send(client, tid, "hello")
    text = client.get("/metrics").text
    assert "# TYPE turns_total counter" in text
    assert "turns_total 1" in text
    import app.main as main_module

    def broken() -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(main_module, "get_engine", broken)
    assert client.get("/healthz").json() == {"status": "degraded"}


def test_database_unavailable_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = client.app.state.runtime  # type: ignore[attr-defined]

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "post_message", broken)
    tid = new_thread(client)
    response = client.post(f"/threads/{tid}/messages", json={"content": "hi"})
    assert response.status_code == 503
    assert "database" not in response.text.lower()


def test_no_internal_identifiers_in_responses(client: TestClient) -> None:
    tid = new_thread(client)
    for text in (
        "I got rear-ended yesterday.",
        "POL-100001, born 4/12/1985",
        "Main St & 5th Ave",
        "No injuries, yes another car",
        "about 2500, no report",
        "yes",
    ):
        body = send(client, tid, text)
        for message in body["messages"]:
            assert not re.search(r"[0-9a-f]{32}|call_|Traceback|idempotency", message)


def test_startup_warns_without_api_key(
    seeded: None, set_env: SetEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import os

    from app.observability import logging as app_logging

    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY"):  # restored to absent at teardown
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)

    stream = io.StringIO()
    app_logging.configure_logging("INFO", stream=stream, force=True)
    set_env(LLM_PROVIDER="anthropic", LANGSMITH_API_KEY="ls-test")
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").status_code == 200
    finally:
        app_logging.configure_logging("INFO", force=True)
    output = stream.getvalue()
    assert os.environ.get("LANGSMITH_TRACING") == "true"  # NFR16: enabled only with a key
    assert "llm_key_missing" in output
    assert "ls-test" not in output  # secrets are never logged (NFR6)
