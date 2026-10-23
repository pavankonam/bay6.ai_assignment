"""Node decorator: enforces the `SupportState` key set (FR3) and logs each node run (NFR15).

LangGraph injects `config` only when the node's own signature declares `config: RunnableConfig`
(checked against `langgraph==1.2.16`), so the wrapper declares it and passes it on to node
functions that take a second parameter. `functools.wraps` is not used because it would make
`inspect.signature` report the wrapped function's signature instead.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.graph.state import STATE_KEYS
from app.observability.logging import get_logger

log = get_logger("app.graph")

NodeFn = Callable[..., dict[str, Any]]
_NO_CONFIG: RunnableConfig = {}


class StateSchemaError(RuntimeError):
    """A node tried to write a key that is not part of `SupportState`."""


def thread_id_of(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {}).get("thread_id") or "")


def graph_node(name: str) -> Callable[[NodeFn], NodeFn]:
    def decorate(fn: NodeFn) -> NodeFn:
        wants_config = len(inspect.signature(fn).parameters) >= 2

        def wrapper(
            state: Mapping[str, Any], config: RunnableConfig = _NO_CONFIG
        ) -> dict[str, Any]:
            started = time.perf_counter()
            update = (fn(state, config) if wants_config else fn(state)) or {}
            unknown = set(update) - STATE_KEYS
            if unknown:
                raise StateSchemaError(f"Node {name!r} wrote unknown state keys: {sorted(unknown)}")
            merged = {**state, **update}
            log.debug(
                "node",
                node=name,
                thread_id=thread_id_of(config),
                turn=merged.get("turn_count"),
                intent=merged.get("active_intent"),
                status=merged.get("status"),
                last_outcome=merged.get("last_outcome"),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return update

        wrapper.__name__ = name
        wrapper.__qualname__ = name
        wrapper.__doc__ = fn.__doc__
        wrapper.node_fn = fn  # type: ignore[attr-defined]
        return wrapper

    return decorate
