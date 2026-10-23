"""In-process Prometheus-style counters (NFR17), rendered in text exposition format."""

from __future__ import annotations

import threading
from collections import defaultdict

COUNTERS: dict[str, str] = {
    "turns_total": "Customer turns processed",
    "tool_calls_total": "Tool calls by tool and outcome",
    "approvals_total": "Approval decisions by action and decision",
    "escalations_total": "Escalations by reason",
    "llm_fallbacks_total": "Rule-based fallbacks after LLM failure",
    "llm_tokens_total": "LLM tokens by node and direction",
    "verification_lockouts_total": "Per-policy verification lockouts",
    "unauthorized_write_blocked_total": "Writes blocked by the write guard",
    "response_guard_fallbacks_total": "LLM responses replaced by templates, by check",
    "write_outcome_lookups_total": "Write-outcome lookups by result",
}

LabelKey = tuple[tuple[str, str], ...]


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, dict[LabelKey, float]] = defaultdict(dict)

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        if name not in COUNTERS:
            raise KeyError(f"Unknown metric {name}")
        key: LabelKey = tuple(sorted(labels.items()))
        with self._lock:
            series = self._values[name]
            series[key] = series.get(key, 0.0) + amount

    def get(self, name: str, **labels: str) -> float:
        key: LabelKey = tuple(sorted(labels.items()))
        with self._lock:
            return self._values.get(name, {}).get(key, 0.0)

    def total(self, name: str) -> float:
        with self._lock:
            return sum(self._values.get(name, {}).values())

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, help_text in COUNTERS.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                series = self._values.get(name, {})
                if not series:
                    lines.append(f"{name} 0")
                for key, value in sorted(series.items()):
                    label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
                    suffix = f"{{{label_text}}}" if label_text else ""
                    lines.append(f"{name}{suffix} {value:g}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


metrics = Metrics()
