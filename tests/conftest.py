"""Shared test configuration: fake LLM, fixed APP_TODAY, isolated DBs, no network."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# Must be set before any app module reads settings.
os.environ["SUPPORT_NO_DOTENV"] = "1"
os.environ["LLM_PROVIDER"] = "fake"
os.environ.setdefault("TOOL_SELECTION_MODE", "llm")
os.environ["APP_TODAY"] = "2026-10-22"
os.environ["TOOL_RETRY_BASE_SECONDS"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("LANGSMITH_API_KEY", None)

from app.config import get_settings
from app.db import dispose_engines

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in _LOCAL_HOSTS and not host.startswith("/"):
        raise RuntimeError(f"Network access is disabled in tests (attempted {host!r})")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point both databases at a per-test temp dir and reset cached settings/engines."""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.db"))
    get_settings.cache_clear()
    yield
    dispose_engines()
    get_settings.cache_clear()


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Override settings for one test: `set_env(MOCK_FAULTS="true")`."""

    def _apply(**values: Any) -> None:
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    return _apply


@pytest.fixture
def seeded() -> None:
    from app.seed import load_seed

    load_seed()


@pytest.fixture
def tool_mode() -> str:
    return get_settings().TOOL_SELECTION_MODE
