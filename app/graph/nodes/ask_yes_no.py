"""`ask_yes_no` node (skeleton; behavior added in later milestones)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node


@graph_node("ask_yes_no")
def ask_yes_no(state: Mapping[str, Any]) -> dict[str, Any]:
    return {}
