"""Per-thread `threading.Lock`s (A8, NFR14a). One process; every mutation path holds the lock."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class ThreadLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, thread_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = self._locks[thread_id] = threading.Lock()
            return lock

    @contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        with self.lock_for(thread_id):
            yield


thread_locks = ThreadLocks()
