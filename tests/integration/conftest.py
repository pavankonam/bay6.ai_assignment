"""Integration harness: drive the compiled graph turn by turn with the fake LLM."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.graph.builder import CompiledGraph, build_graph
from app.graph.checkpointer import close_checkpointer, open_checkpointer
from app.graph.messages import human, text_of
from app.llm_fake import FAKE_SCRIPT


class Conversation:
    """One thread. `say()` returns the new customer-visible agent replies for that turn."""

    def __init__(self, graph: CompiledGraph, thread_id: str | None = None) -> None:
        self.graph = graph
        self.thread_id = thread_id or str(uuid.uuid4())
        self.config: Any = {"configurable": {"thread_id": self.thread_id}, "recursion_limit": 50}
        self.turn_llm_calls: list[int] = []

    @property
    def state(self) -> dict[str, Any]:
        return dict(self.graph.get_state(self.config).values)

    @property
    def messages(self) -> list[BaseMessage]:
        return list(self.state.get("messages") or [])

    def say(self, text: str) -> list[str]:
        before = len(self.messages)
        calls_before = len(FAKE_SCRIPT.requests)
        self.graph.invoke({"messages": [human(text)]}, self.config)
        self.turn_llm_calls.append(len(FAKE_SCRIPT.requests) - calls_before)
        return self.replies_since(before)

    def replies_since(self, index: int) -> list[str]:
        return [
            text_of(m)
            for m in self.messages[index:]
            if isinstance(m, AIMessage) and not m.tool_calls
        ]

    def reply(self, text: str) -> str:
        return "\n".join(self.say(text))

    def tool_messages(self, name: str | None = None) -> list[ToolMessage]:
        return [
            m
            for m in self.messages
            if isinstance(m, ToolMessage) and (name is None or m.name == name)
        ]

    def tool_names(self) -> list[str]:
        return [str(m.name) for m in self.tool_messages()]

    def tool_args(self, name: str) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for message in self.messages:
            if isinstance(message, AIMessage):
                calls.extend(c["args"] for c in message.tool_calls if c["name"] == name)
        return calls

    def transcript(self) -> str:
        lines = []
        for m in self.messages:
            if isinstance(m, HumanMessage):
                lines.append(f"USER: {text_of(m)}")
            elif isinstance(m, AIMessage) and not m.tool_calls:
                lines.append(f"AGENT: {text_of(m)}")
            elif isinstance(m, ToolMessage):
                lines.append(f"  [{m.name}] {json.dumps(m.artifact, default=str)[:120]}")
        return "\n".join(lines)

    def asked(self) -> list[str]:
        from app.graph.messages import last_asked_slots

        return last_asked_slots(self.state)

    def answer_until_summary(self, answers: dict[str, str], max_turns: int = 8) -> str:
        """Answer whatever slots the agent asks for (by first asked slot) until a summary."""
        reply = ""
        for _ in range(max_turns):
            if self.state.get("status") != "awaiting_info":
                break
            asked = self.asked()
            key = next((a for a in asked if a in answers), None)
            if key is None:
                raise AssertionError(f"No scripted answer for {asked}:\n{self.transcript()}")
            reply = self.reply(answers[key])
        return reply

    def all_agent_text(self) -> str:
        return "\n".join(self.replies_since(0))


@pytest.fixture
def checkpoint_path(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints.db"


@pytest.fixture
def graph(seeded: None, checkpoint_path: Path) -> Iterator[CompiledGraph]:
    saver = open_checkpointer(checkpoint_path)
    yield build_graph(saver)
    close_checkpointer(saver)


@pytest.fixture
def convo(graph: CompiledGraph) -> Callable[[], Conversation]:
    def make(thread_id: str | None = None) -> Conversation:
        return Conversation(graph, thread_id)

    return make
