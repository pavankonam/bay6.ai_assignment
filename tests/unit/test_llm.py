"""M2: LLM gateway (prompt assembly, redaction, repair retry, fallback, caps) and the fake model."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from app import llm
from app.llm_fake import FakeScriptedChatModel
from app.nlu.redaction import redact_known_dob, redactor_for
from app.nlu.schemas import ConfirmationResult, YesNoResult
from app.observability.metrics import metrics
from app.tools.definitions import READ_TOOLS
from tests.fixtures.fake_llm import FakeScript


def history() -> list[Any]:
    return [HumanMessage(content="POL-100001 born April 12 1985")]


def test_prompts_loaded_with_kind_headers() -> None:
    assert set(llm.PROMPTS) == {"classify_extract", "plan_tool", "confirm_yes_no", "respond"}
    for name, text in llm.PROMPTS.items():
        assert llm.prompt_kind(text) == name
    assert llm.prompt_kind("no header") is None
    assert llm.prompt_kind("") is None


def test_build_messages_keeps_user_text_out_of_system_prompt() -> None:
    messages = llm.build_messages("respond", {"outcome": "SMALLTALK"}, history())
    assert isinstance(messages[0], SystemMessage)
    assert "POL-100001" not in str(messages[0].content)
    assert "1-800-555-0199" in str(messages[0].content)
    assert '"outcome": "SMALLTALK"' in str(messages[0].content)
    assert isinstance(messages[1], HumanMessage)


def test_build_history_filters_and_limits(set_env: Any) -> None:
    messages: list[Any] = [
        AIMessage(content="greeting"),
        HumanMessage(content="m1"),
        AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}]),
        ToolMessage(content="tool", tool_call_id="1"),
    ]
    messages += [
        HumanMessage(content=f"u{i}") if i % 2 else AIMessage(content=f"a{i}") for i in range(10)
    ]
    selected = llm.build_history(messages)
    assert len(selected) <= 6
    assert isinstance(selected[0], HumanMessage)
    assert all(not isinstance(m, ToolMessage) for m in selected)
    set_env(LLM_HISTORY_MAX_CHARS="5")
    long = [
        HumanMessage(content="x" * 20),
        AIMessage(content="y" * 3),
        HumanMessage(content="z" * 4),
    ]
    trimmed = llm.build_history(long)
    assert sum(len(str(m.content)) for m in trimmed) <= 5
    assert [str(m.content) for m in trimmed] == ["zzzz"]


def test_build_history_applies_redaction() -> None:
    state = {"slots": {"dob": "1985-04-12"}}
    selected = llm.build_history(history(), redactor_for(state))
    assert str(selected[0].content) == "POL-100001 born [DOB]"


@pytest.mark.parametrize(
    "text",
    [
        "born 1985-04-12",
        "dob 4/12/1985",
        "DOB 04/12/1985",
        "April 12, 1985",
        "apr 12 1985",
        "4/12/85",
        "12 April 1985",
        "04-12-1985",
    ],
)
def test_redact_known_dob_formats(text: str) -> None:
    from datetime import date

    assert "[DOB]" in redact_known_dob(text, date(1985, 4, 12))


def test_redact_without_known_dob_is_identity() -> None:
    assert redact_known_dob("born 1985-04-12", None) == "born 1985-04-12"
    assert redactor_for({})("x") == "x"


def test_structured_call_success_records_tokens(fake_llm: FakeScript) -> None:
    fake_llm.push("confirm_yes_no", YesNoResult(answer="yes"))
    outcome = llm.structured_call(
        YesNoResult,
        node="handle_pending_question",
        prompt="confirm_yes_no",
        context={},
        history=history(),
        llm_calls=0,
    )
    assert outcome.value == YesNoResult(answer="yes")
    assert outcome.calls == 1
    assert not outcome.failed
    assert metrics.get("llm_tokens_total", node="handle_pending_question", direction="input") > 0
    assert fake_llm.calls("confirm_yes_no")[0].tools == ["YesNoResult"]


def test_structured_call_repairs_once_then_succeeds(fake_llm: FakeScript) -> None:
    fake_llm.push("confirm_yes_no", {"answer": "maybe"}, {"answer": "no"})
    outcome = llm.structured_call(
        YesNoResult, node="n", prompt="confirm_yes_no", context={}, history=history(), llm_calls=0
    )
    assert outcome.value == YesNoResult(answer="no")
    assert outcome.calls == 2
    second = fake_llm.calls("confirm_yes_no")[1]
    assert "previous output was invalid" in second.messages[0][1]


def test_structured_call_falls_back_after_failed_repair(fake_llm: FakeScript) -> None:
    fake_llm.push("confirm_yes_no", {"answer": "maybe"}, {"decision": "nope"})
    outcome = llm.structured_call(
        ConfirmationResult,
        node="n",
        prompt="confirm_yes_no",
        context={},
        history=history(),
        llm_calls=0,
    )
    assert outcome.failed
    assert outcome.calls == 2
    assert metrics.total("llm_fallbacks_total") == 1


def test_structured_call_exception_falls_back(fake_llm: FakeScript) -> None:
    fake_llm.push("confirm_yes_no", TimeoutError("slow"))
    outcome = llm.structured_call(
        YesNoResult, node="n", prompt="confirm_yes_no", context={}, history=history(), llm_calls=0
    )
    assert outcome.failed
    assert outcome.calls == 1


def test_call_cap_forces_fallback_without_calling(set_env: Any, fake_llm: FakeScript) -> None:
    set_env(MAX_LLM_CALLS_PER_THREAD="2")
    for outcome in (
        llm.structured_call(
            YesNoResult,
            node="n",
            prompt="confirm_yes_no",
            context={},
            history=history(),
            llm_calls=2,
        ),
        llm.text_call(node="n", prompt="respond", context={}, history=history(), llm_calls=2),
        llm.tool_call(
            READ_TOOLS, node="n", prompt="plan_tool", context={}, history=history(), llm_calls=5
        ),
    ):
        assert outcome.failed
        assert outcome.calls == 0
    assert fake_llm.calls() == []
    # Repair retry would exceed the cap: only one call is made.
    fake_llm.push("confirm_yes_no", {"answer": "maybe"})
    outcome = llm.structured_call(
        YesNoResult, node="n", prompt="confirm_yes_no", context={}, history=history(), llm_calls=1
    )
    assert outcome.failed
    assert outcome.calls == 1


def test_text_call(fake_llm: FakeScript) -> None:
    fake_llm.push("respond", "Your policy is active.", "  ", RuntimeError("boom"))
    ok = llm.text_call(node="respond", prompt="respond", context={}, history=history(), llm_calls=0)
    assert ok.value == "Your policy is active."
    empty = llm.text_call(
        node="respond", prompt="respond", context={}, history=history(), llm_calls=0
    )
    assert empty.failed
    error = llm.text_call(
        node="respond", prompt="respond", context={}, history=history(), llm_calls=0
    )
    assert error.failed


def test_tool_call_paths(fake_llm: FakeScript) -> None:
    ok = llm.tool_call(
        READ_TOOLS[:1],
        node="plan_tool",
        prompt="plan_tool",
        context={},
        history=history(),
        llm_calls=0,
    )
    assert ok.value is not None
    assert ok.value.tool_calls[0]["name"] == "verify_policyholder"
    fake_llm.push("plan_tool", AIMessage(content="I won't call a tool"), ValueError("x"))
    for _ in range(2):
        outcome = llm.tool_call(
            READ_TOOLS[:1],
            node="plan_tool",
            prompt="plan_tool",
            context={},
            history=history(),
            llm_calls=0,
        )
        assert outcome.failed
        assert outcome.calls == 1


def test_fake_requires_script_or_heuristic(fake_llm: FakeScript) -> None:
    model = FakeScriptedChatModel()
    with pytest.raises(RuntimeError, match="No fake response"):
        model.invoke([SystemMessage(content="<!-- prompt: nonexistent -->"), HumanMessage("x")])
    fake_llm.push("respond", 42)
    with pytest.raises(TypeError):
        model.invoke([SystemMessage(content=llm.PROMPTS["respond"]), HumanMessage("x")])
    assert fake_llm.pending() == {}


def test_fake_parses_context_and_user_text(fake_llm: FakeScript) -> None:
    fake_llm.push("respond", "ok")
    messages = llm.build_messages("respond", {"outcome": "SMALLTALK"}, history())
    FakeScriptedChatModel().invoke(messages)
    (request,) = fake_llm.calls("respond")
    assert request.context == {"outcome": "SMALLTALK"}
    assert request.user_text.startswith("POL-100001")


def test_get_chat_model_providers(set_env: Any) -> None:
    llm.reset_chat_model()
    assert isinstance(llm.get_chat_model(), FakeScriptedChatModel)
    assert llm.get_chat_model() is llm.get_chat_model()
    set_env(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test-not-real")
    llm.reset_chat_model()
    model = llm.get_chat_model()
    assert type(model).__name__ == "ChatAnthropic"
    assert getattr(model, "max_tokens", None) == 1024
    assert getattr(model, "temperature", None) == 0
    assert getattr(model, "max_retries", None) == 1
    llm.reset_chat_model()


def test_schemas_validate() -> None:
    with pytest.raises(ValidationError):
        YesNoResult(answer="perhaps")  # type: ignore[arg-type]
