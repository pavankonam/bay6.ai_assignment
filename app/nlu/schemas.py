"""Structured-output schemas for LLM calls (Section 8.10)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

IntentLabel = Literal[
    "policy_status",
    "report_claim",
    "cancel_policy",
    "provide_info",
    "smalltalk",
    "out_of_scope",
    "human_handoff",
]


class DateMentionOut(BaseModel):
    """The LLM's role for one deterministic date span."""

    text: str = Field(description="Exact span from the latest message")
    role: Literal["dob", "incident_date", "effective_date", "unknown"]


class UnderstandResult(BaseModel):
    """Intent classification + slot extraction for one customer message."""

    intents: list[IntentLabel] = Field(description="Ordered; the first is the primary intent")
    confidence: float = Field(ge=0.0, le=1.0)
    identity_slots: dict[str, Any] = Field(default_factory=dict)
    task_slots: dict[str, dict[str, Any]] = Field(default_factory=dict)
    date_roles: list[DateMentionOut] = Field(default_factory=list)
    is_correction: bool = False


class ConfirmationResult(BaseModel):
    """Classification of a non-bare reply to a confirmation summary (FR29)."""

    decision: Literal["confirm_yes", "confirm_no", "edit", "other", "handoff"]
    slot_changes: dict[str, Any] = Field(default_factory=dict)


class YesNoResult(BaseModel):
    """Classification of a non-bare reply to a pending yes/no question (FR11a)."""

    answer: Literal["yes", "no", "unrelated", "handoff"]
