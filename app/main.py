"""FastAPI app (FR53–FR56). Graph-touching handlers are synchronous `def` handlers that run in the
threadpool (A8); every state change goes through `SupportRuntime` and its per-thread locks."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.admin import build_admin_router
from app.approvals.reconcile import reconcile
from app.config import get_settings
from app.db import get_engine
from app.deps import Runtime
from app.graph.builder import build_graph
from app.graph.checkpointer import close_checkpointer, open_checkpointer
from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import metrics
from app.ratelimit import RateLimiter
from app.runtime import (
    ConflictError,
    MessageTooLongError,
    NotFoundError,
    SupportRuntime,
)

log = get_logger(__name__)


class MessageIn(BaseModel):
    content: str = Field(max_length=2000)
    client_message_id: str | None = None


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer_id: str = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=1000)


def _seed_if_empty() -> None:
    """Convenience for a clean clone: load the seed data when the policy table is empty."""
    from sqlmodel import Session, select

    from app.mock_api.repository import Policy
    from app.seed import load_seed

    with Session(get_engine()) as session:
        if session.exec(select(Policy)).first() is None:
            load_seed()
            log.info("seed_loaded_on_startup")


def _enable_tracing() -> None:
    """NFR16: optional LangSmith tracing when a key is configured (never required)."""
    key = get_settings().LANGSMITH_API_KEY
    if key is not None and key.get_secret_value():
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_API_KEY", key.get_secret_value())


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return {
            "type": value.type,
            "content": value.content,
            "additional_kwargs": value.additional_kwargs,
            "tool_calls": getattr(value, "tool_calls", None),
            "name": getattr(value, "name", None),
        }
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_serialize(v) for v in value]
    return json.loads(json.dumps(value, default=str))


def create_app(runtime_factory: Callable[[], SupportRuntime] | None = None) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.LOG_LEVEL)
        if settings.LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY is None:
            log.warning(
                "llm_key_missing",
                hint="ANTHROPIC_API_KEY is not set: LLM calls will fall back to rules (FR48). "
                "Set LLM_PROVIDER=fake for the offline demo.",
            )
        _enable_tracing()
        _seed_if_empty()
        saver = None
        if runtime_factory is None:
            saver = open_checkpointer()
            app.state.runtime = SupportRuntime(build_graph(saver))
        else:
            app.state.runtime = runtime_factory()
        reconcile(app.state.runtime)  # FR43a: before the API accepts requests
        yield
        if saver is not None:
            close_checkpointer(saver)

    app = FastAPI(title="Acme Mutual Support Assistant", version="0.1.0", lifespan=lifespan)
    limiter = RateLimiter(settings.RATE_LIMIT_PER_MINUTE)
    app.state.limiter = limiter

    def rate_limited(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        if not request.app.state.limiter.allow(client):
            raise HTTPException(status_code=429, detail="Too many requests")

    # Secrets are read once when the app is created (NFR6) and never logged.
    reviewer_token = settings.REVIEWER_TOKEN.get_secret_value()
    admin_token = settings.ADMIN_TOKEN.get_secret_value()

    def require_reviewer(x_reviewer_token: Annotated[str | None, Header()] = None) -> None:
        if not x_reviewer_token or not secrets.compare_digest(x_reviewer_token, reviewer_token):
            raise HTTPException(status_code=401, detail="Invalid reviewer token")

    def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
        if not x_admin_token or not secrets.compare_digest(x_admin_token, admin_token):
            raise HTTPException(status_code=401, detail="Invalid admin token")

    @app.exception_handler(OperationalError)
    @app.exception_handler(sqlite3.DatabaseError)
    def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        log.error("database_unavailable", path=request.url.path, error_type=type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})

    @app.post("/threads", dependencies=[Depends(rate_limited)])
    def create_thread(rt: Runtime) -> dict[str, str]:
        return {"thread_id": rt.create_thread()}

    @app.post("/threads/{thread_id}/messages", dependencies=[Depends(rate_limited)])
    def post_message(thread_id: str, body: MessageIn, rt: Runtime) -> dict[str, Any]:
        try:
            result = rt.post_message(thread_id, body.content, body.client_message_id)
        except MessageTooLongError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result.to_dict()

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str, rt: Runtime) -> dict[str, Any]:
        try:
            return rt.history(thread_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc

    @app.get("/threads/{thread_id}/state", dependencies=[Depends(require_admin)])
    def get_state(thread_id: str, rt: Runtime) -> dict[str, Any]:
        snapshot = rt.snapshot(thread_id)
        if not snapshot.values:
            raise HTTPException(status_code=404, detail="Thread not found")
        return {
            "values": _serialize(dict(snapshot.values)),
            "next": list(snapshot.next),
            "interrupts": [_serialize(i.value) for i in snapshot.interrupts],
        }

    @app.get("/approvals", dependencies=[Depends(require_reviewer)])
    def list_approvals(
        rt: Runtime,
        status: Annotated[
            Literal["pending", "approved", "rejected", "expired"] | None, Query()
        ] = None,
    ) -> dict[str, Any]:
        return {"approvals": rt.approvals.list_approvals(status)}

    @app.post("/approvals/{approval_id}/decision", dependencies=[Depends(require_reviewer)])
    def decide(approval_id: str, body: DecisionIn, rt: Runtime) -> dict[str, Any]:
        try:
            result = rt.decide(approval_id, body.decision, body.reviewer_id, body.comment)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Approval not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "approval_id": result.approval_id,
            "status": result.status,
            "thread_status": result.thread_status,
        }

    @app.get("/healthz")
    def healthz(rt: Runtime) -> dict[str, str]:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            rt.snapshot("healthz-probe")
        except Exception:
            return {"status": "degraded"}
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def get_metrics() -> str:
        return metrics.render()

    app.include_router(build_admin_router(require_admin))

    return app


app = create_app()
