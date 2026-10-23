"""Model factory and the single LLM call wrapper (Section 8.10, NFR3a, NFR8a, FR48).

Every LLM call goes through `structured_call`, `tool_call` or `text_call`, which:
- build the prompt (system prompt from `app/prompts/*.md` + a JSON context block written by code;
  customer text only ever travels as user-role messages, NFR5),
- redact DOBs from every message (NFR8a),
- enforce `MAX_LLM_CALLS_PER_THREAD`,
- retry an invalid structured output once with a repair hint, then signal fallback (`value=None`),
- record token usage (`llm_tokens_total`) and fallbacks (`llm_fallbacks_total`).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.config import get_settings
from app.graph.messages import customer_visible, text_of
from app.observability.logging import get_logger
from app.observability.metrics import metrics

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)
V = TypeVar("V")

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_NAMES = ("classify_extract", "plan_tool", "confirm_yes_no", "respond")
HISTORY_MESSAGES = 6


def _load_prompts() -> dict[str, str]:
    return {name: (PROMPT_DIR / f"{name}.md").read_text() for name in PROMPT_NAMES}


PROMPTS: dict[str, str] = _load_prompts()  # loaded once at startup (NFR20)


def prompt_kind(system_text: str) -> str | None:
    """Recover the prompt name from its `<!-- prompt: name -->` header (used by the fake LLM)."""
    first = system_text.lstrip().splitlines()[0] if system_text.strip() else ""
    if first.startswith("<!-- prompt:") and first.endswith("-->"):
        return first.removeprefix("<!-- prompt:").removesuffix("-->").strip()
    return None


_model_lock = threading.Lock()
_model: BaseChatModel | None = None


def get_chat_model() -> BaseChatModel:
    global _model
    with _model_lock:
        if _model is None:
            _model = _build_model()
        return _model


def reset_chat_model() -> None:
    global _model
    with _model_lock:
        _model = None


def _build_model() -> BaseChatModel:
    settings = get_settings()
    if settings.LLM_PROVIDER == "fake":
        from app.llm_fake import FakeScriptedChatModel

        return FakeScriptedChatModel()
    from langchain_anthropic import ChatAnthropic

    api_key = settings.ANTHROPIC_API_KEY
    model: BaseChatModel = ChatAnthropic(
        model=settings.LLM_MODEL,
        temperature=0,
        max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=1,
        api_key=api_key.get_secret_value() if api_key else None,
    )
    return model


# --- prompt assembly ---------------------------------------------------------------------------

Redactor = Callable[[str], str]


def _identity(text: str) -> str:
    return text


def build_history(
    messages: Sequence[AnyMessage], redact: Redactor = _identity
) -> list[BaseMessage]:
    """The last 6 customer-visible messages (no ToolMessages, no tool-call AIMessages), redacted
    and limited to `LLM_HISTORY_MAX_CHARS` (oldest dropped first)."""
    budget = get_settings().LLM_HISTORY_MAX_CHARS
    selected: list[BaseMessage] = []
    used = 0
    for message in reversed(customer_visible(list(messages))[-HISTORY_MESSAGES:]):
        text = redact(text_of(message))
        if selected and used + len(text) > budget:
            break
        text = text[-budget:]
        used += len(text)
        role = HumanMessage if isinstance(message, HumanMessage) else AIMessage
        selected.append(role(content=text))
    selected.reverse()
    while selected and not isinstance(selected[0], HumanMessage):
        selected.pop(0)  # providers expect the conversation to start with the user
    return selected


def build_messages(
    prompt: str, context: Mapping[str, Any], history: Sequence[BaseMessage], repair_hint: str = ""
) -> list[BaseMessage]:
    system = PROMPTS[prompt].replace("{SUPPORT_PHONE}", get_settings().SUPPORT_PHONE)
    system += "\n\n<context>\n" + json.dumps(context, default=str, sort_keys=True) + "\n</context>"
    if repair_hint:
        system += f"\n\nYour previous output was invalid: {repair_hint}. Return valid output."
    return [SystemMessage(content=system), *history]


# --- call wrapper ------------------------------------------------------------------------------


@dataclass
class LLMOutcome(Generic[V]):
    value: V | None
    calls: int

    @property
    def failed(self) -> bool:
        return self.value is None


def _cap_reached(llm_calls: int) -> bool:
    return llm_calls >= get_settings().MAX_LLM_CALLS_PER_THREAD


def _record_usage(node: str, message: Any) -> None:
    usage = getattr(message, "usage_metadata", None) or {}
    for direction, key in (("input", "input_tokens"), ("output", "output_tokens")):
        tokens = usage.get(key) if isinstance(usage, Mapping) else None
        if tokens:
            metrics.inc("llm_tokens_total", float(tokens), node=node, direction=direction)
    if usage:
        log.info(
            "llm_usage",
            node=node,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )


def _fallback(node: str, reason: str) -> None:
    metrics.inc("llm_fallbacks_total")
    log.warning("llm_fallback", node=node, reason=reason)


def structured_call(
    schema: type[T],
    *,
    node: str,
    prompt: str,
    context: Mapping[str, Any],
    history: Sequence[BaseMessage],
    llm_calls: int,
) -> LLMOutcome[T]:
    """One structured call plus at most one repair retry. `value=None` → use the rule fallback."""
    if _cap_reached(llm_calls):
        _fallback(node, "call_cap")
        return LLMOutcome(None, 0)
    runnable = get_chat_model().with_structured_output(schema, include_raw=True)
    calls = 0
    hint = ""
    for _ in range(2):
        if _cap_reached(llm_calls + calls):
            break
        calls += 1
        try:
            out = runnable.invoke(build_messages(prompt, context, history, hint))
        except Exception as exc:
            _fallback(node, type(exc).__name__)
            return LLMOutcome(None, calls)
        assert isinstance(out, dict)
        _record_usage(node, out.get("raw"))
        parsed = out.get("parsed")
        if isinstance(parsed, schema):
            return LLMOutcome(parsed, calls)
        hint = type(out.get("parsing_error")).__name__ if out.get("parsing_error") else "no output"
    _fallback(node, "invalid_output")
    return LLMOutcome(None, calls)


def tool_call(
    tools: Sequence[BaseTool],
    *,
    node: str,
    prompt: str,
    context: Mapping[str, Any],
    history: Sequence[BaseMessage],
    llm_calls: int,
) -> LLMOutcome[AIMessage]:
    """Ask the model to propose a call to one of `tools` (the allowlisted read tools)."""
    if _cap_reached(llm_calls):
        _fallback(node, "call_cap")
        return LLMOutcome(None, 0)
    try:
        message = (
            get_chat_model()
            .bind_tools(list(tools), tool_choice="any")
            .invoke(build_messages(prompt, context, history))
        )
    except Exception as exc:
        _fallback(node, type(exc).__name__)
        return LLMOutcome(None, 1)
    _record_usage(node, message)
    if not isinstance(message, AIMessage) or not message.tool_calls:
        _fallback(node, "no_tool_call")
        return LLMOutcome(None, 1)
    return LLMOutcome(message, 1)


def text_call(
    *,
    node: str,
    prompt: str,
    context: Mapping[str, Any],
    history: Sequence[BaseMessage],
    llm_calls: int,
) -> LLMOutcome[str]:
    if _cap_reached(llm_calls):
        _fallback(node, "call_cap")
        return LLMOutcome(None, 0)
    try:
        message = get_chat_model().invoke(build_messages(prompt, context, history))
    except Exception as exc:
        _fallback(node, type(exc).__name__)
        return LLMOutcome(None, 1)
    _record_usage(node, message)
    text = text_of(message).strip()
    if not text:
        _fallback(node, "empty_text")
        return LLMOutcome(None, 1)
    return LLMOutcome(text, 1)
