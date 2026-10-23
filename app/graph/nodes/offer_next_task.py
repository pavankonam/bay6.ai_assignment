"""`offer_next_task`: offer a queued (FR10) or suspended (FR11) task and set `pending_question`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node
from app.graph.speech import say
from app.responses import templates


@graph_node("offer_next_task")
def offer_next_task(state: Mapping[str, Any]) -> dict[str, Any]:
    queue = list(state.get("intent_queue") or [])
    suspended = list(state.get("suspended_tasks") or [])
    if queue:
        intent, source = queue[0]["intent"], "queue"
    else:
        intent, source = suspended[-1]["intent"], "suspended"
    return {
        "messages": [say(state, templates.next_task_offer(intent, source))],
        "pending_question": {"kind": "next_task", "context": {"source": source, "intent": intent}},
        "status": "awaiting_info",
    }
