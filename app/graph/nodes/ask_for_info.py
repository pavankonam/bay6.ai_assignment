"""`ask_for_info` node (skeleton; behavior added in later milestones)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node


@graph_node("ask_for_info")
def ask_for_info(state: Mapping[str, Any]) -> dict[str, Any]:
    return {}
