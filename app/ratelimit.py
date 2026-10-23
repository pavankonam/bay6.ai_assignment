"""In-memory per-client token bucket (FR19b)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.capacity = float(max(per_minute, 1))
        self.refill_per_second = self.capacity / 60.0
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(self.capacity, current)
            elapsed = max(0.0, current - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated = current
            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True
