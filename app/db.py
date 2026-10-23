"""SQLAlchemy engine for `app.db` (mock backend + approvals + side tables)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def _on_connect(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA secure_delete=ON")  # purged rows are overwritten, not just unlinked
    cursor.close()


def get_engine(path: Path | None = None) -> Engine:
    """Return the shared engine for `path` (default `APP_DB_PATH`), creating tables on first use."""
    db_path = Path(path or get_settings().APP_DB_PATH)
    key = str(db_path.resolve())
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
            )
            event.listen(engine, "connect", _on_connect)
            _import_models()
            SQLModel.metadata.create_all(engine)
            _engines[key] = engine
        return engine


def dispose_engines() -> None:
    """Dispose every cached engine (tests and shutdown)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


@contextmanager
def session_scope(path: Path | None = None) -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    session = Session(get_engine(path), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _import_models() -> None:
    """Import table modules so their metadata is registered before `create_all`."""
    import app.approvals.models
    import app.mock_api.repository  # noqa: F401
