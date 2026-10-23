"""M1: tool wrappers, retry/timeout, fault injection, write-outcome lookup."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from sqlmodel import select

from app.db import session_scope
from app.mock_api.faults import get_fault_injector
from app.mock_api.repository import Cancellation, Claim
from app.observability.metrics import metrics
from app.tools import errors
from app.tools.definitions import (
    READ_TOOL_NAMES,
    READ_TOOLS,
    WRITE_TOOL_NAMES,
    CancelPolicyArgs,
    CreateClaimArgs,
    cancel_policy,
    cancel_policy_call,
    create_claim,
    create_claim_call,
    find_open_claims,
    get_cancellation_quote_call,
    get_policy_status,
    get_policy_status_call,
    lookup_write_result,
    verify_policyholder_call,
)
from app.tools.errors import ServiceUnavailableError, ToolError
from app.tools.retry import run_tool

pytestmark = pytest.mark.usefixtures("seeded")

SetEnv = Callable[..., None]


def claim(policy_id: str = "POL-100001", key: str = "k1", **extra: Any) -> CreateClaimArgs:
    values: dict[str, Any] = {
        "policy_id": policy_id,
        "incident_date": date(2026, 10, 21),
        "incident_location": "Main St & 5th Ave",
        "incident_description": "Rear-ended at a light",
        "injuries": False,
        "other_party_involved": True,
        "estimated_damage_usd": 2500.0,
        "idempotency_key": key,
    }
    values.update(extra)
    return CreateClaimArgs(**values)


def cancel(policy_id: str = "POL-100001", key: str = "c1") -> CancelPolicyArgs:
    return CancelPolicyArgs(
        policy_id=policy_id,
        effective_date=date(2026, 11, 1),
        reason="switching_provider",
        idempotency_key=key,
        approval_id="APR-AAAAA",
    )


def rows(model: type[Claim] | type[Cancellation], policy_id: str) -> int:
    with session_scope() as session:
        stmt = select(model).where(model.policy_id == policy_id)
        return len(session.exec(stmt).all())


# --- envelope and tool registration --------------------------------------------------------


def test_tool_sets_are_disjoint_and_have_schemas() -> None:
    assert {"verify_policyholder", "get_policy_status", "get_cancellation_quote"} == READ_TOOL_NAMES
    assert {"create_claim", "cancel_policy"} == WRITE_TOOL_NAMES
    for t in (*READ_TOOLS, create_claim, cancel_policy):
        assert t.args_schema is not None
        assert t.description


def test_verify_envelope() -> None:
    assert verify_policyholder_call("POL-100001", date(1985, 4, 12)) == {
        "ok": True,
        "data": {"verified": True},
        "error": None,
    }


def test_not_found_is_not_retried() -> None:
    result = get_policy_status_call("POL-000000")
    assert result["ok"] is False
    assert result["error"] is not None
    assert result["error"]["code"] == errors.NOT_FOUND
    assert result["error"]["retryable"] is False
    assert result["error"]["attempts"] == 1


def test_duplicate_claim_envelope_carries_existing_id() -> None:
    result = create_claim_call(claim(policy_id="POL-100006", incident_date=date(2026, 10, 15)))
    assert result["error"] is not None
    assert result["error"]["code"] == errors.DUPLICATE_CLAIM
    assert result["data"] == {"existing_claim_id": "CLM-2026-000001"}
    assert result["error"]["attempts"] == 1


def test_tools_run_in_toolnode_with_artifact_envelope() -> None:
    class S(TypedDict):
        messages: Annotated[list[AnyMessage], add_messages]

    graph = StateGraph(S)
    graph.add_node("tools", ToolNode(list(READ_TOOLS)))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    call = AIMessage(
        content="",
        tool_calls=[{"name": "get_policy_status", "args": {"policy_id": "POL-100001"}, "id": "c1"}],
    )
    out = app.invoke({"messages": [call]})["messages"][-1]
    assert isinstance(out, ToolMessage)
    assert out.artifact["ok"] is True
    assert json.loads(str(out.content))["data"]["status"] == "ACTIVE"
    assert "1985-04-12" not in str(out.content)
    # A write tool is not executable by the read ToolNode.
    bad = AIMessage(
        content="",
        tool_calls=[{"name": "cancel_policy", "args": {}, "id": "c2"}],
    )
    out = app.invoke({"messages": [bad]})["messages"][-1]
    assert isinstance(out, ToolMessage)
    assert out.status == "error"


def test_tool_invoke_returns_json_content() -> None:
    content = get_policy_status.invoke({"policy_id": "POL-100001"})
    assert json.loads(content)["ok"] is True
    written = create_claim.invoke(claim().model_dump())
    assert json.loads(written)["data"]["claim_id"].startswith("CLM-2026-")
    cancelled = cancel_policy.invoke(cancel(key="c-tool").model_dump())
    assert json.loads(cancelled)["data"]["cancellation_id"] == "CNL-2026-000001"


# --- retry wrapper -------------------------------------------------------------------------


def test_retry_wrapper_retries_retryable_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServiceUnavailableError()
        return {"x": 1}

    assert run_tool("get_policy_status", flaky) == errors.ok({"x": 1})
    assert calls["n"] == 3


def test_retry_wrapper_max_three_attempts() -> None:
    calls = {"n": 0}

    def down() -> dict[str, Any]:
        calls["n"] += 1
        raise ServiceUnavailableError()

    result = run_tool("get_policy_status", down)
    assert calls["n"] == 3
    assert result["error"] is not None
    assert result["error"]["attempts"] == 3
    assert result["error"]["retryable"] is True
    assert metrics.get("tool_calls_total", tool="get_policy_status", outcome="SERVICE_UNAVAILABLE")


def test_retry_wrapper_business_error_not_retried() -> None:
    calls = {"n": 0}

    def business() -> dict[str, Any]:
        calls["n"] += 1
        raise ToolError(errors.NOT_ELIGIBLE, "nope")

    result = run_tool("create_claim", business)
    assert calls["n"] == 1
    assert result["error"] is not None
    assert result["error"]["code"] == errors.NOT_ELIGIBLE


def test_retry_wrapper_converts_unexpected_exceptions() -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("secret stack detail")

    result = run_tool("get_policy_status", boom)
    assert result["error"] is not None
    assert result["error"]["code"] == errors.INTERNAL
    assert "secret" not in result["error"]["message"]


def test_retry_wrapper_enforces_timeout() -> None:
    def slow() -> dict[str, Any]:
        time.sleep(0.3)
        return {}

    started = time.perf_counter()
    result = run_tool("get_policy_status", slow, timeout=0.02, retries=1)
    assert result["error"] is not None
    assert result["error"]["code"] == errors.TIMEOUT
    assert result["error"]["attempts"] == 2
    assert time.perf_counter() - started < 0.25


def test_backoff_schedule(set_env: SetEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(TOOL_RETRY_BASE_SECONDS="0.5")
    sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)

    def down() -> dict[str, Any]:
        raise ServiceUnavailableError()

    run_tool("get_policy_status", down)
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 0.6
    assert 1.0 <= sleeps[1] <= 1.1


# --- fault injection -----------------------------------------------------------------------


def test_no_faults_when_disabled() -> None:
    assert get_policy_status_call("POL-999500")["ok"] is True
    assert create_claim_call(claim("POL-999501"))["ok"] is True


def test_magic_unavailable(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true")
    assert verify_policyholder_call("POL-999500", date(2000, 1, 1))["data"] == {"verified": True}
    for result in (
        get_policy_status_call("POL-999500"),
        get_cancellation_quote_call("POL-999500", date(2026, 11, 1)),
        create_claim_call(claim("POL-999500")),
        cancel_policy_call(cancel("POL-999500")),
    ):
        assert result["error"] is not None
        assert result["error"]["code"] == errors.SERVICE_UNAVAILABLE
        assert result["error"]["attempts"] == 3
    assert rows(Claim, "POL-999500") == 0


def test_magic_timeout(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true")
    assert verify_policyholder_call("POL-999408", date(2000, 1, 1))["ok"] is True
    result = get_cancellation_quote_call("POL-999408", date(2026, 11, 1))
    assert result["error"] is not None
    assert result["error"]["code"] == errors.TIMEOUT


def test_magic_write_fault_reads_succeed(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true")
    assert get_policy_status_call("POL-999501")["ok"] is True
    assert get_cancellation_quote_call("POL-999501", date(2026, 11, 1))["ok"] is True
    result = create_claim_call(claim("POL-999501"))
    assert result["error"] is not None
    assert result["error"]["code"] == errors.SERVICE_UNAVAILABLE
    assert rows(Claim, "POL-999501") == 0
    lookup = lookup_write_result("k1", "POL-999501")
    assert lookup == errors.ok({"found": False, "action_type": None, "data": None})


def test_fault_clear_after_lets_explicit_retry_succeed(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAULT_CLEAR_AFTER="3")
    first = cancel_policy_call(cancel("POL-999501", key="w1"))
    assert first["ok"] is False
    assert get_fault_injector().magic_faults_injected == 3
    retry = cancel_policy_call(cancel("POL-999501", key="w1"))
    assert retry["ok"] is True
    assert rows(Cancellation, "POL-999501") == 1


def test_commit_then_timeout(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true")
    result = create_claim_call(claim("POL-999502", key="ct1"))
    assert result["error"] is not None
    assert result["error"]["code"] == errors.TIMEOUT
    assert rows(Claim, "POL-999502") == 1  # committed once, replays did not duplicate
    lookup = lookup_write_result("ct1", "POL-999502")
    assert lookup["ok"] is True
    assert lookup["data"] is not None
    assert lookup["data"]["found"] is True
    assert lookup["data"]["action_type"] == "create_claim"
    assert lookup["data"]["data"]["claim_id"].startswith("CLM-2026-")


def test_commit_then_timeout_cancellation(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true")
    result = cancel_policy_call(cancel("POL-999502", key="ct2"))
    assert result["ok"] is False
    lookup = lookup_write_result("ct2", "POL-999502")
    assert lookup["data"] is not None
    assert lookup["data"]["action_type"] == "cancel_policy"
    assert rows(Cancellation, "POL-999502") == 1


def test_lookup_fault(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAULT_LOOKUP="true")
    result = lookup_write_result("whatever", "POL-999501")
    assert result["error"] is not None
    assert result["error"]["code"] == errors.SERVICE_UNAVAILABLE
    assert result["error"]["attempts"] == 1  # single attempt
    assert lookup_write_result("whatever", "POL-100001")["ok"] is True


def test_random_faults_respect_tool_list(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="1.0")
    assert verify_policyholder_call("POL-100001", date(1985, 4, 12))["ok"] is True
    assert get_policy_status_call("POL-100001")["ok"] is False
    set_env(MOCK_FAULT_TOOLS="get_cancellation_quote")
    assert get_policy_status_call("POL-100001")["ok"] is True


def test_random_faults_seeded(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true", MOCK_FAILURE_RATE="0.5", TOOL_MAX_RETRIES="0")
    first = [get_policy_status_call("POL-100001")["ok"] for _ in range(10)]
    set_env(MOCK_SEED="42")  # resets the injector with the same seed
    second = [get_policy_status_call("POL-100001")["ok"] for _ in range(10)]
    assert first == second
    assert True in first
    assert False in first


def test_injected_latency(set_env: SetEnv) -> None:
    set_env(MOCK_FAULTS="true", MOCK_LATENCY_MS="30")
    started = time.perf_counter()
    assert get_policy_status_call("POL-100001")["ok"] is True
    assert time.perf_counter() - started >= 0.03


def test_find_open_claims_helper() -> None:
    assert [c["claim_id"] for c in find_open_claims("POL-100006", date(2026, 10, 15))] == [
        "CLM-2026-000001"
    ]


def test_lookup_finds_completed_writes() -> None:
    created = create_claim_call(claim(key="lk1"))
    assert created["data"] is not None
    lookup = lookup_write_result("lk1")
    assert lookup["data"] == {"found": True, "action_type": "create_claim", "data": created["data"]}


def test_metrics_render_prometheus_text() -> None:
    get_policy_status_call("POL-100001")
    text = metrics.render()
    assert "# TYPE tool_calls_total counter" in text
    assert 'tool_calls_total{outcome="ok",tool="get_policy_status"} 1' in text
    assert "escalations_total 0" in text
    with pytest.raises(KeyError):
        metrics.inc("not_a_metric")
