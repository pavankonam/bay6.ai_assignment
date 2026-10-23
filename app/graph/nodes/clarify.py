"""`clarify` node (skeleton; behavior added in later milestones)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node


@graph_node("clarify")
def clarify(state: Mapping[str, Any]) -> dict[str, Any]:
    return {}
