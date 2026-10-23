"""`handle_pending_question`: answers to yes/no follow-ups (FR11a, FR11b, FR29b, FR47a)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.lifecycle import SUSPEND_RESET, identity_only, reset_task_state
from app.graph.messages import PENDING_ANSWER, annotate, latest_human_text
from app.graph.nodes.base import graph_node
from app.graph.nodes.execute_write_action import resolve_unknown_outcome, success_update
from app.graph.speech import say
from app.llm import build_history, structured_call
from app.nlu.redaction import redactor_for
from app.nlu.rules import bare_yes_no, is_handoff
from app.nlu.schemas import YesNoResult
from app.observability.logging import get_logger
from app.responses.outcomes import render_template

log = get_logger(__name__)


def classify_reply(state: Mapping[str, Any], kind: str) -> tuple[str, int]:
    """'yes' | 'no' | 'unrelated' | 'handoff' and the number of LLM calls used."""
    text = latest_human_text(state)
    if is_handoff(text):
        return "handoff", 0
    bare = bare_yes_no(text, retry=kind == "retry_write")
    if bare is not None:
        return bare, 0
    outcome = structured_call(
        YesNoResult,
        node="handle_pending_question",
        prompt="confirm_yes_no",
        context={"question_kind": kind},
        history=build_history(state.get("messages") or [], redactor_for(state)),
        llm_calls=int(state.get("llm_calls", 0)),
    )
    if outcome.value is None:
        return "unrelated", outcome.calls  # FR48: non-bare replies can't be read without the LLM
    return outcome.value.answer, outcome.calls


def restore_task(
    state: Mapping[str, Any], intent: str, stashed: Mapping[str, Any], policy_id: str | None
) -> dict[str, Any]:
    """Restore a suspended/queued/expired task: re-verify if the policy differs, always re-run the
    lookup and every check (no derived data is reused, FR11)."""
    slots = identity_only(state.get("slots"))
    verified_policy = state.get("verified_policy_id")
    if policy_id and policy_id != slots.get("policy_id"):
        slots["policy_id"] = policy_id
        slots.pop("dob", None)  # the DOB on file belongs to the other policy
    slots.update(stashed)
    same_policy = bool(policy_id) and policy_id == verified_policy and state.get("verified")
    return {
        **SUSPEND_RESET,
        "active_intent": intent,
        "slots": slots,
        "slots_skipped": [],
        "phase": "lookup" if same_policy or not policy_id else "verify",
        "approval": None,
        "pending_action": None,
    }


def _handle_next_task(
    state: Mapping[str, Any], answer: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    source = context.get("source")
    intent = str(context.get("intent"))
    queue = list(state.get("intent_queue") or [])
    suspended = list(state.get("suspended_tasks") or [])
    task: Mapping[str, Any] | None = None
    if source == "queue" and queue:
        task = queue.pop(0)
    elif suspended:
        matching = [i for i, t in enumerate(suspended) if t["intent"] == intent]
        task = suspended.pop(matching[-1] if matching else len(suspended) - 1)
    update: dict[str, Any] = {"intent_queue": queue, "suspended_tasks": suspended}
    if answer == "yes" and task is not None:
        policy_id = task.get("policy_id") or (state.get("slots") or {}).get("policy_id")
        update.update(restore_task(state, intent, task.get("slots") or {}, policy_id))
    elif answer == "no":
        update["response_facts"] = {"template": "okay_more_help"}
    return update


def _handle_duplicate(state: Mapping[str, Any], answer: str) -> dict[str, Any]:
    if answer == "yes":
        return {"duplicate_override": True}
    if answer == "no":
        return {"last_outcome": "CLAIM_NOT_FILED_DUPLICATE"}
    # Unrelated: the claim is dropped inline (not left active or suspended), then `understand`.
    cleared = {**state, "last_outcome": "CLAIM_NOT_FILED_DUPLICATE"}
    return {
        "last_outcome": "CLAIM_NOT_FILED_DUPLICATE",
        "messages": [say(state, "I won't file that claim.")],
        **reset_task_state(cleared),
    }


def withdrawal_check(state: Mapping[str, Any], *, handoff: bool = False) -> dict[str, Any]:
    """FR47a: before telling the customer anything, re-check the backend by idempotency key."""
    action = state.get("pending_action") or {}
    found, data = resolve_unknown_outcome(action)
    if found == "found":
        assert data is not None
        return success_update(action, data)  # it went through: report the real confirmation
    if found == "absent":
        return {
            "last_outcome": "WRITE_WITHDRAWN",
            "error": None,
            "resume_step": None,
            "response_facts": {"outcome": "WRITE_WITHDRAWN"},
        }
    log.error(
        "write_outcome_unknown",
        idempotency_key=action.get("idempotency_key"),
        handoff=handoff,
    )
    return {"last_outcome": "WRITE_OUTCOME_UNKNOWN", "error": None, "resume_step": None}


def _handle_retry_write(
    state: Mapping[str, Any], answer: str, context: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Returns the effective answer and the state update (FR11a `retry_write` row)."""
    if answer == "yes":
        return "yes", {}
    if answer == "unrelated" and int(context.get("reask_count", 0)) == 0:
        return "reask", {
            "pending_question": {
                "kind": "retry_write",
                "context": {**context, "reask_count": int(context.get("reask_count", 0)) + 1},
            }
        }
    return "no", withdrawal_check(state)


def _handle_restart(
    state: Mapping[str, Any], answer: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    if answer == "yes":
        return restore_task(
            state, str(context.get("intent")), context.get("slots") or {}, context.get("policy_id")
        )
    if answer == "no":
        return {"response_facts": {"template": "no_resubmit"}}
    return {}


@graph_node("handle_pending_question")
def handle_pending_question(state: Mapping[str, Any]) -> dict[str, Any]:
    pending = state.get("pending_question") or {}
    kind = str(pending.get("kind"))
    context = dict(pending.get("context") or {})
    answer, calls = classify_reply(state, kind)
    update: dict[str, Any] = {"pending_question": None}
    if calls:
        update["llm_calls"] = int(state.get("llm_calls", 0)) + calls
    if kind == "retry_write":
        if answer == "handoff":
            # FR29b: truthful status first, then escalate.
            status = withdrawal_check(state, handoff=True)
            update.update(status)
            outcome = status.get("last_outcome")
            if outcome != "WRITE_OUTCOME_UNKNOWN":
                text = render_template(outcome, state, status.get("response_facts") or {})
                update["messages"] = [say(state, text)]
        else:
            answer, retry_update = _handle_retry_write(state, answer, context)
            update.update(retry_update)
    elif answer != "handoff":
        if kind == "next_task":
            update.update(_handle_next_task(state, answer, context))
        elif kind == "file_duplicate_claim":
            update.update(_handle_duplicate(state, answer))
        elif kind == "restart_expired":
            update.update(_handle_restart(state, answer, context))
    annotated = annotate(state, **{PENDING_ANSWER: {"kind": kind, "answer": answer}})
    update["messages"] = annotated + list(update.get("messages") or [])
    return update
