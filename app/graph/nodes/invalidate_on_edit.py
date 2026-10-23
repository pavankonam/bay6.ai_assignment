"""`invalidate_on_edit` node (skeleton; behavior added in later milestones)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.nodes.base import graph_node


@graph_node("invalidate_on_edit")
def invalidate_on_edit(state: Mapping[str, Any]) -> dict[str, Any]:
    return {}
