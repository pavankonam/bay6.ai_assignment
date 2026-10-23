"""Admin endpoints (Hardening): reconciliation, policy unlock, retention purge. All require
`X-Admin-Token` and go through the runtime's per-thread locks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.approvals.reconcile import reconcile
from app.deps import Runtime
from app.security import unlock
from app.validation.validators import normalize_policy_id


def build_admin_router(require_admin: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    @router.post("/approvals/reconcile")
    def reconcile_approvals(rt: Runtime) -> dict[str, Any]:
        return reconcile(rt).to_dict()

    @router.post("/policies/{policy_id}/unlock")
    def unlock_policy(policy_id: str) -> dict[str, Any]:
        normalized = normalize_policy_id(policy_id) or policy_id
        return {"policy_id": normalized, "failures_removed": unlock(normalized)}

    @router.post("/purge")
    def purge(
        rt: Runtime,
        older_than_days: Annotated[int, Query(ge=0)] = 30,
    ) -> dict[str, Any]:
        return rt.purge(older_than_days)

    return router
