"""`FakeScriptedChatModel`: a deterministic, network-free chat model (`LLM_PROVIDER=fake`).

It is a real `BaseChatModel`, so it goes through exactly the same `with_structured_output` /
`bind_tools` code paths as `ChatAnthropic`. Responses come from:

1. a script (`FAKE_SCRIPT.push(kind, response, ...)`, used by tests), else
2. a rule-based heuristic for that prompt kind (used by tests that do not script, and by the
   no-API-key demo).

Every request is recorded in `FAKE_SCRIPT.requests` (for call-count and redaction tests).
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from app.graph.messages import text_of
from app.llm import prompt_kind

_CONTEXT_RE = re.compile(r"<context>\n(.*)\n</context>", re.DOTALL)

Heuristic = Callable[["FakeRequest"], Any]


@dataclass
class FakeRequest:
    kind: str
    messages: list[tuple[str, str]]
    tools: list[str]
    context: dict[str, Any]

    @property
    def user_text(self) -> str:
        users = [text for role, text in self.messages if role == "human"]
        return users[-1] if users else ""

    @property
    def payload_text(self) -> str:
        """Everything sent to the model, for redaction assertions."""
        return "\n".join(text for _, text in self.messages)


@dataclass
class FakeScript:
    queues: dict[str, deque[Any]] = field(default_factory=lambda: defaultdict(deque))
    requests: list[FakeRequest] = field(default_factory=list)
    heuristics: dict[str, Heuristic] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, kind: str, *responses: Any) -> None:
        """Queue responses for a prompt kind. A response may be a pydantic model, a dict of
        structured args, a string (text reply), an `AIMessage`, or an exception to raise."""
        with self.lock:
            self.queues[kind].extend(responses)

    def pop(self, kind: str) -> tuple[bool, Any]:
        with self.lock:
            queue = self.queues.get(kind)
            if queue:
                return True, queue.popleft()
            return False, None

    def reset(self) -> None:
        with self.lock:
            self.queues.clear()
            self.requests.clear()

    def calls(self, kind: str | None = None) -> list[FakeRequest]:
        return [r for r in self.requests if kind is None or r.kind == kind]

    def pending(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.queues.items() if v}


FAKE_SCRIPT = FakeScript()


def register_heuristic(kind: str, fn: Heuristic) -> None:
    FAKE_SCRIPT.heuristics[kind] = fn


def _role(message: BaseMessage) -> str:
    return message.type


def _parse_context(system: str) -> dict[str, Any]:
    match = _CONTEXT_RE.search(system)
    if not match:
        return {}
    loaded = json.loads(match.group(1))
    return loaded if isinstance(loaded, dict) else {}


def _default_plan_tool(request: FakeRequest) -> AIMessage:
    name = request.tools[0] if request.tools else "verify_policyholder"
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": {}, "id": "call_" + uuid.uuid4().hex}]
    )


class FakeScriptedChatModel(BaseChatModel):
    """Scripted / heuristic chat model. Never touches the network."""

    @property
    def _llm_type(self) -> str:
        return "fake-scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        names = [str(convert_to_openai_tool(t)["function"]["name"]) for t in tools]
        return self.bind(tools=names, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system = next((text_of(m) for m in messages if isinstance(m, SystemMessage)), "")
        request = FakeRequest(
            kind=prompt_kind(system) or "unknown",
            messages=[(_role(m), text_of(m)) for m in messages],
            tools=list(kwargs.get("tools") or []),
            context=_parse_context(system),
        )
        with FAKE_SCRIPT.lock:
            FAKE_SCRIPT.requests.append(request)
        found, response = FAKE_SCRIPT.pop(request.kind)
        if not found:
            response = self._heuristic(request)
        if isinstance(response, BaseException):
            raise response
        message = self._to_message(response, request)
        input_chars = sum(len(text) for _, text in request.messages)
        message.usage_metadata = {
            "input_tokens": max(1, input_chars // 4),
            "output_tokens": max(1, len(str(message.content) + str(message.tool_calls)) // 4),
            "total_tokens": 0,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def _heuristic(request: FakeRequest) -> Any:
        if request.kind == "plan_tool":
            return _default_plan_tool(request)
        if not FAKE_SCRIPT.heuristics:
            import app.nlu.fake_heuristics  # noqa: F401  (registers heuristics)
        handler = FAKE_SCRIPT.heuristics.get(request.kind)
        if handler is None:
            raise RuntimeError(f"No fake response scripted for prompt kind {request.kind!r}")
        return handler(request)

    @staticmethod
    def _to_message(response: Any, request: FakeRequest) -> AIMessage:
        if isinstance(response, AIMessage):
            return response.model_copy()
        if isinstance(response, str):
            return AIMessage(content=response)
        if isinstance(response, BaseModel):
            args: dict[str, Any] = response.model_dump(mode="json")
        elif isinstance(response, dict):
            args = response
        else:
            raise TypeError(f"Unsupported fake response {type(response).__name__}")
        name = request.tools[0] if request.tools else "result"
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": "call_" + uuid.uuid4().hex}],
        )
