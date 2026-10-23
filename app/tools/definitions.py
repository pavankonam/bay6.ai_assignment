"""LangChain `@tool` definitions backed by the mock API (FR22, Section 8.6).

Read tools run through `ToolNode`; write tools are executed only by `execute_write_action`
(FR25) and are never bound to the LLM (NFR4). Each tool returns `(json_content, envelope)`
via `response_format="content_and_artifact"`, so `ToolMessage.artifact` holds the envelope.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from app.config import get_settings
from app.mock_api.service import get_services
from app.tools.errors import ToolResult
from app.tools.retry import run_tool

CancelReason = Literal["sold_vehicle", "switching_provider", "cost", "no_longer_needed", "other"]


def _content(result: ToolResult) -> str:
    return json.dumps(result, default=str)


class VerifyPolicyholderArgs(BaseModel):
    policy_id: str = Field(description="Policy number, format POL-123456")
    dob: date = Field(description="Policyholder date of birth (ISO-8601)")


class GetPolicyStatusArgs(BaseModel):
    policy_id: str = Field(description="Verified policy number")


class GetCancellationQuoteArgs(BaseModel):
    policy_id: str = Field(description="Verified policy number")
    effective_date: date = Field(description="Requested cancellation effective date")


class CreateClaimArgs(BaseModel):
    policy_id: str
    incident_date: date
    incident_location: str
    incident_description: str
    injuries: bool
    other_party_involved: bool
    estimated_damage_usd: float | None = None
    police_report_number: str | None = None
    allow_duplicate: bool = False
    idempotency_key: str
    approval_id: str | None = None


class CancelPolicyArgs(BaseModel):
    policy_id: str
    effective_date: date
    reason: CancelReason
    idempotency_key: str
    approval_id: str


def verify_policyholder_call(policy_id: str, dob: date) -> ToolResult:
    return run_tool("verify_policyholder", lambda: get_services().policies.verify(policy_id, dob))


def get_policy_status_call(policy_id: str) -> ToolResult:
    return run_tool("get_policy_status", lambda: get_services().policies.get_status(policy_id))


def get_cancellation_quote_call(policy_id: str, effective_date: date) -> ToolResult:
    return run_tool(
        "get_cancellation_quote",
        lambda: get_services().policies.get_cancellation_quote(policy_id, effective_date),
    )


def create_claim_call(args: CreateClaimArgs) -> ToolResult:
    return run_tool("create_claim", lambda: get_services().claims.create_claim(**args.model_dump()))


def cancel_policy_call(args: CancelPolicyArgs) -> ToolResult:
    return run_tool(
        "cancel_policy", lambda: get_services().cancellations.cancel_policy(**args.model_dump())
    )


@tool(
    "verify_policyholder",
    args_schema=VerifyPolicyholderArgs,
    response_format="content_and_artifact",
)
def verify_policyholder(policy_id: str, dob: date) -> tuple[str, ToolResult]:
    """Verify a policyholder's identity by policy number and date of birth.

    Returns {"verified": true|false}. An unknown policy also returns false.
    """
    result = verify_policyholder_call(policy_id, dob)
    return _content(result), result


@tool("get_policy_status", args_schema=GetPolicyStatusArgs, response_format="content_and_artifact")
def get_policy_status(policy_id: str) -> tuple[str, ToolResult]:
    """Look up a verified policy's status, vehicle, term, payment and coverages.

    Never returns the holder's name or date of birth.
    """
    result = get_policy_status_call(policy_id)
    return _content(result), result


@tool(
    "get_cancellation_quote",
    args_schema=GetCancellationQuoteArgs,
    response_format="content_and_artifact",
)
def get_cancellation_quote(policy_id: str, effective_date: date) -> tuple[str, ToolResult]:
    """Quote the pro-rated refund and fee for cancelling a policy on an effective date."""
    result = get_cancellation_quote_call(policy_id, effective_date)
    return _content(result), result


@tool("create_claim", args_schema=CreateClaimArgs, response_format="content_and_artifact")
def create_claim(**kwargs: Any) -> tuple[str, ToolResult]:
    """File a vehicle claim (first notice of loss). Write tool: code-invoked only."""
    result = create_claim_call(CreateClaimArgs(**kwargs))
    return _content(result), result


@tool("cancel_policy", args_schema=CancelPolicyArgs, response_format="content_and_artifact")
def cancel_policy(**kwargs: Any) -> tuple[str, ToolResult]:
    """Cancel a policy after human approval. Write tool: code-invoked only."""
    result = cancel_policy_call(CancelPolicyArgs(**kwargs))
    return _content(result), result


READ_TOOLS: tuple[BaseTool, ...] = (verify_policyholder, get_policy_status, get_cancellation_quote)
WRITE_TOOLS: tuple[BaseTool, ...] = (create_claim, cancel_policy)
READ_TOOL_NAMES = frozenset(t.name for t in READ_TOOLS)
WRITE_TOOL_NAMES = frozenset(t.name for t in WRITE_TOOLS)


def find_open_claims(policy_id: str, incident_date: date) -> list[dict[str, Any]]:
    """Internal FR33 helper; not a tool and not on the allowlist."""
    return get_services().claims.find_open_claims(policy_id, incident_date)


def lookup_write_result(idempotency_key: str, policy_id: str | None = None) -> ToolResult:
    """Internal FR47a helper: same timeout as tools, a single attempt, never bound to the LLM."""
    settings = get_settings()
    return run_tool(
        "lookup_write_result",
        lambda: get_services().writes.lookup_write_result(idempotency_key, policy_id),
        retries=0,
        timeout=settings.TOOL_TIMEOUT_SECONDS,
    )


__all__ = [
    "READ_TOOLS",
    "READ_TOOL_NAMES",
    "WRITE_TOOLS",
    "WRITE_TOOL_NAMES",
    "CancelPolicyArgs",
    "CreateClaimArgs",
    "cancel_policy",
    "create_claim",
    "find_open_claims",
    "get_cancellation_quote",
    "get_policy_status",
    "lookup_write_result",
    "verify_policyholder",
]
