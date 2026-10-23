"""Side messages (FR39) and the customer-visible history merge (FR53, FR53b)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage
from sqlalchemy import delete
from sqlmodel import col, select

from app.approvals.models import ProcessedMessage, ThreadSideMessage
from app.config import utcnow_iso
from app.db import session_scope
from app.graph.messages import is_customer_visible, text_of
from app.nlu.redaction import redact_dob


def add_side_message(thread_id: str, role: str, content: str) -> dict[str, Any]:
    with session_scope() as session:
        row = ThreadSideMessage(
            thread_id=thread_id, role=role, content=content, created_at=utcnow_iso()
        )
        session.add(row)
        session.flush()
        return {"id": row.id, "role": role, "content": content, "created_at": row.created_at}


def side_messages(thread_id: str) -> list[ThreadSideMessage]:
    with session_scope() as session:
        rows = session.exec(
            select(ThreadSideMessage)
            .where(ThreadSideMessage.thread_id == thread_id)
            .order_by(col(ThreadSideMessage.id))
        ).all()
        return list(rows)


def merged_history(thread_id: str, messages: Sequence[AnyMessage]) -> list[dict[str, Any]]:
    """Graph messages (customer-visible only) + side messages, ordered by `created_at` with
    insertion order (graph index, then side-message id) as the tiebreaker. DOBs are masked."""
    entries: list[tuple[str, int, int, dict[str, Any]]] = []
    for index, message in enumerate(messages):
        if not is_customer_visible(message):
            continue
        role = "user" if isinstance(message, HumanMessage) else "assistant"
        content = text_of(message)
        if role == "user":
            content = redact_dob(content)
        created = str(message.additional_kwargs.get("created_at") or "")
        entries.append(
            (
                created,
                0,
                index,
                {"role": role, "content": content, "created_at": created, "source": "graph"},
            )
        )
    for row in side_messages(thread_id):
        content = redact_dob(row.content) if row.role == "user" else row.content
        entries.append(
            (
                row.created_at,
                1,
                int(row.id or 0),
                {
                    "role": row.role,
                    "content": content,
                    "created_at": row.created_at,
                    "source": "side",
                },
            )
        )
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    return [e[3] for e in entries]


def stored_response(thread_id: str, client_message_id: str) -> dict[str, Any] | None:
    """FR53a: the stored response for a retried `(thread_id, client_message_id)`."""
    with session_scope() as session:
        row = session.exec(
            select(ProcessedMessage)
            .where(ProcessedMessage.thread_id == thread_id)
            .where(ProcessedMessage.client_message_id == client_message_id)
        ).first()
        return dict(row.response) if row is not None else None


def store_response(thread_id: str, client_message_id: str, result: Any) -> None:
    response = {
        "messages": list(result.messages),
        "status": result.status,
        "pending_approval_id": result.pending_approval_id,
        "side": result.side,
    }
    with session_scope() as session:
        session.add(
            ProcessedMessage(
                thread_id=thread_id,
                client_message_id=client_message_id,
                response=response,
                created_at=utcnow_iso(),
            )
        )


def delete_thread_rows(thread_id: str) -> None:
    """NFR10 purge: side messages and processed-message records for one thread."""
    with session_scope() as session:
        for model in (ThreadSideMessage, ProcessedMessage):
            session.exec(delete(model).where(col(model.thread_id) == thread_id))
