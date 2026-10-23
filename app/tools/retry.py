"""The single retry layer (FR45): per-attempt timeout + tenacity retries inside the tool wrapper.

Tools never raise to the graph: every outcome becomes a `{ok, data, error}` envelope.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential, wait_random

from app.config import get_settings
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.tools import errors
from app.tools.errors import ToolError, ToolResult, ToolTimeoutError

log = get_logger(__name__)
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ToolError) and exc.retryable


def _call_with_timeout(fn: Callable[[], dict[str, Any]], timeout: float) -> dict[str, Any]:
    ctx = contextvars.copy_context()
    future = _executor.submit(ctx.run, fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        raise ToolTimeoutError(f"No response within {timeout}s") from exc


def run_tool(
    tool_name: str,
    fn: Callable[[], dict[str, Any]],
    *,
    retries: int | None = None,
    timeout: float | None = None,
) -> ToolResult:
    """Run `fn` with a timeout per attempt and up to `retries` retries on retryable errors."""
    settings = get_settings()
    max_retries = settings.TOOL_MAX_RETRIES if retries is None else retries
    per_attempt_timeout = settings.TOOL_TIMEOUT_SECONDS if timeout is None else timeout
    base = settings.TOOL_RETRY_BASE_SECONDS
    attempts = 0

    def attempt() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return _call_with_timeout(fn, per_attempt_timeout)

    retrying = Retrying(
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential(multiplier=base, min=0, max=max(base * 4, 0))
        + wait_random(0, base * 0.2),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    started = time.perf_counter()
    result: ToolResult
    try:
        result = errors.ok(retrying(attempt))
    except ToolError as exc:
        result = errors.fail(exc.code, exc.message, attempts=attempts, data=exc.data)
    except Exception as exc:  # tools never raise to the graph
        log.error("tool_unexpected_exception", tool=tool_name, error_type=type(exc).__name__)
        result = errors.fail(errors.INTERNAL, "Unexpected tool failure", attempts=attempts)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    error = result["error"]
    outcome = "ok" if result["ok"] else (error["code"] if error else "error")
    metrics.inc("tool_calls_total", tool=tool_name, outcome=outcome)
    log.info(
        "tool_call",
        tool=tool_name,
        latency_ms=latency_ms,
        attempts=attempts,
        error_code=error["code"] if error else None,
    )
    return result
