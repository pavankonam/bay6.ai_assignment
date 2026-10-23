"""M7: CLI as an HTTP client of a live API (FR54), including CLI-vs-API concurrency."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from sqlmodel import select
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.db import session_scope
from app.main import create_app
from app.mock_api.repository import Cancellation

SetEnv = Callable[..., None]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_api(seeded: None, set_env: SetEnv) -> Iterator[str]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    set_env(SUPPORT_API_URL=url)
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("API did not start")
        time.sleep(0.02)
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def run_cli(*args: str, input_text: str | None = None) -> Any:
    return CliRunner().invoke(cli_app, list(args), input=input_text)


def api_send(url: str, tid: str, content: str) -> dict[str, Any]:
    response = httpx.post(f"{url}/threads/{tid}/messages", json={"content": content}, timeout=30)
    response.raise_for_status()
    return dict(response.json())


def submit_cancellation(url: str, identity: str = "POL-100001 / 04/12/1985") -> tuple[str, str]:
    tid = httpx.post(f"{url}/threads").json()["thread_id"]
    for text in ("I want to cancel my insurance.", identity, "Nov 1, switching provider"):
        api_send(url, tid, text)
    body = api_send(url, tid, "yes")
    return tid, str(body["pending_approval_id"])


def cancellations() -> list[Cancellation]:
    with session_scope() as session:
        return list(session.exec(select(Cancellation)).all())


def test_cli_reports_unreachable_api(set_env: SetEnv) -> None:
    set_env(SUPPORT_API_URL=f"http://127.0.0.1:{_free_port()}")
    for args in (
        ["approvals", "list"],
        ["approvals", "decide", "APR-AAAAA", "--approve"],
        ["chat"],
    ):
        result = run_cli(*args)
        assert result.exit_code == 1
        assert "API not reachable at http://127.0.0.1:" in result.output


def test_cli_chat(live_api: str) -> None:
    result = run_cli("chat", input_text="is my policy active? POL-100001 born 4/12/1985\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Thread " in result.output
    assert "is ACTIVE" in result.output


def test_cli_chat_refresh_shows_reviewer_outcome(live_api: str) -> None:
    tid, approval_id = submit_cancellation(live_api)
    assert run_cli("approvals", "decide", approval_id, "--approve").exit_code == 0
    result = run_cli("chat", "--thread-id", tid, input_text="/quit\n")
    assert result.exit_code == 0, result.output
    assert "Your cancellation was approved." in result.output  # shown when resuming
    other_tid, other_id = submit_cancellation(live_api, "POL-100004 / 11/03/1995")
    waiting = run_cli("chat", "--thread-id", other_tid, input_text="/quit\n")
    assert "sent to a support specialist" in waiting.output
    run_cli("approvals", "decide", other_id, "--reject", "--comment", "Call us")


def test_cli_chat_refresh_picks_up_decision(live_api: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewer decides while the customer is in `support chat`; /refresh shows the outcome."""
    tid, approval_id = submit_cancellation(live_api)
    import app.cli as cli_module

    real_prompt = cli_module.typer.prompt
    replies = iter(["/refresh", "/quit"])

    def prompt(*args: Any, **kwargs: Any) -> str:
        answer = next(replies)
        if answer == "/refresh":
            run_cli("approvals", "decide", approval_id, "--approve")
        return answer

    monkeypatch.setattr(cli_module.typer, "prompt", prompt)
    result = run_cli("chat", "--thread-id", tid)
    monkeypatch.setattr(cli_module.typer, "prompt", real_prompt)
    assert result.exit_code == 0, result.output
    assert result.output.count("Your cancellation was approved.") == 1


def test_cli_approvals_list_and_decide(live_api: str) -> None:
    tid, approval_id = submit_cancellation(live_api)
    listing = run_cli("approvals", "list")
    assert listing.exit_code == 0, listing.output
    assert approval_id in listing.output
    assert "Jane Doe" in listing.output
    assert "1985" not in listing.output
    decided = run_cli(
        "approvals",
        "decide",
        approval_id,
        "--approve",
        "--comment",
        "Verified",
        "--reviewer-id",
        "marcus",
    )
    assert decided.exit_code == 0, decided.output
    assert f"{approval_id}: approved" in decided.output
    again = run_cli("approvals", "decide", approval_id, "--reject")
    assert again.exit_code == 1
    assert "Error 409" in again.output
    history = httpx.get(f"{live_api}/threads/{tid}").json()["messages"]
    assert history[-1]["content"].startswith("Your cancellation was approved.")
    assert len(cancellations()) == 1
    empty = run_cli("approvals", "list")
    assert "No approvals." in empty.output


def test_cli_decide_requires_exactly_one_flag(live_api: str) -> None:
    assert run_cli("approvals", "decide", "APR-AAAAA").exit_code == 1
    assert run_cli("approvals", "decide", "APR-AAAAA", "--approve", "--reject").exit_code == 1


def test_cli_wrong_token_fails_cleanly(live_api: str, set_env: SetEnv) -> None:
    set_env(REVIEWER_TOKEN="wrong-token")
    result = run_cli("approvals", "list")
    assert result.exit_code == 1
    assert "Error 401" in result.output


def test_cli_truncates_long_messages(live_api: str) -> None:
    result = run_cli("chat", input_text="x" * 2100 + "\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Message truncated to 2000 characters." in result.output


@pytest.mark.parametrize("attempt", range(3))
def test_cli_decision_and_api_message_concurrently(live_api: str, attempt: int) -> None:
    tid, approval_id = submit_cancellation(live_api)
    barrier = threading.Barrier(2, timeout=10)
    results: dict[str, Any] = {}

    def customer() -> None:
        barrier.wait()
        results["message"] = api_send(live_api, tid, "any update?")

    def reviewer() -> None:
        barrier.wait()
        results["cli"] = run_cli("approvals", "decide", approval_id, "--approve")

    threads = [threading.Thread(target=customer), threading.Thread(target=reviewer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert results["cli"].exit_code == 0, results["cli"].output
    assert len(cancellations()) == 1  # serialized by the per-thread lock: written once
    history = httpx.get(f"{live_api}/threads/{tid}").json()["messages"]
    texts = [m["content"] for m in history]
    assert sum(t.startswith("Your cancellation was approved.") for t in texts) == 1
    assert sum("sent to a support specialist" in t for t in texts) == 1
    body = results["message"]
    if body["status"] == "awaiting_approval":
        assert "still pending" in body["messages"][0]


def test_cli_admin_commands(live_api: str) -> None:
    from app.approvals.service import approvals_service

    _, approval_id = submit_cancellation(live_api)
    approvals_service.decide(approval_id, "approve", "marcus", None)  # decided, not resumed
    reconciled = run_cli("approvals", "reconcile")
    assert reconciled.exit_code == 0, reconciled.output
    assert "resumed=1 expired=0 repaired_interrupts=0" in reconciled.output
    assert len(cancellations()) == 1
    unlocked = run_cli("policy", "unlock", "POL-100001")
    assert unlocked.exit_code == 0, unlocked.output
    assert "POL-100001: removed 0 failure(s)" in unlocked.output
    purged = run_cli("purge", "--older-than-days", "0")
    assert purged.exit_code == 0, purged.output
    assert "purged=1 skipped=0" in purged.output
