"""Shared FastAPI dependencies (module level so postponed annotations resolve)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.runtime import SupportRuntime


def get_runtime(request: Request) -> SupportRuntime:
    rt: SupportRuntime = request.app.state.runtime
    return rt


Runtime = Annotated[SupportRuntime, Depends(get_runtime)]
