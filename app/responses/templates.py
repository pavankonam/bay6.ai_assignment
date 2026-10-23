"""Deterministic customer-facing templates (FR50: decision outcomes never use the LLM)."""

from __future__ import annotations

from app.config import get_settings

CAPABILITIES = (
    "checking your policy status, reporting a vehicle claim, or requesting a cancellation"
)


def phone() -> str:
    return get_settings().SUPPORT_PHONE


def more_help() -> str:
    return "Is there anything else I can help with?"


def greeting() -> str:
    return "Hi, I'm the Acme Mutual support assistant."


def system_unavailable() -> str:
    return (
        "I'm having trouble reaching our systems right now. Please try again in a few minutes, "
        f"or call {phone()}."
    )


def escalation(reason: str = "default") -> str:
    contact = f"Please call {phone()} and a support specialist will help you."
    internal = f"Something went wrong on our side while handling your request. {contact}"
    texts = {
        "handoff": f"Of course. You can reach a support specialist at {phone()}.",
        "locked": (
            "I couldn't verify your identity, so I can't continue with policy requests in this "
            f"conversation. For your security, please call {phone()} to continue."
        ),
        "turn_limit": f"This conversation has gone on for a while. {contact}",
        "clarification": f"I'm sorry, I'm having trouble understanding. {contact}",
        "internal_error": internal,
        "recursion_limit": internal,
        "write_outcome_unknown": (
            "A specialist will confirm the status of your approved request and follow up with "
            f"you. You can also call {phone()}."
        ),
    }
    return texts.get(reason, f"I'm going to connect you with a specialist. {contact}")


def empty_message() -> str:
    return "I didn't catch that. How can I help?"
