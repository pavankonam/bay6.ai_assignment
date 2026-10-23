"""`respond`: the final message for this turn (FR50, FR51, FR52)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.messages import ASKED_SLOTS, NLU, annotation
from app.graph.nodes.base import graph_node
from app.graph.speech import say
from app.graph.state import TERMINAL_OUTCOMES
from app.llm import build_history, text_call
from app.nlu.redaction import redactor_for
from app.observability.logging import get_logger
from app.responses.guard import check_response
from app.responses.outcomes import (
    TEMPLATE_ONLY_OUTCOMES,
    last_question,
    last_question_meta,
    render_template,
)

log = get_logger(__name__)


def _outcome(state: Mapping[str, Any]) -> str | None:
    outcome = state.get("last_outcome")
    if outcome:
        return str(outcome)
    primary = annotation(state, NLU).get("primary")
    if primary == "smalltalk":
        return "SMALLTALK"
    if primary == "out_of_scope":
        return "OUT_OF_SCOPE_ANSWERED"
    return None


@graph_node("respond")
def respond(state: Mapping[str, Any]) -> dict[str, Any]:
    outcome = _outcome(state)
    nlu = annotation(state, NLU)
    facts = dict(state.get("response_facts") or {})
    facts.setdefault("outcome", outcome)
    if nlu.get("coverage_question"):
        facts["coverage_question"] = True
    template = render_template(outcome, state, facts)
    text = template
    update: dict[str, Any] = {"last_outcome": outcome, "response_facts": facts}
    use_llm = (
        outcome not in TEMPLATE_ONLY_OUTCOMES
        and outcome is not None
        and not facts.get("coverage_question")
        and not facts.get("template")
        and not nlu.get("fallback")
    )
    if use_llm:
        llm_calls = int(state.get("llm_calls", 0))
        result = text_call(
            node="respond",
            prompt="respond",
            context={
                "outcome": outcome,
                "facts": facts,
                "pending_question_text": (
                    last_question(state)
                    if outcome in ("SMALLTALK", "OUT_OF_SCOPE_ANSWERED")
                    else None
                ),
            },
            history=build_history(state.get("messages") or [], redactor_for(state)),
            llm_calls=llm_calls,
        )
        update["llm_calls"] = llm_calls + result.calls
        if result.value is not None:
            failed = check_response(
                result.value, {**facts, "pending_question": last_question(state)}
            )
            if failed is None:
                text = result.value
            else:
                log.warning("response_guard_fallback", check=failed, outcome=outcome)
    meta: dict[str, Any] = {ASKED_SLOTS: []}
    if outcome in ("SMALLTALK", "OUT_OF_SCOPE_ANSWERED"):
        question, asked = last_question_meta(state)
        if question:  # the pending question was repeated, so it is still the one being answered
            meta = {ASKED_SLOTS: asked, "question": question}
    update["messages"] = [say(state, text, **meta)]
    if outcome in TERMINAL_OUTCOMES:
        update["status"] = "completed"
    return update
