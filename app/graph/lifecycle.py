"""Task lifecycle (FR5a): reset task-specific state only after a terminal outcome."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.state import TERMINAL_OUTCOMES
from app.validation.slots import IDENTITY_SLOTS

TASK_RESET: dict[str, Any] = {
    "active_intent": None,
    "phase": "collect",
    "slots_skipped": [],
    "missing_slots": [],
    "invalid_slot_feedback": {},
    "clarification_attempts": {},
    "policy_snapshot": None,
    "quote": None,
    "claim_checks": None,
    "duplicate_override": False,
    "pending_action": None,
    "approval": None,
    "resume_step": None,
    "error": None,
    "confirmation_reask_count": 0,
}

# Cleared when a task is suspended (FR11): task slots plus all derived task state.
SUSPEND_RESET: dict[str, Any] = {
    "phase": "collect",
    "missing_slots": [],
    "policy_snapshot": None,
    "quote": None,
    "claim_checks": None,
    "duplicate_override": False,
    "pending_action": None,
    "invalid_slot_feedback": {},
    "clarification_attempts": {},
}


def identity_only(slots: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (slots or {}).items() if k in IDENTITY_SLOTS}


def task_only(slots: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (slots or {}).items() if k not in IDENTITY_SLOTS}


def reset_task_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """FR5a. Terminal `last_outcome` → clear task state (keep identity, verification, messages,
    queue and suspended tasks). Non-terminal → change nothing."""
    if state.get("last_outcome") not in TERMINAL_OUTCOMES:
        return {}
    return {**TASK_RESET, "slots": identity_only(state.get("slots"))}
