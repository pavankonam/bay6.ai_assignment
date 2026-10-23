"""The thread runtime: message handling and approval decisions (Section 8.8).

Framework-free so it can be tested directly; the FastAPI layer (`app/main.py`) is a thin wrapper.
Every public method that touches a thread holds that thread's lock for its whole duration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import structlog
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, StateSnapshot

from app.approvals.service import STATUS_TO_DECISION, ApprovalsService, ApprovalView
from app.approvals.service import approvals_service as default_approvals
from app.concurrency import ThreadLocks, thread_locks
from app.config import get_settings, utcnow
from app.graph.builder import CompiledGraph
from app.graph.messages import ai, human, text_of
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.responses import templates
from app.security import purge_failures_older_than
from app.threads import (
    add_side_message,
    delete_thread_rows,
    merged_history,
    store_response,
    stored_response,
)

log = get_logger(__name__)
MAX_MESSAGE_CHARS = 2000


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class MessageTooLongError(ValueError):
    pass


@dataclass
class MessageResult:
    messages: list[str]
    status: str
    pending_approval_id: str | None = None
    side: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "status": self.status,
            "pending_approval_id": self.pending_approval_id,
        }


@dataclass
class DecisionResult:
    approval_id: str
    status: str
    thread_status: str
    messages: list[str] = field(default_factory=list)


def _config(thread_id: str) -> RunnableConfig:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": get_settings().RECURSION_LIMIT,
    }


def _agent_texts(messages: list[AnyMessage]) -> list[str]:
    return [text_of(m) for m in messages if isinstance(m, AIMessage) and not m.tool_calls]


def _interrupt_approval_id(snapshot: StateSnapshot) -> str | None:
    for item in snapshot.interrupts:
        value = item.value
        if isinstance(value, dict) and value.get("approval_id"):
            return str(value["approval_id"])
    return None


class SupportRuntime:
    def __init__(
        self,
        graph: CompiledGraph,
        *,
        approvals: ApprovalsService | None = None,
        locks: ThreadLocks | None = None,
    ) -> None:
        self.graph = graph
        self.approvals = approvals or default_approvals
        self.locks = locks or thread_locks

    # --- helpers ---------------------------------------------------------------------------------

    def snapshot(self, thread_id: str) -> StateSnapshot:
        return self.graph.get_state(_config(thread_id))

    def _messages(self, thread_id: str) -> list[AnyMessage]:
        return list(self.snapshot(thread_id).values.get("messages") or [])

    def _status(self, thread_id: str) -> tuple[str, str | None]:
        values = self.snapshot(thread_id).values
        status = str(values.get("status") or "in_progress")
        pending = None
        if status == "awaiting_approval":
            pending = (values.get("pending_action") or {}).get("approval_id")
        return status, pending

    def _invoke(self, thread_id: str, payload: Any) -> None:
        """Invocation wrapper: NFR13 (recursion limit → escalate) before NFR14 (anything else →
        system-unavailable, status unchanged)."""
        config = _config(thread_id)
        status_before = self.snapshot(thread_id).values.get("status")
        # NFR15: every log line emitted during this run (nodes, tools) carries the thread id.
        bound = structlog.contextvars.bind_contextvars(thread_id=thread_id)
        try:
            self.graph.invoke(payload, config)
        except GraphRecursionError:
            log.error("recursion_limit", thread_id=thread_id)
            metrics.inc("escalations_total", reason="recursion_limit")
            self.graph.update_state(
                config,
                {
                    "messages": [ai(templates.escalation("recursion_limit"))],
                    "status": "escalated",
                    "last_outcome": "ESCALATED",
                    "pending_action": None,
                },
                as_node="escalate",
            )
        except Exception as exc:
            log.error("unhandled_exception", thread_id=thread_id, error_type=type(exc).__name__)
            self.graph.update_state(
                config,
                {
                    "messages": [ai(templates.system_unavailable())],
                    "last_outcome": "SYSTEM_UNAVAILABLE",
                    # NFR14: the turn's status is left as it was before this turn started.
                    **({"status": status_before} if status_before else {}),
                },
                as_node="clarify",
            )
        finally:
            structlog.contextvars.reset_contextvars(**bound)

    def ensure_interrupt(self, thread_id: str) -> None:
        """Repair a crash between `announce_approval` and `interrupt()` (Section 8.8)."""
        snapshot = self.snapshot(thread_id)
        if snapshot.next == ("human_approval",) and not snapshot.interrupts:
            log.warning("ensure_interrupt_repair", thread_id=thread_id)
            self._invoke(thread_id, None)

    def resume_thread(self, approval: ApprovalView) -> None:
        """Resume the interrupt with the recorded decision if it matches; then mark it done."""
        self.ensure_interrupt(approval.thread_id)
        snapshot = self.snapshot(approval.thread_id)
        if (
            snapshot.next == ("human_approval",)
            and _interrupt_approval_id(snapshot) == approval.approval_id
            and approval.status in STATUS_TO_DECISION
        ):
            decision = {
                "decision": STATUS_TO_DECISION[approval.status],
                "reviewer_id": approval.reviewer_id,
                "comment": approval.comment,
                "decided_at": approval.decided_at,
            }
            self._invoke(approval.thread_id, Command(resume=decision))
        self.approvals.mark_resumed(approval.approval_id)

    # --- customer messages -----------------------------------------------------------------------

    def create_thread(self) -> str:
        return str(uuid.uuid4())

    def post_message(
        self, thread_id: str, content: str, client_message_id: str | None = None
    ) -> MessageResult:
        """Section 8.8 customer message handling, with `client_message_id` idempotency (FR53a)."""
        if len(content) > MAX_MESSAGE_CHARS:
            raise MessageTooLongError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")
        if client_message_id:
            stored = stored_response(thread_id, client_message_id)
            if stored is not None:  # FR53a: a retried request never runs a second turn
                return MessageResult(**stored)
        with self.locks.hold(thread_id):
            if client_message_id:  # re-check under the lock (concurrent retries)
                stored = stored_response(thread_id, client_message_id)
                if stored is not None:
                    return MessageResult(**stored)
            result = self._handle_message(thread_id, content)
            if client_message_id:
                store_response(thread_id, client_message_id, result)
            return result

    def _handle_message(self, thread_id: str, content: str) -> MessageResult:
        """Steps 2–4 of Section 8.8; the caller holds the thread lock."""
        self.ensure_interrupt(thread_id)  # step 2a
        snapshot = self.snapshot(thread_id)
        approval_id = _interrupt_approval_id(snapshot)
        if snapshot.next == ("human_approval",) and approval_id:
            return self._message_while_interrupted(thread_id, content, approval_id)
        before = len(snapshot.values.get("messages") or [])
        self._invoke(thread_id, {"messages": [human(content)]})
        new = _agent_texts(self._messages(thread_id)[before:])
        status, pending = self._status(thread_id)
        return MessageResult(new, status, pending)

    def _message_while_interrupted(
        self, thread_id: str, content: str, approval_id: str
    ) -> MessageResult:
        approval = self.approvals.get(approval_id)
        if (
            approval is not None
            and approval.status == "pending"
            and self.approvals.is_past_ttl(approval)
        ):
            self.approvals.expire(approval_id)  # step 3a
            approval = self.approvals.get(approval_id)
        if (
            approval is not None
            and approval.status != "pending"
            and (approval.resume_status == "pending")
        ):
            add_side_message(thread_id, "user", content)  # consumed by the resume (3a/3b)
            before = len(self._messages(thread_id))
            self.resume_thread(approval)
            new = _agent_texts(self._messages(thread_id)[before:])
            status, pending = self._status(thread_id)
            return MessageResult(new, status, pending)
        # 3c: still pending within the TTL → side reply only; the graph is not invoked.
        add_side_message(thread_id, "user", content)
        reply = templates.still_pending(approval_id)
        add_side_message(thread_id, "assistant", reply)
        return MessageResult([reply], "awaiting_approval", approval_id, side=True)

    def history(self, thread_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(thread_id)
        if not snapshot.values:
            raise NotFoundError(thread_id)
        status, pending = self._status(thread_id)
        return {
            "thread_id": thread_id,
            "status": status,
            "pending_approval_id": pending,
            "messages": merged_history(thread_id, snapshot.values.get("messages") or []),
        }

    # --- retention (NFR10) -----------------------------------------------------------------------

    def thread_activity(self) -> dict[str, str]:
        """Latest checkpoint timestamp per thread id."""
        latest: dict[str, str] = {}
        for item in self.graph.checkpointer.list(None):  # type: ignore[union-attr]
            thread_id = str(item.config["configurable"]["thread_id"])
            stamp = str(item.checkpoint["ts"])
            if stamp > latest.get(thread_id, ""):
                latest[thread_id] = stamp
        return latest

    def purge(self, older_than_days: int) -> dict[str, Any]:
        """Delete threads inactive for more than N days from both databases, skipping threads
        with a pending approval or interrupt. Mock business records are kept."""
        cutoff = (utcnow() - timedelta(days=older_than_days)).isoformat()
        purged: list[str] = []
        skipped: list[str] = []
        for thread_id, stamp in self.thread_activity().items():
            if stamp >= cutoff:
                continue
            with self.locks.hold(thread_id):
                snapshot = self.snapshot(thread_id)
                pending = [
                    a for a in self.approvals.for_threads([thread_id]) if a.status == "pending"
                ]
                if snapshot.interrupts or snapshot.next == ("human_approval",) or pending:
                    skipped.append(thread_id)
                    continue
                self.graph.checkpointer.delete_thread(thread_id)  # type: ignore[union-attr]
                self.approvals.delete_for_thread(thread_id)
                delete_thread_rows(thread_id)
                purged.append(thread_id)
        if purged:
            conn = getattr(self.graph.checkpointer, "conn", None)
            if conn is not None:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        failures = purge_failures_older_than(older_than_days)
        log.warning("purge_completed", purged=len(purged), skipped=len(skipped))
        return {"purged": purged, "skipped": skipped, "verification_failures_removed": failures}

    # --- reviewer decisions ----------------------------------------------------------------------

    def decide(
        self, approval_id: str, decision: str, reviewer_id: str, comment: str | None
    ) -> DecisionResult:
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(approval_id)
        thread_id = approval.thread_id
        with self.locks.hold(thread_id):
            self.ensure_interrupt(thread_id)
            approval = self.approvals.get(approval_id)
            assert approval is not None
            if approval.status == "pending" and self.approvals.is_past_ttl(approval):
                # Late decision: expire, resume with "expired" so the customer gets the notice.
                self.approvals.expire(approval_id)
                expired = self.approvals.get(approval_id)
                assert expired is not None
                self.resume_thread(expired)
                raise ConflictError("Approval expired before the decision")
            if approval.status != "pending":
                raise ConflictError(f"Approval is already {approval.status}")
            snapshot = self.snapshot(thread_id)
            if (
                snapshot.next != ("human_approval",)
                or _interrupt_approval_id(snapshot) != approval_id
            ):
                raise ConflictError("Thread is not waiting for this approval")
            if not self.approvals.decide(approval_id, decision, reviewer_id, comment):
                raise ConflictError("Approval was decided concurrently")
            decided = self.approvals.get(approval_id)
            assert decided is not None
            before = len(self._messages(thread_id))
            self.resume_thread(decided)
            status, _ = self._status(thread_id)
            return DecisionResult(
                approval_id,
                decided.status,
                status,
                _agent_texts(self._messages(thread_id)[before:]),
            )
