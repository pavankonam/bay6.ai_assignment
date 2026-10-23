"""Scripted demo: all three requests + the human-approval flow, with no API key and no network.

Starts its own API (fake LLM, temporary databases, APP_TODAY=2026-10-22) on a free localhost
port, then plays the customer over HTTP and the reviewer through the real `support` CLI.

    make demo        # or: .venv/bin/python scripts/demo.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(tempfile.mkdtemp(prefix="support-demo-"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PORT = _free_port()
API = f"http://127.0.0.1:{PORT}"
ENV = {
    "SUPPORT_NO_DOTENV": "1",
    "LLM_PROVIDER": "fake",
    "TOOL_SELECTION_MODE": os.environ.get("TOOL_SELECTION_MODE", "llm"),
    "APP_TODAY": "2026-10-22",
    "APP_DB_PATH": str(DATA_DIR / "app.db"),
    "CHECKPOINT_DB_PATH": str(DATA_DIR / "checkpoints.db"),
    "SUPPORT_API_URL": API,
    "REVIEWER_TOKEN": "demo-reviewer-token",
    "ADMIN_TOKEN": "demo-admin-token",
    "LOG_LEVEL": "WARNING",
}
os.environ.update(ENV)
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import create_app  # noqa: E402

BOLD, DIM, CYAN, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)
if not sys.stdout.isatty():
    BOLD = DIM = CYAN = GREEN = YELLOW = RESET = ""


def heading(title: str) -> None:
    print(f"\n{BOLD}{'=' * 78}\n{title}\n{'=' * 78}{RESET}")


def show(who: str, text: str, color: str) -> None:
    lines = []
    for paragraph in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, 70) or [""])
    print(f"{color}{who:>9}:{RESET} {lines[0]}")
    for line in lines[1:]:
        print(f"{'':>11}{line}")


class Customer:
    def __init__(self) -> None:
        self.thread_id = httpx.post(f"{API}/threads").json()["thread_id"]
        self.seen = 0

    def says(self, text: str) -> dict[str, object]:
        show("Customer", text, GREEN)
        body = httpx.post(
            f"{API}/threads/{self.thread_id}/messages", json={"content": text}, timeout=60
        ).json()
        for message in body["messages"]:
            show("Agent", message, CYAN)
        self.seen = len(self.history())
        return dict(body)

    def history(self) -> list[dict[str, str]]:
        return list(httpx.get(f"{API}/threads/{self.thread_id}").json()["messages"])

    def refresh(self) -> None:
        print(f"{DIM}  (customer refreshes the conversation: GET /threads/{{id}}){RESET}")
        history = self.history()
        for message in history[self.seen :]:
            if message["role"] == "assistant":
                show("Agent", message["content"], CYAN)
        self.seen = len(history)


def reviewer(*args: str) -> str:
    command = [sys.executable, "-m", "app.cli", *args]
    print(f"{YELLOW}{'Reviewer':>9}:{RESET} $ support {' '.join(args)}")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, **ENV, "COLUMNS": "100"},
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.rstrip().splitlines():
        print(f"{'':>11}{DIM}{line}{RESET}")
    return result.stdout


def pending_approval_id() -> str:
    approvals = httpx.get(
        f"{API}/approvals",
        params={"status": "pending"},
        headers={"X-Reviewer-Token": ENV["REVIEWER_TOKEN"]},
    ).json()["approvals"]
    return str(approvals[-1]["approval_id"])


def main() -> None:
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    print(f"{DIM}API running at {API} (fake LLM, APP_TODAY=2026-10-22, data in {DATA_DIR}){RESET}")
    try:
        heading("1. Check policy status (read-only, no approval)")
        jane = Customer()
        jane.says("Hi, can you check if my policy is still active?")
        jane.says("POL-100001, born April 12 1985")

        heading("2. Report a vehicle claim (multi-turn, low severity, no approval)")
        jane.says("I got rear-ended yesterday.")
        jane.says("Main St & 5th Ave, Springfield")
        jane.says("No injuries, yes another car")
        jane.says("about 2500, no report")
        jane.says("yes")

        heading("3. Request a policy cancellation (always human-approved)")
        jane.says("Now I'd like to cancel the policy.")
        jane.says("Nov 1, switching provider")
        jane.says("yes")
        jane.says("any update?")
        reviewer("approvals", "list")
        approval_id = pending_approval_id()
        reviewer(
            "approvals",
            "decide",
            approval_id,
            "--approve",
            "--comment",
            "Verified, processed.",
            "--reviewer-id",
            "marcus",
        )
        jane.refresh()

        heading("4. A claim that needs review, rejected by the reviewer")
        wei = Customer()
        wei.says("Someone hit my car in a parking lot yesterday and I hurt my neck.")
        wei.says("POL-100004, DOB 11/3/1995")
        wei.says("Oak Rd, Springfield")  # injuries + other party came from the first message
        wei.says("skip")  # no estimate → unknown severity → needs review (FR32)
        wei.says("yes")
        reviewer(
            "approvals",
            "decide",
            pending_approval_id(),
            "--reject",
            "--comment",
            "Please call us so we can take a recorded statement.",
        )
        wei.refresh()

        heading("Done")
        print("Try it yourself: `make run` in one terminal, `.venv/bin/support chat` in another.")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
