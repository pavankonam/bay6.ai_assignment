"""Structured JSON logging with PII masking (NFR15, NFR8)."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

from app.observability.masking import mask_structure

_configured = False


def _mask_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    masked = mask_structure(dict(event_dict))
    assert isinstance(masked, dict)
    return masked


def configure_logging(
    level: str = "INFO", stream: TextIO | None = None, force: bool = False
) -> None:
    """Configure structlog once. Safe to call repeatedly; `force` reconfigures (tests)."""
    global _configured
    if _configured and not force:
        return
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=log_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mask_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger. Typed as Any because structlog's proxy types are dynamic."""
    return structlog.get_logger(name)
