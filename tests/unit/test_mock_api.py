"""M1: mock backend services, business rules, refund formula, idempotency."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlmodel import select

from app.db import session_scope
from app.mock_api.repository import Cancellation, Claim, Policy
from app.mock_api.service import MockCancellationService, MockClaimService, MockPolicyService
from app.rules import compute_refund, has_collision_coverage, policy_in_force_on, status_on
from app.tools import errors
from app.tools.errors import ToolError
from app.tools.idempotency import business_payload, idempotency_key, payload_hash

pytestmark = pytest.mark.usefixtures("seeded")


def claim_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "policy_id": "POL-100001",
        "incident_date": date(2026, 10, 21),
        "incident_location": "Main St & 5th Ave, Springfield",
        "incident_description": "Rear-ended by another vehicle",
        "injuries": False,
        "other_party_involved": True,
        "estimated_damage_usd": 2500.0,
        "police_report_number": None,
        "allow_duplicate": False,
        "idempotency_key": "key-1",
        "approval_id": None,
    }
    args.update(overrides)
    return args


def cancel_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "policy_id": "POL-100001",
        "effective_date": date(2026, 11, 1),
        "reason": "switching_provider",
        "idempotency_key": "ckey-1",
        "approval_id": "APR-AAAAA",
    }
    args.update(overrides)
    return args


def count(model: type[Claim] | type[Cancellation]) -> int:
    with session_scope() as session:
        return len(session.exec(select(model)).all())


# --- verification and lookup -------------------------------------------------------------


def test_verify_matches_dob_only_for_the_right_policy() -> None:
    svc = MockPolicyService()
    assert svc.verify("POL-100001", date(1985, 4, 12)) == {"verified": True}
    assert svc.verify("POL-100001", date(1985, 4, 13)) == {"verified": False}
    assert svc.verify("POL-000000", date(1985, 4, 12)) == {"verified": False}


def test_status_lookup_is_data_minimized() -> None:
    data = MockPolicyService().get_status("POL-100001")
    assert "dob" not in data
    assert not {"holder_first_name", "holder_last_name", "holder_name"} & set(data)
    assert "1985-04-12" not in str(data)
    assert "Jane" not in str(data)
    assert data["status"] == "ACTIVE"
    assert data["vehicle_make"] == "Toyota"
    assert data["status_history"] == [{"status": "ACTIVE", "from": "2026-01-01", "to": None}]


def test_status_lookup_not_found() -> None:
    with pytest.raises(ToolError) as exc:
        MockPolicyService().get_status("POL-000000")
    assert exc.value.code == errors.NOT_FOUND


# --- refund formula ----------------------------------------------------------------------


def test_refund_flow_c_example() -> None:
    quote = MockPolicyService().get_cancellation_quote("POL-100001", date(2026, 11, 1))
    assert quote == {
        "refund_estimate_usd": 175.55,
        "fee_usd": 25.0,
        "effective_date": "2026-11-01",
        "remaining_days": 61,
        "term_days": 365,
    }


def test_refund_lapsed_policy_is_zero() -> None:
    quote = MockPolicyService().get_cancellation_quote("POL-100002", date(2026, 11, 1))
    assert quote["refund_estimate_usd"] == 0.0
    assert quote["remaining_days"] == 0


def test_refund_rounds_half_up() -> None:
    # 1000 * 1 / 400 = 2.5 - 0 → exactly representable; check half-up at the cent.
    quote = compute_refund(
        term_start=date(2026, 1, 1),
        term_end=date(2026, 1, 8),
        paid_through=date(2026, 1, 8),
        annual_premium_usd=0.25,
        effective_date=date(2026, 1, 8),
        fee_usd=Decimal("0"),
    )
    # 0.25 * 1 / 8 = 0.03125 → 0.03
    assert quote["refund_estimate_usd"] == 0.03
    quote = compute_refund(
        term_start=date(2026, 1, 1),
        term_end=date(2026, 1, 4),
        paid_through=date(2026, 1, 4),
        annual_premium_usd=0.1,
        effective_date=date(2026, 1, 4),
        fee_usd=Decimal("0"),
    )
    # 0.1 / 4 = 0.025 → 0.03 (half-up, not banker's 0.02)
    assert quote["refund_estimate_usd"] == 0.03


@pytest.mark.parametrize(
    ("policy_id", "effective"),
    [
        ("POL-100003", date(2026, 11, 1)),  # CANCELLED
        ("POL-100005", date(2026, 11, 1)),  # PENDING_CANCELLATION
        ("POL-100001", date(2026, 10, 21)),  # past
        ("POL-100001", date(2026, 12, 22)),  # > +60 days
    ],
)
def test_quote_not_eligible(policy_id: str, effective: date) -> None:
    with pytest.raises(ToolError) as exc:
        MockPolicyService().get_cancellation_quote(policy_id, effective)
    assert exc.value.code == errors.NOT_ELIGIBLE


def test_quote_boundaries_allowed() -> None:
    svc = MockPolicyService()
    assert svc.get_cancellation_quote("POL-100001", date(2026, 10, 22))["remaining_days"] == 71
    assert svc.get_cancellation_quote("POL-100001", date(2026, 12, 21))["remaining_days"] == 11


def test_quote_not_found() -> None:
    with pytest.raises(ToolError) as exc:
        MockPolicyService().get_cancellation_quote("POL-000000", date(2026, 11, 1))
    assert exc.value.code == errors.NOT_FOUND


# --- status_history eligibility ----------------------------------------------------------

HISTORY_LAPSED = [
    {"status": "ACTIVE", "from": "2026-03-01", "to": "2026-08-31"},
    {"status": "LAPSED", "from": "2026-09-01", "to": None},
]
HISTORY_PENDING = [
    {"status": "ACTIVE", "from": "2026-02-01", "to": "2026-10-10"},
    {"status": "PENDING_CANCELLATION", "from": "2026-10-11", "to": None},
]


@pytest.mark.parametrize(
    ("history", "day", "eff", "expected"),
    [
        (HISTORY_LAPSED, date(2026, 8, 31), None, True),
        (HISTORY_LAPSED, date(2026, 9, 1), None, False),
        (HISTORY_LAPSED, date(2026, 2, 28), None, False),  # before the term
        (HISTORY_PENDING, date(2026, 10, 20), "2026-11-15", True),  # before effective date
        (HISTORY_PENDING, date(2026, 11, 15), "2026-11-15", False),
        (HISTORY_PENDING, date(2026, 10, 20), None, True),
        (
            [{"status": "CANCELLED", "from": "2026-06-30", "to": None}],
            date(2026, 7, 1),
            None,
            False,
        ),
    ],
)
def test_policy_in_force_on(
    history: list[dict[str, Any]], day: date, eff: str | None, expected: bool
) -> None:
    assert policy_in_force_on(history, day, eff) is expected


def test_status_on_and_collision() -> None:
    assert status_on(HISTORY_LAPSED, date(2026, 9, 2)) == "LAPSED"
    assert status_on(HISTORY_LAPSED, date(2025, 1, 1)) is None
    assert has_collision_coverage([{"type": "COLLISION", "deductible": 500}])
    assert not has_collision_coverage([{"type": "LIABILITY", "deductible": None}])


# --- claims ------------------------------------------------------------------------------


def test_create_claim_success_and_sequential_ids() -> None:
    svc = MockClaimService()
    first = svc.create_claim(**claim_args())
    assert first == {
        "claim_id": "CLM-2026-000002",
        "status": "OPEN",
        "next_steps": "An adjuster will contact you within 2 business days.",
    }
    second = svc.create_claim(
        **claim_args(idempotency_key="key-2", incident_date=date(2026, 10, 1))
    )
    assert second["claim_id"] == "CLM-2026-000003"


def test_create_claim_replay_returns_original_without_duplicate() -> None:
    svc = MockClaimService()
    first = svc.create_claim(**claim_args())
    # approval_id is a control arg assigned after the key: not part of payload_hash (FR27)
    replay = svc.create_claim(**claim_args(approval_id="APR-ZZZZZ", allow_duplicate=True))
    assert replay == first
    assert count(Claim) == 2  # seed claim + one


def test_create_claim_same_key_different_payload_conflicts() -> None:
    svc = MockClaimService()
    svc.create_claim(**claim_args())
    with pytest.raises(ToolError) as exc:
        svc.create_claim(**claim_args(incident_location="Elsewhere Rd"))
    assert exc.value.code == errors.CONFLICT


def test_duplicate_claim_detected_and_override() -> None:
    svc = MockClaimService()
    args = claim_args(policy_id="POL-100006", incident_date=date(2026, 10, 15))
    with pytest.raises(ToolError) as exc:
        svc.create_claim(**args)
    assert exc.value.code == errors.DUPLICATE_CLAIM
    assert exc.value.data == {"existing_claim_id": "CLM-2026-000001"}
    created = svc.create_claim(**{**args, "allow_duplicate": True})
    assert created["claim_id"] == "CLM-2026-000002"


def test_find_open_claims() -> None:
    svc = MockClaimService()
    assert svc.find_open_claims("POL-100006", date(2026, 10, 15)) == [
        {"claim_id": "CLM-2026-000001", "incident_date": "2026-10-15", "status": "OPEN"}
    ]
    assert svc.find_open_claims("POL-100006", date(2026, 10, 14)) == []
    assert svc.find_open_claims("POL-100001", date(2026, 10, 15)) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"policy_id": "POL-100002", "incident_date": date(2026, 10, 1)},  # lapsed on that date
        {"incident_date": date(2026, 10, 23)},  # future
        {"incident_date": date(2025, 10, 21)},  # > 365 days
        {"policy_id": "POL-000000"},  # unknown
    ],
)
def test_create_claim_not_eligible(overrides: dict[str, Any]) -> None:
    with pytest.raises(ToolError) as exc:
        MockClaimService().create_claim(**claim_args(**overrides))
    assert exc.value.code == errors.NOT_ELIGIBLE
    assert count(Claim) == 1


def test_claim_on_pending_cancellation_before_effective_date_allowed() -> None:
    created = MockClaimService().create_claim(
        **claim_args(policy_id="POL-100005", incident_date=date(2026, 10, 20))
    )
    assert created["status"] == "OPEN"


# --- cancellations -----------------------------------------------------------------------


def test_cancel_requires_approval() -> None:
    with pytest.raises(ToolError) as exc:
        MockCancellationService().cancel_policy(**cancel_args(approval_id=None))
    assert exc.value.code == errors.APPROVAL_REQUIRED
    assert count(Cancellation) == 0


def test_cancel_success_updates_policy_status_and_history() -> None:
    result = MockCancellationService().cancel_policy(**cancel_args())
    assert result == {
        "cancellation_id": "CNL-2026-000001",
        "effective_date": "2026-11-01",
        "refund_estimate_usd": 175.55,
    }
    with session_scope() as session:
        policy = session.get(Policy, "POL-100001")
        assert policy is not None
        assert policy.status == "PENDING_CANCELLATION"
        assert policy.cancellation_effective_date == date(2026, 11, 1)
        assert policy.status_history == [
            {"status": "ACTIVE", "from": "2026-01-01", "to": "2026-10-21"},
            {"status": "PENDING_CANCELLATION", "from": "2026-10-22", "to": None},
        ]


def test_cancel_effective_today_is_cancelled() -> None:
    MockCancellationService().cancel_policy(**cancel_args(effective_date=date(2026, 10, 22)))
    with session_scope() as session:
        policy = session.get(Policy, "POL-100001")
        assert policy is not None
        assert policy.status == "CANCELLED"


def test_cancel_replay_and_conflict_and_not_eligible() -> None:
    svc = MockCancellationService()
    first = svc.cancel_policy(**cancel_args())
    # Replay works even though the policy is now PENDING_CANCELLATION.
    assert svc.cancel_policy(**cancel_args(approval_id="APR-OTHER")) == first
    with pytest.raises(ToolError) as conflict:
        svc.cancel_policy(**cancel_args(reason="cost"))
    assert conflict.value.code == errors.CONFLICT
    with pytest.raises(ToolError) as not_eligible:
        svc.cancel_policy(**cancel_args(idempotency_key="ckey-2"))
    assert not_eligible.value.code == errors.NOT_ELIGIBLE
    assert count(Cancellation) == 1


def test_cancel_unknown_policy_not_eligible() -> None:
    with pytest.raises(ToolError) as exc:
        MockCancellationService().cancel_policy(**cancel_args(policy_id="POL-000000"))
    assert exc.value.code == errors.NOT_ELIGIBLE


def test_cancel_lapsed_policy_zero_refund() -> None:
    result = MockCancellationService().cancel_policy(**cancel_args(policy_id="POL-100002"))
    assert result["refund_estimate_usd"] == 0.0


# --- idempotency helpers -----------------------------------------------------------------


def test_payload_hash_over_business_fields_only() -> None:
    values = {**claim_args(), "estimated_damage_usd": 2500}
    payload = business_payload("create_claim", values)
    assert set(payload) == {
        "policy_id",
        "incident_date",
        "incident_location",
        "incident_description",
        "injuries",
        "other_party_involved",
        "estimated_damage_usd",
        "police_report_number",
    }
    with_controls = business_payload(
        "create_claim", {**values, "approval_id": "APR-X", "allow_duplicate": True}
    )
    assert payload_hash(payload) == payload_hash(with_controls)
    # int vs float and date vs ISO string canonicalize identically
    as_float = business_payload(
        "create_claim",
        {**values, "estimated_damage_usd": 2500.0, "incident_date": "2026-10-21"},
    )
    assert payload_hash(payload) == payload_hash(as_float)


def test_idempotency_key_changes_with_pending_action_id() -> None:
    payload = business_payload("cancel_policy", cancel_args())
    key_a = idempotency_key("thread-1", "pa-1", "cancel_policy", payload)
    key_b = idempotency_key("thread-1", "pa-2", "cancel_policy", payload)
    assert key_a != key_b
    assert key_a == idempotency_key("thread-1", "pa-1", "cancel_policy", payload)
    assert len(key_a) == 64


def test_set_env_fixture_type(set_env: Callable[..., None]) -> None:
    set_env(MOCK_FAULTS="false")
