"""Mock backend services behind swappable Protocols (design decision 13).

Services raise `ToolError` for business/internal errors; the tool wrapper turns those into
`{ok, data, error}` envelopes. Faults are injected via `FaultInjector`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from sqlmodel import Session, col, select

from app.config import today, utcnow_iso
from app.db import session_scope
from app.mock_api.faults import FaultInjector, get_fault_injector
from app.mock_api.repository import Cancellation, Claim, Policy
from app.rules import CANCELLABLE_STATUSES, compute_refund, policy_in_force_on
from app.tools import errors
from app.tools.errors import ToolError
from app.tools.idempotency import business_payload, payload_hash

CLAIM_NEXT_STEPS = "An adjuster will contact you within 2 business days."
STATUS_FIELDS = (
    "policy_id",
    "status",
    "product",
    "vehicle_year",
    "vehicle_make",
    "vehicle_model",
    "term_start",
    "term_end",
    "paid_through",
    "annual_premium_usd",
    "coverages",
    "status_history",
    "cancellation_effective_date",
)
CANCELLATION_WINDOW_DAYS = 60
CLAIM_WINDOW_DAYS = 365

_write_lock = threading.Lock()


class PolicyService(Protocol):
    def verify(self, policy_id: str, dob: date) -> dict[str, Any]: ...
    def get_status(self, policy_id: str) -> dict[str, Any]: ...
    def get_cancellation_quote(self, policy_id: str, effective_date: date) -> dict[str, Any]: ...


class ClaimService(Protocol):
    def create_claim(
        self,
        *,
        policy_id: str,
        incident_date: date,
        incident_location: str,
        incident_description: str,
        injuries: bool,
        other_party_involved: bool,
        estimated_damage_usd: float | None,
        police_report_number: str | None,
        allow_duplicate: bool,
        idempotency_key: str,
        approval_id: str | None,
    ) -> dict[str, Any]: ...
    def find_open_claims(self, policy_id: str, incident_date: date) -> list[dict[str, Any]]: ...


class CancellationService(Protocol):
    def cancel_policy(
        self,
        *,
        policy_id: str,
        effective_date: date,
        reason: str,
        idempotency_key: str,
        approval_id: str | None,
    ) -> dict[str, Any]: ...


class WriteLookupService(Protocol):
    def lookup_write_result(
        self, idempotency_key: str, policy_id: str | None = None
    ) -> dict[str, Any]: ...


def _status_view(policy: Policy) -> dict[str, Any]:
    """Data-minimized policy view: no DOB, no holder name (NFR8b)."""
    view: dict[str, Any] = {}
    for name in STATUS_FIELDS:
        value = getattr(policy, name)
        view[name] = value.isoformat() if isinstance(value, date) else value
    return view


def _next_id(session: Session, model: type[Claim] | type[Cancellation], prefix: str) -> str:
    column = col(Claim.claim_id) if model is Claim else col(Cancellation.cancellation_id)
    rows = session.exec(select(column).where(column.startswith(prefix))).all()
    highest = max((int(str(r).rsplit("-", 1)[1]) for r in rows), default=0)
    return f"{prefix}{highest + 1:06d}"


def _claim_result(claim: Claim) -> dict[str, Any]:
    return {"claim_id": claim.claim_id, "status": claim.status, "next_steps": CLAIM_NEXT_STEPS}


def _cancellation_result(row: Cancellation) -> dict[str, Any]:
    return {
        "cancellation_id": row.cancellation_id,
        "effective_date": row.effective_date.isoformat(),
        "refund_estimate_usd": row.refund_estimate_usd,
    }


class MockPolicyService:
    def __init__(self, faults: FaultInjector | None = None) -> None:
        self._faults = faults

    @property
    def faults(self) -> FaultInjector:
        return self._faults or get_fault_injector()

    def verify(self, policy_id: str, dob: date) -> dict[str, Any]:
        # Magic IDs are never faulted for verification (Section 8.4.3); random faults may be.
        self.faults.before_call("verify_policyholder", None)
        with session_scope() as session:
            policy = session.get(Policy, policy_id)
            verified = policy is not None and policy.dob == dob
        return {"verified": verified}

    def get_status(self, policy_id: str) -> dict[str, Any]:
        self.faults.before_call("get_policy_status", policy_id)
        with session_scope() as session:
            policy = session.get(Policy, policy_id)
            if policy is None:
                raise ToolError(errors.NOT_FOUND, "Policy not found")
            return _status_view(policy)

    def get_cancellation_quote(self, policy_id: str, effective_date: date) -> dict[str, Any]:
        self.faults.before_call("get_cancellation_quote", policy_id)
        with session_scope() as session:
            policy = session.get(Policy, policy_id)
            if policy is None:
                raise ToolError(errors.NOT_FOUND, "Policy not found")
            _check_cancellable(policy, effective_date)
            return compute_refund(
                term_start=policy.term_start,
                term_end=policy.term_end,
                paid_through=policy.paid_through,
                annual_premium_usd=policy.annual_premium_usd,
                effective_date=effective_date,
            )


def _check_cancellable(policy: Policy, effective_date: date) -> None:
    if policy.status not in CANCELLABLE_STATUSES:
        raise ToolError(errors.NOT_ELIGIBLE, f"Policy status is {policy.status}")
    now = today()
    if not now <= effective_date <= now + timedelta(days=CANCELLATION_WINDOW_DAYS):
        raise ToolError(errors.NOT_ELIGIBLE, "Effective date outside the allowed window")


def _check_replay(
    existing_hash: str | None, new_hash: str, result: dict[str, Any]
) -> dict[str, Any]:
    if existing_hash != new_hash:
        raise ToolError(errors.CONFLICT, "Idempotency key reused with a different payload")
    return result


class MockClaimService:
    def __init__(self, faults: FaultInjector | None = None) -> None:
        self._faults = faults

    @property
    def faults(self) -> FaultInjector:
        return self._faults or get_fault_injector()

    def create_claim(
        self,
        *,
        policy_id: str,
        incident_date: date,
        incident_location: str,
        incident_description: str,
        injuries: bool,
        other_party_involved: bool,
        estimated_damage_usd: float | None,
        police_report_number: str | None,
        allow_duplicate: bool,
        idempotency_key: str,
        approval_id: str | None,
    ) -> dict[str, Any]:
        self.faults.before_call("create_claim", policy_id)
        p_hash = payload_hash(
            business_payload(
                "create_claim",
                {
                    "policy_id": policy_id,
                    "incident_date": incident_date,
                    "incident_location": incident_location,
                    "incident_description": incident_description,
                    "injuries": injuries,
                    "other_party_involved": other_party_involved,
                    "estimated_damage_usd": estimated_damage_usd,
                    "police_report_number": police_report_number,
                },
            )
        )
        with _write_lock, session_scope() as session:
            existing = session.exec(
                select(Claim).where(Claim.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                result = _check_replay(existing.payload_hash, p_hash, _claim_result(existing))
            else:
                result = self._insert_claim(
                    session,
                    policy_id=policy_id,
                    incident_date=incident_date,
                    incident_location=incident_location,
                    incident_description=incident_description,
                    injuries=injuries,
                    other_party_involved=other_party_involved,
                    estimated_damage_usd=estimated_damage_usd,
                    police_report_number=police_report_number,
                    allow_duplicate=allow_duplicate,
                    idempotency_key=idempotency_key,
                    approval_id=approval_id,
                    p_hash=p_hash,
                )
        self.faults.after_commit("create_claim", policy_id)
        return result

    def _insert_claim(
        self,
        session: Session,
        *,
        policy_id: str,
        incident_date: date,
        incident_location: str,
        incident_description: str,
        injuries: bool,
        other_party_involved: bool,
        estimated_damage_usd: float | None,
        police_report_number: str | None,
        allow_duplicate: bool,
        idempotency_key: str,
        approval_id: str | None,
        p_hash: str,
    ) -> dict[str, Any]:
        policy = session.get(Policy, policy_id)
        now = today()
        if policy is None:
            raise ToolError(errors.NOT_ELIGIBLE, "Policy not found")
        if incident_date > now or incident_date < now - timedelta(days=CLAIM_WINDOW_DAYS):
            raise ToolError(errors.NOT_ELIGIBLE, "Incident date outside the filing window")
        if not policy_in_force_on(
            policy.status_history, incident_date, policy.cancellation_effective_date
        ):
            raise ToolError(errors.NOT_ELIGIBLE, "Policy not in force on the incident date")
        if not allow_duplicate:
            duplicate = _open_claims(session, policy_id, incident_date)
            if duplicate:
                raise ToolError(
                    errors.DUPLICATE_CLAIM,
                    "An open claim exists for this incident date",
                    data={"existing_claim_id": duplicate[0].claim_id},
                )
        claim = Claim(
            claim_id=_next_id(session, Claim, f"CLM-{now.year}-"),
            policy_id=policy_id,
            incident_date=incident_date,
            incident_location=incident_location,
            description=incident_description,
            injuries=injuries,
            other_party_involved=other_party_involved,
            police_report_number=police_report_number,
            estimated_damage_usd=estimated_damage_usd,
            status="OPEN",
            created_at=utcnow_iso(),
            idempotency_key=idempotency_key,
            payload_hash=p_hash,
            approval_id=approval_id,
            allow_duplicate=allow_duplicate,
        )
        session.add(claim)
        return _claim_result(claim)

    def find_open_claims(self, policy_id: str, incident_date: date) -> list[dict[str, Any]]:
        """Internal helper for FR33 (not bound to the LLM)."""
        with session_scope() as session:
            return [
                {
                    "claim_id": c.claim_id,
                    "incident_date": c.incident_date.isoformat(),
                    "status": c.status,
                }
                for c in _open_claims(session, policy_id, incident_date)
            ]


def _open_claims(session: Session, policy_id: str, incident_date: date) -> list[Claim]:
    statement = (
        select(Claim)
        .where(Claim.policy_id == policy_id)
        .where(Claim.incident_date == incident_date)
        .where(Claim.status == "OPEN")
        .order_by(col(Claim.claim_id))
    )
    return list(session.exec(statement).all())


class MockCancellationService:
    def __init__(self, faults: FaultInjector | None = None) -> None:
        self._faults = faults

    @property
    def faults(self) -> FaultInjector:
        return self._faults or get_fault_injector()

    def cancel_policy(
        self,
        *,
        policy_id: str,
        effective_date: date,
        reason: str,
        idempotency_key: str,
        approval_id: str | None,
    ) -> dict[str, Any]:
        self.faults.before_call("cancel_policy", policy_id)
        if not approval_id:
            raise ToolError(errors.APPROVAL_REQUIRED, "cancel_policy requires an approval_id")
        p_hash = payload_hash(
            business_payload(
                "cancel_policy",
                {"policy_id": policy_id, "effective_date": effective_date, "reason": reason},
            )
        )
        with _write_lock, session_scope() as session:
            existing = session.exec(
                select(Cancellation).where(Cancellation.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                result = _check_replay(
                    existing.payload_hash, p_hash, _cancellation_result(existing)
                )
            else:
                result = self._insert_cancellation(
                    session,
                    policy_id=policy_id,
                    effective_date=effective_date,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    approval_id=approval_id,
                    p_hash=p_hash,
                )
        self.faults.after_commit("cancel_policy", policy_id)
        return result

    def _insert_cancellation(
        self,
        session: Session,
        *,
        policy_id: str,
        effective_date: date,
        reason: str,
        idempotency_key: str,
        approval_id: str,
        p_hash: str,
    ) -> dict[str, Any]:
        policy = session.get(Policy, policy_id)
        if policy is None:
            raise ToolError(errors.NOT_ELIGIBLE, "Policy not found")
        _check_cancellable(policy, effective_date)
        quote = compute_refund(
            term_start=policy.term_start,
            term_end=policy.term_end,
            paid_through=policy.paid_through,
            annual_premium_usd=policy.annual_premium_usd,
            effective_date=effective_date,
        )
        now = today()
        row = Cancellation(
            cancellation_id=_next_id(session, Cancellation, f"CNL-{now.year}-"),
            policy_id=policy_id,
            effective_date=effective_date,
            reason=reason,
            refund_estimate_usd=quote["refund_estimate_usd"],
            fee_usd=quote["fee_usd"],
            approval_id=approval_id,
            created_at=utcnow_iso(),
            idempotency_key=idempotency_key,
            payload_hash=p_hash,
        )
        session.add(row)
        new_status = "CANCELLED" if effective_date == now else "PENDING_CANCELLATION"
        policy.status_history = _append_status(policy.status_history, new_status, now)
        policy.status = new_status
        policy.cancellation_effective_date = effective_date
        session.add(policy)
        return _cancellation_result(row)


def _append_status(history: list[dict[str, Any]], status: str, start: date) -> list[dict[str, Any]]:
    """Close the open history entry the day before `start` and open a new one."""
    updated: list[dict[str, Any]] = []
    for entry in history:
        item = dict(entry)
        if item.get("to") is None:
            if date.fromisoformat(str(item["from"])) >= start:
                continue
            item["to"] = (start - timedelta(days=1)).isoformat()
        updated.append(item)
    updated.append({"status": status, "from": start.isoformat(), "to": None})
    return updated


class MockWriteLookupService:
    def __init__(self, faults: FaultInjector | None = None) -> None:
        self._faults = faults

    @property
    def faults(self) -> FaultInjector:
        return self._faults or get_fault_injector()

    def lookup_write_result(
        self, idempotency_key: str, policy_id: str | None = None
    ) -> dict[str, Any]:
        """FR47a: the stored claim/cancellation result for `idempotency_key`, if any."""
        self.faults.before_lookup(policy_id)
        with session_scope() as session:
            claim = session.exec(
                select(Claim).where(Claim.idempotency_key == idempotency_key)
            ).first()
            if claim is not None:
                return {"found": True, "action_type": "create_claim", "data": _claim_result(claim)}
            cancellation = session.exec(
                select(Cancellation).where(Cancellation.idempotency_key == idempotency_key)
            ).first()
            if cancellation is not None:
                return {
                    "found": True,
                    "action_type": "cancel_policy",
                    "data": _cancellation_result(cancellation),
                }
        return {"found": False, "action_type": None, "data": None}


@dataclass
class Services:
    policies: PolicyService
    claims: ClaimService
    cancellations: CancellationService
    writes: WriteLookupService


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        _services = Services(
            policies=MockPolicyService(),
            claims=MockClaimService(),
            cancellations=MockCancellationService(),
            writes=MockWriteLookupService(),
        )
    return _services


def set_services(services: Services | None) -> None:
    """Swap the backend (real adapters, or test doubles). `None` restores the mocks."""
    global _services
    _services = services
