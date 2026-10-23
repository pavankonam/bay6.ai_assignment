"""SQLite checkpointer: one shared connection, `check_same_thread=False`, WAL (NFR14a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import get_settings


def open_checkpointer(path: Path | str | None = None) -> SqliteSaver:
    db_path = Path(path or get_settings().CHECKPOINT_DB_PATH)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA secure_delete=ON")  # NFR10: purged checkpoints are overwritten
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def close_checkpointer(saver: SqliteSaver) -> None:
    saver.conn.close()
