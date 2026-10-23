"""`support` CLI (Typer). Every command except `graph draw` is a thin HTTP client of the running
API at `SUPPORT_API_URL` (FR54): no command opens `app.db`/`checkpoints.db` or runs the graph, so
every mutation goes through the API's per-thread locks."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

import httpx
import typer
from rich.console import Console

from app.config import PROJECT_ROOT, get_settings

app = typer.Typer(help="Acme Mutual support assistant CLI", no_args_is_help=True)
graph_app = typer.Typer(help="Graph utilities", no_args_is_help=True)
approvals_app = typer.Typer(help="Reviewer commands", no_args_is_help=True)
policy_app = typer.Typer(help="Operator commands for policies", no_args_is_help=True)
app.add_typer(graph_app, name="graph")
app.add_typer(approvals_app, name="approvals")
app.add_typer(policy_app, name="policy")

DEFAULT_GRAPH_DOC = PROJECT_ROOT / "docs" / "graph.md"
MAX_MESSAGE_CHARS = 2000
console = Console()


def _fail(message: str) -> NoReturn:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _request(method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
    settings = get_settings()
    headers: dict[str, str] = {}
    if token == "reviewer":
        headers["X-Reviewer-Token"] = settings.REVIEWER_TOKEN.get_secret_value()
    elif token == "admin":
        headers["X-Admin-Token"] = settings.ADMIN_TOKEN.get_secret_value()
    url = settings.SUPPORT_API_URL.rstrip("/") + path
    try:
        response = httpx.request(method, url, headers=headers, timeout=60.0, **kwargs)
    except httpx.TransportError:
        _fail(f"API not reachable at {settings.SUPPORT_API_URL}")
    if response.status_code >= 400:
        detail = (
            response.json().get("detail")
            if response.headers.get("content-type", "").startswith("application/json")
            else response.text
        )
        _fail(f"Error {response.status_code}: {detail}")
    return response.json()


# --- customer chat -------------------------------------------------------------------------------


def _print_agent(messages: list[str]) -> None:
    for message in messages:
        console.print(f"[bold cyan]Agent:[/bold cyan] {message}")


@app.command()
def chat(
    thread_id: Annotated[str | None, typer.Option(help="Resume an existing thread")] = None,
) -> None:
    """Interactive customer session. Commands: /refresh (check for reviewer updates), /quit."""
    tid = thread_id or _request("POST", "/threads")["thread_id"]
    console.print(f"[dim]Thread {tid} — type /refresh to check for updates, /quit to exit.[/dim]")
    seen = 0
    if thread_id:  # resuming: show the conversation so far, including any reviewer outcome
        history = _request("GET", f"/threads/{tid}")["messages"]
        for message in history:
            who = "[bold cyan]Agent:[/bold cyan]" if message["role"] == "assistant" else "You:"
            console.print(f"{who} {message['content']}")
        seen = len(history)
    while True:
        try:
            text = typer.prompt("You", prompt_suffix=": ")
        except (EOFError, typer.Abort):
            break
        if text.strip() == "/quit":
            break
        if text.strip() == "/refresh":
            history = _request("GET", f"/threads/{tid}")["messages"]
            _print_agent([m["content"] for m in history[seen:] if m["role"] == "assistant"])
            seen = len(history)
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            console.print(f"[yellow]Message truncated to {MAX_MESSAGE_CHARS} characters.[/yellow]")
            text = text[:MAX_MESSAGE_CHARS]
        result = _request("POST", f"/threads/{tid}/messages", json={"content": text})
        _print_agent(result["messages"])
        seen = len(_request("GET", f"/threads/{tid}")["messages"])
        if result["status"] == "awaiting_approval":
            console.print(
                "[dim](Waiting for a reviewer. Type /refresh to check for updates.)[/dim]"
            )


# --- reviewer ------------------------------------------------------------------------------------


@approvals_app.command("list")
def approvals_list(
    status: Annotated[
        str | None, typer.Option(help="pending|approved|rejected|expired")
    ] = "pending",
) -> None:
    """List approvals (default: pending)."""
    params = {"status": status} if status else {}
    approvals = _request("GET", "/approvals", token="reviewer", params=params)["approvals"]
    for item in approvals:
        customer = item.get("customer") or {}
        console.rule(f"[bold]{item['approval_id']}[/bold]  {item['status']}  {item['action_type']}")
        console.print(
            f"Customer: {customer.get('holder_name')} · {customer.get('policy_id')} · "
            f"DOB {customer.get('dob_masked')}"
        )
        console.print(f"Payload:  {item.get('payload')}")
        console.print(f"Risk:     {', '.join(item.get('risk_flags') or []) or 'none'}")
        console.print(f"Summary:  {item.get('summary')}")
        console.print(f"Created:  {item.get('created_at')}")
    if not approvals:
        console.print("[dim]No approvals.[/dim]")


@approvals_app.command("decide")
def approvals_decide(
    approval_id: str,
    approve: Annotated[bool, typer.Option("--approve", help="Approve the request")] = False,
    reject: Annotated[bool, typer.Option("--reject", help="Reject the request")] = False,
    comment: Annotated[str | None, typer.Option(help="Note passed on to the customer")] = None,
    reviewer_id: Annotated[str, typer.Option(help="Your reviewer id")] = "reviewer",
) -> None:
    """Approve or reject a pending request."""
    if approve == reject:
        _fail("Pass exactly one of --approve or --reject.")
    body = {
        "decision": "approve" if approve else "reject",
        "reviewer_id": reviewer_id,
        "comment": comment,
    }
    result = _request("POST", f"/approvals/{approval_id}/decision", token="reviewer", json=body)
    console.print(f"{result['approval_id']}: {result['status']} (thread {result['thread_status']})")


@approvals_app.command("reconcile")
def approvals_reconcile() -> None:
    """Resume decided/expired approvals and repair stuck interrupts (FR43a)."""
    result = _request("POST", "/admin/approvals/reconcile", token="admin")
    console.print(
        f"resumed={result['resumed']} expired={result['expired']} "
        f"repaired_interrupts={result['repaired_interrupts']}"
    )


@policy_app.command("unlock")
def policy_unlock(policy_id: str) -> None:
    """Lift the per-policy verification lockout after phone verification (FR19a)."""
    result = _request("POST", f"/admin/policies/{policy_id}/unlock", token="admin")
    console.print(f"{result['policy_id']}: removed {result['failures_removed']} failure(s)")


@app.command()
def purge(
    older_than_days: Annotated[int, typer.Option(help="Retention in days")] = 30,
) -> None:
    """Delete conversation data older than N days from both databases (NFR10)."""
    result = _request(
        "POST", "/admin/purge", token="admin", params={"older_than_days": older_than_days}
    )
    console.print(
        f"purged={len(result['purged'])} skipped={len(result['skipped'])} "
        f"verification_failures_removed={result['verification_failures_removed']}"
    )


# --- graph ---------------------------------------------------------------------------------------


@graph_app.command("draw")
def graph_draw(
    output: Annotated[Path, typer.Option(help="Markdown file to write")] = DEFAULT_GRAPH_DOC,
) -> None:
    """Build the graph with an in-memory checkpointer and write a Mermaid diagram."""
    from app.graph.builder import draw_mermaid

    mermaid = draw_mermaid()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "# Support graph\n\n"
        "Generated by `support graph draw` from the compiled LangGraph (do not edit by hand).\n\n"
        f"```mermaid\n{mermaid}```\n"
    )
    typer.echo(f"Wrote {output}")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
