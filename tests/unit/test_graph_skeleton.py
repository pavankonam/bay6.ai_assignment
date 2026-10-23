"""M2: state schema, node contract, allowlist, tool-call synthesis/validation, builder, CLI draw."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.graph.builder import build_graph, draw_mermaid
from app.graph.checkpointer import close_checkpointer, open_checkpointer
from app.graph.nodes import NODES
from app.graph.nodes.base import StateSchemaError, graph_node
from app.graph.nodes.ingest import ingest
from app.graph.nodes.plan_tool import plan_tool, synthesize_tool_call
from app.graph.nodes.validate_tool_call import validate_tool_call
from app.graph.phases import tool_args, tool_phase
from app.graph.state import STATE_KEYS, TERMINAL_OUTCOMES, Outcome
from app.tools.allowlist import allowed_tool_names, bindable_tools, default_tool_name
from app.tools.definitions import WRITE_TOOL_NAMES
from tests.fixtures.fake_llm import FakeScript

PRD_STATE_KEYS = {
    "messages",
    "turn_count",
    "status",
    "active_intent",
    "intent_confidence",
    "intent_queue",
    "suspended_tasks",
    "pending_question",
    "phase",
    "slots",
    "slots_skipped",
    "missing_slots",
    "invalid_slot_feedback",
    "clarification_attempts",
    "confirmation_reask_count",
    "verified",
    "verified_policy_id",
    "verification_attempts",
    "policy_snapshot",
    "quote",
    "claim_checks",
    "duplicate_override",
    "pending_action",
    "approval",
    "tool_result",
    "error",
    "resume_step",
    "response_facts",
    "llm_calls",
    "last_outcome",
}
INTENTS = ("policy_status", "report_claim", "cancel_policy", None)
PHASES = ("collect", "verify", "lookup", "quote", "confirm", "approve", "execute", "done", None)


def test_state_schema_matches_prd_exactly() -> None:
    assert STATE_KEYS == PRD_STATE_KEYS


def test_terminal_outcomes_subset() -> None:
    import typing

    all_outcomes = set(typing.get_args(Outcome))
    assert all_outcomes - TERMINAL_OUTCOMES == {
        "OUT_OF_SCOPE_ANSWERED",
        "SMALLTALK",
        "SYSTEM_UNAVAILABLE",
    }


def test_node_writing_unknown_key_fails() -> None:
    @graph_node("bad")
    def bad(state: Any) -> dict[str, Any]:
        return {"slots": {}, "not_a_key": 1}

    with pytest.raises(StateSchemaError, match="not_a_key"):
        bad({})


def test_every_graph_node_is_schema_checked() -> None:
    assert all(getattr(fn, "__wrapped__", None) for fn in NODES.values())


# --- allowlist (Section 8.7, NFR4) -------------------------------------------------------------


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("phase", PHASES)
def test_write_tools_never_bindable(intent: str | None, phase: str | None) -> None:
    names = {t.name for t in bindable_tools(intent, phase)}
    assert not names & WRITE_TOOL_NAMES
    if intent is None:
        assert names == set()


@pytest.mark.parametrize(
    ("intent", "phase", "expected"),
    [
        ("policy_status", "verify", ("verify_policyholder",)),
        ("report_claim", "verify", ("verify_policyholder",)),
        ("cancel_policy", "verify", ("verify_policyholder",)),
        ("policy_status", "lookup", ("get_policy_status",)),
        ("report_claim", "lookup", ("get_policy_status",)),
        ("cancel_policy", "lookup", ("get_policy_status",)),
        ("cancel_policy", "quote", ("get_cancellation_quote",)),
        ("report_claim", "quote", ()),
        ("policy_status", "execute", ()),
        (None, "verify", ()),
    ],
)
def test_allowlist_table(intent: str | None, phase: str, expected: tuple[str, ...]) -> None:
    assert allowed_tool_names(intent, phase) == expected
    assert default_tool_name(intent, phase) == (expected[0] if expected else None)


# --- phases and tool args ----------------------------------------------------------------------


def test_tool_phase_and_args() -> None:
    slots = {"policy_id": "POL-100001", "dob": "1985-04-12", "effective_date": "2026-11-01"}
    state: dict[str, Any] = {"active_intent": "cancel_policy", "slots": slots}
    assert tool_phase(state) == "verify"
    assert tool_args("verify_policyholder", state) == {
        "policy_id": "POL-100001",
        "dob": "1985-04-12",
    }
    state.update(verified=True, verified_policy_id="POL-100001")
    assert tool_phase(state) == "lookup"
    state["policy_snapshot"] = {"policy_id": "POL-100001"}
    assert tool_phase(state) == "quote"
    assert tool_args("get_cancellation_quote", state) == {
        "policy_id": "POL-100001",
        "effective_date": "2026-11-01",
    }
    state["active_intent"] = "report_claim"
    assert tool_phase(state) is None
    with pytest.raises(ValueError, match="create_claim"):
        tool_args("create_claim", state)


# --- plan_tool / validate_tool_call ------------------------------------------------------------


def verify_state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="POL-100001 born 4/12/1985", id="h1")],
        "active_intent": "policy_status",
        "slots": {"policy_id": "POL-100001", "dob": "1985-04-12"},
        "verified": False,
        "llm_calls": 0,
    }
    state.update(extra)
    return state


def test_deterministic_synthesis_shape() -> None:
    message = synthesize_tool_call("verify_policyholder")
    assert message.content == ""
    assert len(message.tool_calls) == 1
    call = message.tool_calls[0]
    assert call["name"] == "verify_policyholder"
    assert call["args"] == {}
    assert re.fullmatch(r"call_[0-9a-f]{32}", str(call["id"]))
    assert "created_at" in message.additional_kwargs


def test_plan_tool_deterministic_mode_makes_no_llm_call(set_env: Any, fake_llm: FakeScript) -> None:
    set_env(TOOL_SELECTION_MODE="deterministic")
    update = plan_tool(verify_state())
    assert update["messages"][0].tool_calls[0]["name"] == "verify_policyholder"
    assert "llm_calls" not in update
    assert fake_llm.calls() == []


def test_plan_tool_llm_mode_binds_only_allowed_tools(set_env: Any, fake_llm: FakeScript) -> None:
    set_env(TOOL_SELECTION_MODE="llm")
    update = plan_tool(verify_state())
    assert update["llm_calls"] == 1
    (request,) = fake_llm.calls("plan_tool")
    assert request.tools == ["verify_policyholder"]
    assert "1985-04-12" not in request.payload_text
    assert "4/12/1985" not in request.payload_text
    assert update["messages"][0].tool_calls[0]["name"] == "verify_policyholder"


def test_plan_tool_llm_failure_falls_back_to_default(set_env: Any, fake_llm: FakeScript) -> None:
    set_env(TOOL_SELECTION_MODE="llm")
    fake_llm.push("plan_tool", TimeoutError("llm down"))
    update = plan_tool(verify_state())
    assert update["messages"][0].tool_calls[0]["name"] == "verify_policyholder"
    assert update["llm_calls"] == 1


def test_plan_tool_without_allowed_tool_raises() -> None:
    with pytest.raises(RuntimeError):
        plan_tool(verify_state(active_intent=None))


def test_validate_tool_call_rejects_and_replaces_with_same_id() -> None:
    proposal = AIMessage(
        content="",
        id="ai-1",
        tool_calls=[
            {"name": "cancel_policy", "args": {"policy_id": "POL-100002"}, "id": "c-evil"},
            {"name": "get_policy_status", "args": {"policy_id": "POL-100002"}, "id": "c-2"},
        ],
    )
    state = verify_state()
    state["messages"] = [*state["messages"], proposal]
    update = validate_tool_call(state)
    (replacement,) = update["messages"]
    assert replacement.id == "ai-1"
    assert len(replacement.tool_calls) == 1
    call = replacement.tool_calls[0]
    assert call["name"] == "verify_policyholder"
    assert call["id"] != "c-evil"
    assert call["args"] == {"policy_id": "POL-100001", "dob": "1985-04-12"}


def test_validate_tool_call_keeps_allowed_call_id_and_overwrites_args() -> None:
    proposal = AIMessage(
        content="",
        id="ai-2",
        tool_calls=[
            {
                "name": "verify_policyholder",
                "args": {"policy_id": "POL-100002", "dob": "1990-09-30"},
                "id": "c-ok",
            }
        ],
    )
    state = verify_state()
    state["messages"] = [*state["messages"], proposal]
    call = validate_tool_call(state)["messages"][0].tool_calls[0]
    assert call["id"] == "c-ok"
    assert call["args"] == {"policy_id": "POL-100001", "dob": "1985-04-12"}


def test_validate_tool_call_requires_a_proposal() -> None:
    with pytest.raises(RuntimeError):
        validate_tool_call(verify_state())


# --- ingest ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, "in_progress"),
        ("completed", "in_progress"),
        ("escalated", "in_progress"),
        ("awaiting_info", "in_progress"),
        ("awaiting_confirmation", "awaiting_confirmation"),
        ("awaiting_approval", "awaiting_approval"),
        ("failed_retryable", "failed_retryable"),
        ("locked", "locked"),
    ],
)
def test_ingest_status_normalization(before: str | None, after: str) -> None:
    state = {"messages": [HumanMessage(content="hi", id="h1")], "status": before}
    assert ingest(state)["status"] == after


def test_ingest_resets_per_turn_fields_but_not_error() -> None:
    state = {
        "messages": [HumanMessage(content="hi", id="h1", additional_kwargs={"created_at": "x"})],
        "turn_count": 4,
        "tool_result": {"tool": "x"},
        "response_facts": {"a": 1},
        "last_outcome": "SMALLTALK",
        "error": {"code": "TIMEOUT"},
        "resume_step": "read_tool",
        "slots": {"policy_id": "POL-100001"},
    }
    update = ingest(state)
    assert update["turn_count"] == 5
    assert update["tool_result"] is None
    assert update["response_facts"] == {}
    assert update["last_outcome"] is None
    assert "error" not in update
    assert "resume_step" not in update
    assert "slots" not in update  # existing values are never overwritten by defaults
    assert "messages" not in update  # created_at already present


def test_ingest_initializes_defaults_and_stamps_created_at() -> None:
    update = ingest({"messages": [HumanMessage(content="hi", id="h1")]})
    assert update["turn_count"] == 1
    assert update["verified"] is False
    assert update["slots"] == {}
    assert update["messages"][0].id == "h1"
    assert "created_at" in update["messages"][0].additional_kwargs


# --- builder / checkpointer / draw -------------------------------------------------------------

EXPECTED_NODES = set(NODES) | {"tools"}


def test_graph_compiles_with_every_node() -> None:
    graph = build_graph()
    drawn = graph.get_graph()
    assert set(drawn.nodes) >= EXPECTED_NODES


def test_graph_runs_on_sqlite_checkpointer_and_persists(tmp_path: Path) -> None:
    saver = open_checkpointer(tmp_path / "cp.db")
    graph = build_graph(saver)
    config: Any = {"configurable": {"thread_id": "t-1"}}
    graph.invoke({"messages": [HumanMessage(content="hi")]}, config)
    close_checkpointer(saver)
    reopened = open_checkpointer(tmp_path / "cp.db")
    state = build_graph(reopened).get_state(config)
    assert state.values["turn_count"] == 1
    close_checkpointer(reopened)


def test_read_tools_execute_inside_graph_path(seeded: None) -> None:
    """plan_tool → validate_tool_call → tools runs a real read tool through ToolNode."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode

    from app.graph.state import SupportState
    from app.tools.definitions import READ_TOOLS

    graph: Any = StateGraph(SupportState)
    graph.add_node("plan_tool", plan_tool)
    graph.add_node("validate_tool_call", validate_tool_call)
    graph.add_node("tools", ToolNode(list(READ_TOOLS)))
    graph.add_edge(START, "plan_tool")
    graph.add_edge("plan_tool", "validate_tool_call")
    graph.add_edge("validate_tool_call", "tools")
    graph.add_edge("tools", END)
    out = graph.compile().invoke(verify_state())
    last = out["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.artifact == {"ok": True, "data": {"verified": True}, "error": None}


def test_draw_mermaid_contains_routes() -> None:
    mermaid = draw_mermaid()
    for node in EXPECTED_NODES:
        assert node in mermaid
    assert "-.->" in mermaid  # conditional edges are drawn


def test_cli_graph_draw_writes_markdown(tmp_path: Path) -> None:
    out = tmp_path / "graph.md"
    result = CliRunner().invoke(cli_app, ["graph", "draw", "--output", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert text.startswith("# Support graph")
    assert "```mermaid" in text
    assert "human_approval" in text
