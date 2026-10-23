"""Fault injection for the mock API (Section 8.6). Active only when `MOCK_FAULTS=true`."""

from __future__ import annotations

import random
import threading
import time

from app.config import get_settings
from app.tools.errors import ServiceUnavailableError, ToolTimeoutError

READ_FAULT_TOOLS = frozenset({"get_policy_status", "get_cancellation_quote"})
WRITE_TOOLS = frozenset({"create_claim", "cancel_policy"})
MAGIC_FAULT_TOOLS = READ_FAULT_TOOLS | WRITE_TOOLS

POL_UNAVAILABLE = "POL-999500"
POL_TIMEOUT = "POL-999408"
POL_WRITE_FAULT = "POL-999501"
POL_COMMIT_TIMEOUT = "POL-999502"
MAGIC_IDS = frozenset({POL_UNAVAILABLE, POL_TIMEOUT, POL_WRITE_FAULT, POL_COMMIT_TIMEOUT})


class FaultInjector:
    """Decides, per call, whether a mock API call fails. Thread-safe and seeded."""

    def __init__(self) -> None:
        settings = get_settings()
        self._lock = threading.Lock()
        self._rng = random.Random(settings.MOCK_SEED)
        self.magic_faults_injected = 0

    @property
    def enabled(self) -> bool:
        return get_settings().MOCK_FAULTS

    def _magic_active(self) -> bool:
        clear_after = get_settings().MOCK_FAULT_CLEAR_AFTER
        return clear_after is None or self.magic_faults_injected < clear_after

    def _record_magic(self) -> None:
        self.magic_faults_injected += 1

    def apply_latency(self) -> None:
        if self.enabled and get_settings().MOCK_LATENCY_MS > 0:
            time.sleep(get_settings().MOCK_LATENCY_MS / 1000)

    def before_call(self, tool: str, policy_id: str | None) -> None:
        """Raise a fault before the call does any work (no side effects)."""
        if not self.enabled:
            return
        self.apply_latency()
        with self._lock:
            if policy_id in MAGIC_IDS and tool in MAGIC_FAULT_TOOLS and self._magic_active():
                if policy_id == POL_UNAVAILABLE:
                    self._record_magic()
                    raise ServiceUnavailableError("Injected fault (POL-999500)")
                if policy_id == POL_TIMEOUT:
                    self._record_magic()
                    raise ToolTimeoutError("Injected timeout (POL-999408)")
                if policy_id == POL_WRITE_FAULT and tool in WRITE_TOOLS:
                    self._record_magic()
                    raise ServiceUnavailableError("Injected write fault (POL-999501)")
            settings = get_settings()
            if (
                settings.MOCK_FAILURE_RATE > 0
                and tool in settings.mock_fault_tools
                and self._rng.random() < settings.MOCK_FAILURE_RATE
            ):
                raise ServiceUnavailableError(f"Injected random fault ({tool})")

    def after_commit(self, tool: str, policy_id: str | None) -> None:
        """POL-999502: the write has committed, but the response is lost (timeout)."""
        if not self.enabled:
            return
        with self._lock:
            if policy_id == POL_COMMIT_TIMEOUT and tool in WRITE_TOOLS and self._magic_active():
                self._record_magic()
                raise ToolTimeoutError("Injected commit-then-timeout (POL-999502)")

    def before_lookup(self, policy_id: str | None) -> None:
        """`lookup_write_result` fails for magic IDs only when `MOCK_FAULT_LOOKUP=true`."""
        if not self.enabled or not get_settings().MOCK_FAULT_LOOKUP:
            return
        if policy_id in MAGIC_IDS:
            raise ServiceUnavailableError("Injected lookup fault")


_injector: FaultInjector | None = None
_injector_lock = threading.Lock()


def get_fault_injector() -> FaultInjector:
    global _injector
    with _injector_lock:
        if _injector is None:
            _injector = FaultInjector()
        return _injector


def reset_fault_injector() -> None:
    global _injector
    with _injector_lock:
        _injector = None
