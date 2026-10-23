"""`invalidate_on_edit`: apply confirmation edits with the FR29a invalidation matrix."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.graph.invalidation import invalidation_updates
from app.graph.messages import CONFIRMATION, annotation
from app.graph.nodes.base import graph_node
from app.validation.validators import validate_slot


@graph_node("invalidate_on_edit")
def invalidate_on_edit(state: Mapping[str, Any]) -> dict[str, Any]:
    changes = annotation(state, CONFIRMATION).get("slot_changes") or {}
    slots = dict(state.get("slots") or {})
    feedback = dict(state.get("invalid_slot_feedback") or {})
    changed: set[str] = set()
    for name, raw in changes.items():
        checked = validate_slot(name, raw)
        if not checked.ok:
            assert checked.error is not None
            feedback[name] = checked.error
            continue
        if slots.get(name) != checked.value:
            slots[name] = checked.value
            changed.add(name)
    # Any edit after prepare_action discards the pending action, even if nothing validated.
    update = invalidation_updates(changed or {"_edit"})
    update.update(
        {
            "slots": slots,
            "invalid_slot_feedback": feedback,
            "confirmation_reask_count": 0,
        }
    )
    return update
