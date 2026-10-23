"""Tool error codes, exceptions and the `{ok, data, error}` result envelope (Section 8.6)."""

from __future__ import annotations

from typing import Any, TypedDict

# Retryable (FR45)
TIMEOUT = "TIMEOUT"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
# Business, non-retryable (FR46)
NOT_ELIGIBLE = "NOT_ELIGIBLE"
DUPLICATE_CLAIM = "DUPLICATE_CLAIM"
# Internal, non-retryable → escalate (FR47b)
NOT_FOUND = "NOT_FOUND"
CONFLICT = "CONFLICT"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
UNAUTHORIZED_WRITE = "UNAUTHORIZED_WRITE"
INTERNAL = "INTERNAL"

RETRYABLE_CODES = frozenset({TIMEOUT, SERVICE_UNAVAILABLE})
BUSINESS_CODES = frozenset({NOT_ELIGIBLE, DUPLICATE_CLAIM})


class ToolErrorPayload(TypedDict):
    code: str
    message: str
    retryable: bool
    attempts: int


class ToolResult(TypedDict):
    ok: bool
    data: dict[str, Any] | None
    error: ToolErrorPayload | None


class ToolError(Exception):
    """Raised by mock services; converted to an error envelope by the tool wrapper."""

    def __init__(self, code: str, message: str = "", *, data: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message or code
        self.data = data

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class ServiceUnavailableError(ToolError):
    def __init__(self, message: str = "Service unavailable") -> None:
        super().__init__(SERVICE_UNAVAILABLE, message)


class ToolTimeoutError(ToolError):
    def __init__(self, message: str = "Timed out") -> None:
        super().__init__(TIMEOUT, message)


class UnauthorizedWriteError(Exception):
    """Raised by the write guard (FR26). Never retryable; escalates (FR47b)."""


def ok(data: dict[str, Any]) -> ToolResult:
    return {"ok": True, "data": data, "error": None}


def fail(
    code: str,
    message: str,
    *,
    attempts: int = 1,
    data: dict[str, Any] | None = None,
) -> ToolResult:
    return {
        "ok": False,
        "data": data,
        "error": {
            "code": code,
            "message": message,
            "retryable": code in RETRYABLE_CODES,
            "attempts": attempts,
        },
    }
