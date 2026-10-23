"""Tool allowlist per intent and phase (Section 8.7). Enforced in code (FR23, NFR4)."""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tools.definitions import READ_TOOLS

_ANY = "*"
_ALLOWLIST: dict[tuple[str, str], tuple[str, ...]] = {
    (_ANY, "verify"): ("verify_policyholder",),
    ("policy_status", "lookup"): ("get_policy_status",),
    ("report_claim", "lookup"): ("get_policy_status",),
    ("cancel_policy", "lookup"): ("get_policy_status",),
    ("cancel_policy", "quote"): ("get_cancellation_quote",),
}
_READ_TOOLS_BY_NAME: dict[str, BaseTool] = {t.name: t for t in READ_TOOLS}

# Write tools are executed only by `execute_write_action` (FR25), never bound to the LLM.
EXECUTE_DEFAULTS: dict[str, str] = {
    "report_claim": "create_claim",
    "cancel_policy": "cancel_policy",
}


def allowed_tool_names(intent: str | None, phase: str | None) -> tuple[str, ...]:
    """LLM-bindable tool names. Empty when there is no active intent (FR6a)."""
    if not intent or not phase:
        return ()
    return _ALLOWLIST.get((intent, phase)) or _ALLOWLIST.get((_ANY, phase)) or ()


def default_tool_name(intent: str | None, phase: str | None) -> str | None:
    """The deterministic default for the phase (FR24 table)."""
    names = allowed_tool_names(intent, phase)
    return names[0] if names else None


def bindable_tools(intent: str | None, phase: str | None) -> list[BaseTool]:
    """Tool objects bound to the LLM in `plan_tool`: read tools only, per the allowlist."""
    return [_READ_TOOLS_BY_NAME[name] for name in allowed_tool_names(intent, phase)]
