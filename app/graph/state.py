"""Graph state (Section 8.4.1). This schema is authoritative: nodes may write only these keys."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict, get_args

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Intent = Literal["policy_status", "report_claim", "cancel_policy"]
Status = Literal[
    "in_progress",
    "awaiting_info",
    "awaiting_confirmation",
    "awaiting_approval",
    "completed",
    "failed_retryable",
    "escalated",
    "locked",
]
Phase = Literal["collect", "verify", "lookup", "quote", "confirm", "approve", "execute", "done"]

Outcome = Literal[
    # terminal
    "STATUS_PROVIDED",
    "CLAIM_CREATED",
    "CLAIM_INELIGIBLE",
    "CLAIM_NOT_FILED_DUPLICATE",
    "CLAIM_REJECTED",
    "CANCEL_COMPLETED",
    "CANCEL_REJECTED",
    "NOT_ELIGIBLE_CANCEL",
    "WRITE_NOT_ELIGIBLE",
    "APPROVAL_EXPIRED",
    "DECLINED_BY_CUSTOMER",
    "WRITE_WITHDRAWN",
    "WRITE_OUTCOME_UNKNOWN",
    "ESCALATED",
    # non-terminal
    "OUT_OF_SCOPE_ANSWERED",
    "SMALLTALK",
    "SYSTEM_UNAVAILABLE",
]
TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {
        "STATUS_PROVIDED",
        "CLAIM_CREATED",
        "CLAIM_INELIGIBLE",
        "CLAIM_NOT_FILED_DUPLICATE",
        "CLAIM_REJECTED",
        "CANCEL_COMPLETED",
        "CANCEL_REJECTED",
        "NOT_ELIGIBLE_CANCEL",
        "WRITE_NOT_ELIGIBLE",
        "APPROVAL_EXPIRED",
        "DECLINED_BY_CUSTOMER",
        "WRITE_WITHDRAWN",
        "WRITE_OUTCOME_UNKNOWN",
        "ESCALATED",
    }
)
INTENTS: tuple[str, ...] = get_args(Intent)


class ErrorInfo(TypedDict):
    code: str  # e.g. TIMEOUT, NOT_FOUND, CONFLICT, LLM_UNAVAILABLE
    message: str  # internal message (never shown to user)
    retryable: bool
    internal: bool  # True → FR47b escalation
    node: str
    attempts: int


class PendingAction(TypedDict):
    pending_action_id: str  # UUID4, new on every prepare_action
    action_type: Literal["create_claim", "cancel_policy"]
    payload: dict[str, Any]  # canonical business fields only (FR27)
    idempotency_key: str
    requires_approval: bool
    approval_reasons: list[str]  # e.g. ["injuries_reported", "damage_estimate_unknown"]
    allow_duplicate: bool
    customer_confirmed: bool
    approval_id: str | None


class ApprovalInfo(TypedDict):
    approval_id: str
    status: Literal["pending", "approved", "rejected", "expired"]
    reviewer_id: str | None
    comment: str | None
    decided_at: str | None


class SuspendedTask(TypedDict):
    intent: Intent
    slots: dict[str, Any]  # task-specific slots only
    policy_id: str  # policy the task was started on; re-verify if it differs on resume


class QueuedTask(TypedDict):
    intent: Intent
    slots: dict[str, Any]  # candidates attributed to this intent by UnderstandResult.task_slots


class PendingQuestion(TypedDict):
    kind: Literal["next_task", "file_duplicate_claim", "restart_expired", "retry_write"]
    context: dict[str, Any]  # e.g. {"source": "queue", "intent": "cancel_policy", "slots": {...}}
    # or {"reask_count": 0, "outcome_unknown": False}


class ClaimChecks(TypedDict):
    incident_date: str
    eligible: bool
    reason: str | None  # e.g. NOT_IN_FORCE, TOO_OLD, FUTURE_DATE
    duplicate_claim_id: str | None


class SupportState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]  # each carries additional_kwargs.created_at
    turn_count: int
    status: Status
    active_intent: Intent | None
    intent_confidence: float
    intent_queue: list[QueuedTask]
    suspended_tasks: list[SuspendedTask]
    pending_question: PendingQuestion | None
    phase: Phase
    slots: dict[str, Any]
    slots_skipped: list[str]
    missing_slots: list[str]
    invalid_slot_feedback: dict[str, str]
    clarification_attempts: dict[str, int]
    confirmation_reask_count: int
    verified: bool
    verified_policy_id: str | None
    verification_attempts: int
    policy_snapshot: dict[str, Any] | None
    quote: dict[str, Any] | None
    claim_checks: ClaimChecks | None
    duplicate_override: bool
    pending_action: PendingAction | None
    approval: ApprovalInfo | None
    tool_result: dict[str, Any] | None
    error: ErrorInfo | None
    resume_step: Literal["read_tool", "execute_write"] | None
    response_facts: dict[str, Any]
    llm_calls: int
    last_outcome: Outcome | None


STATE_KEYS: frozenset[str] = frozenset(SupportState.__annotations__)
