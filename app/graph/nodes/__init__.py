"""Graph nodes, one module per node (Section 8.2)."""

from __future__ import annotations

from app.graph.nodes.announce_approval import announce_approval
from app.graph.nodes.ask_confirmation import ask_confirmation
from app.graph.nodes.ask_for_info import ask_for_info
from app.graph.nodes.ask_yes_no import ask_yes_no
from app.graph.nodes.base import NodeFn
from app.graph.nodes.clarify import clarify
from app.graph.nodes.close_task import close_task
from app.graph.nodes.escalate import escalate
from app.graph.nodes.execute_write_action import execute_write_action
from app.graph.nodes.handle_confirmation import handle_confirmation
from app.graph.nodes.handle_error import handle_error
from app.graph.nodes.handle_pending_question import handle_pending_question
from app.graph.nodes.human_approval import human_approval
from app.graph.nodes.ingest import ingest
from app.graph.nodes.invalidate_on_edit import invalidate_on_edit
from app.graph.nodes.offer_next_task import offer_next_task
from app.graph.nodes.plan_tool import plan_tool
from app.graph.nodes.prepare_action import prepare_action
from app.graph.nodes.process_tool_result import process_tool_result
from app.graph.nodes.respond import respond
from app.graph.nodes.understand import understand
from app.graph.nodes.validate_slots import validate_slots
from app.graph.nodes.validate_tool_call import validate_tool_call

NODES: dict[str, NodeFn] = {
    "ingest": ingest,
    "understand": understand,
    "validate_slots": validate_slots,
    "ask_for_info": ask_for_info,
    "ask_yes_no": ask_yes_no,
    "clarify": clarify,
    "plan_tool": plan_tool,
    "validate_tool_call": validate_tool_call,
    "process_tool_result": process_tool_result,
    "prepare_action": prepare_action,
    "ask_confirmation": ask_confirmation,
    "handle_confirmation": handle_confirmation,
    "invalidate_on_edit": invalidate_on_edit,
    "announce_approval": announce_approval,
    "human_approval": human_approval,
    "execute_write_action": execute_write_action,
    "handle_pending_question": handle_pending_question,
    "handle_error": handle_error,
    "escalate": escalate,
    "respond": respond,
    "close_task": close_task,
    "offer_next_task": offer_next_task,
}
