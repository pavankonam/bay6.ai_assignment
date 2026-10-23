"""Deterministic business rules shared by the graph and the mock backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CANCELLATION_FEE_USD = Decimal("25.00")
CANCELLABLE_STATUSES = frozenset({"ACTIVE", "LAPSED"})


def _as_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def status_on(status_history: Sequence[Mapping[str, Any]], day: date) -> str | None:
    """The policy status on `day` from `status_history` (`from`/`to` inclusive, `to=None` open)."""
    for entry in status_history:
        start = _as_date(entry.get("from"))
        end = _as_date(entry.get("to"))
        if start is not None and start <= day and (end is None or day <= end):
            return str(entry["status"])
    return None


def policy_in_force_on(
    status_history: Sequence[Mapping[str, Any]],
    day: date,
    cancellation_effective_date: date | str | None,
) -> bool:
    """FR31: in force if ACTIVE, or PENDING_CANCELLATION before the cancellation effective date."""
    status = status_on(status_history, day)
    if status == "ACTIVE":
        return True
    if status == "PENDING_CANCELLATION":
        effective = _as_date(cancellation_effective_date)
        return effective is None or day < effective
    return False


def has_collision_coverage(coverages: Sequence[Mapping[str, Any]]) -> bool:
    return any(str(c.get("type", "")).upper() == "COLLISION" for c in coverages)


def round_half_up(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_refund(
    *,
    term_start: date,
    term_end: date,
    paid_through: date,
    annual_premium_usd: float | Decimal,
    effective_date: date,
    fee_usd: Decimal = CANCELLATION_FEE_USD,
) -> dict[str, Any]:
    """Section 8.6 refund formula (exact)."""
    term_days = (term_end - term_start).days + 1
    basis_end = min(term_end, paid_through)
    remaining_days = max(0, (basis_end - effective_date).days + 1)
    premium = Decimal(str(annual_premium_usd))
    raw = premium * Decimal(remaining_days) / Decimal(term_days) - fee_usd
    refund = max(Decimal("0"), round_half_up(raw))
    return {
        "refund_estimate_usd": float(refund),
        "fee_usd": float(fee_usd),
        "effective_date": effective_date.isoformat(),
        "remaining_days": remaining_days,
        "term_days": term_days,
    }
