"""Approval workflow and API side tables in `app.db` (Section 8.4.2)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"

    approval_id: str = Field(primary_key=True)
    idempotency_key: str = Field(unique=True, nullable=False)
    pending_action_id: str
    thread_id: str = Field(index=True)
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    customer: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    risk_flags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    summary: str = ""
    status: str = Field(default="pending", index=True)
    reviewer_id: str | None = None
    comment: str | None = None
    created_at: str
    decided_at: str | None = None
    resume_status: str = "none"


class VerificationFailure(SQLModel, table=True):
    __tablename__ = "verification_failures"

    id: int | None = Field(default=None, primary_key=True)
    policy_id: str = Field(index=True)
    thread_id: str
    failed_at: str


class ProcessedMessage(SQLModel, table=True):
    __tablename__ = "processed_messages"
    __table_args__ = (UniqueConstraint("thread_id", "client_message_id"),)

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(index=True)
    client_message_id: str
    response: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: str


class ThreadSideMessage(SQLModel, table=True):
    __tablename__ = "thread_side_messages"

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(index=True)
    role: str
    content: str
    created_at: str
