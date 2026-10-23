"""Graph wiring (Section 8.3). Isolated here so LangGraph API changes touch one file (R9)."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from app.graph import routing
from app.graph.nodes import NODES
from app.graph.state import SupportState
from app.tools.definitions import READ_TOOLS

CompiledGraph = CompiledStateGraph[Any, Any, Any, Any]


def _paths(*targets: str) -> dict[Hashable, str]:
    return {t: t for t in targets}


def build_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> CompiledGraph:
    """Build and compile the support graph. Defaults to an in-memory checkpointer."""
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(SupportState)
    for name, fn in NODES.items():
        # Nodes take `Mapping[str, Any]` views of `SupportState`; LangGraph wants the TypedDict.
        graph.add_node(name, cast(Any, fn))
    # Read tools only; write tools are executed by `execute_write_action` (FR25, NFR4).
    graph.add_node("tools", ToolNode(list(READ_TOOLS)))

    graph.add_edge(START, "ingest")
    graph.add_conditional_edges(
        "ingest",
        routing.route_entry,
        _paths(
            "escalate", "clarify", "handle_confirmation", "handle_pending_question", "understand"
        ),
    )
    graph.add_conditional_edges(
        "understand",
        routing.route_intent,
        _paths("escalate", "respond", "clarify", "validate_slots"),
    )
    graph.add_conditional_edges(
        "validate_slots",
        routing.route_slots,
        _paths(
            "escalate",
            "clarify",
            "ask_for_info",
            "plan_tool",
            "respond",
            "ask_yes_no",
            "prepare_action",
        ),
    )
    graph.add_edge("plan_tool", "validate_tool_call")
    graph.add_edge("validate_tool_call", "tools")
    graph.add_edge("tools", "process_tool_result")
    graph.add_conditional_edges(
        "process_tool_result",
        routing.route_after_tool,
        _paths(
            "handle_error",
            "ask_for_info",
            "escalate",
            "respond",
            "validate_slots",
            "prepare_action",
        ),
    )
    graph.add_edge("prepare_action", "ask_confirmation")
    graph.add_edge("ask_confirmation", END)
    graph.add_conditional_edges(
        "handle_confirmation",
        routing.route_confirm,
        _paths(
            "escalate",
            "announce_approval",
            "execute_write_action",
            "invalidate_on_edit",
            "ask_confirmation",
            "respond",
        ),
    )
    graph.add_edge("invalidate_on_edit", "validate_slots")
    graph.add_edge("announce_approval", "human_approval")
    graph.add_conditional_edges(
        "human_approval", routing.route_approval, _paths("execute_write_action", "respond")
    )
    graph.add_conditional_edges(
        "execute_write_action", routing.route_write, _paths("handle_error", "respond")
    )
    graph.add_conditional_edges(
        "handle_pending_question",
        routing.route_pending_question,
        _paths(
            "escalate",
            "validate_slots",
            "execute_write_action",
            "ask_yes_no",
            "respond",
            "understand",
        ),
    )
    graph.add_conditional_edges("handle_error", routing.route_error, _paths("escalate", "respond"))
    for terminal in ("ask_yes_no", "clarify", "ask_for_info", "escalate", "offer_next_task"):
        graph.add_edge(terminal, END)
    graph.add_edge("respond", "close_task")
    graph.add_conditional_edges(
        "close_task",
        routing.route_next_task,
        {"offer_next_task": "offer_next_task", END: END},
    )
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def draw_mermaid() -> str:
    """Mermaid source for the compiled graph (no DB access)."""
    return build_graph(InMemorySaver()).get_graph().draw_mermaid()
