"""Node decorator: enforces the `SupportState` key set (FR3) and logs each node run (NFR15)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

from app.graph.state import STATE_KEYS
from app.observability.logging import get_logger

log = get_logger("app.graph")

NodeFn = Callable[[Mapping[str, Any]], dict[str, Any]]


class StateSchemaError(RuntimeError):
    """A node tried to write a key that is not part of `SupportState`."""


def graph_node(name: str) -> Callable[[NodeFn], NodeFn]:
    def decorate(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        def wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
            started = time.perf_counter()
            update = fn(state) or {}
            unknown = set(update) - STATE_KEYS
            if unknown:
                raise StateSchemaError(f"Node {name!r} wrote unknown state keys: {sorted(unknown)}")
            merged = {**state, **update}
            log.debug(
                "node",
                node=name,
                turn=merged.get("turn_count"),
                intent=merged.get("active_intent"),
                status=merged.get("status"),
                last_outcome=merged.get("last_outcome"),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return update

        wrapper.__name__ = name
        return wrapper

    return decorate
