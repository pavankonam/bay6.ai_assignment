"""Mock backend tables (policies, claims, cancellations) and data access helpers."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Policy(SQLModel, table=True):
    __tablename__ = "policies"

    policy_id: str = Field(primary_key=True)
    holder_first_name: str
    holder_last_name: str
    dob: date
    status: str
    product: str = "PERSONAL_AUTO"
    vehicle_year: int
    vehicle_make: str
    vehicle_model: str
    vehicle_plate: str
    term_start: date
    term_end: date
    paid_through: date
    annual_premium_usd: float
    coverages: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status_history: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    cancellation_effective_date: date | None = None


class Claim(SQLModel, table=True):
    __tablename__ = "claims"

    claim_id: str = Field(primary_key=True)
    policy_id: str = Field(index=True)
    incident_date: date
    incident_location: str
    description: str
    injuries: bool
    other_party_involved: bool
    police_report_number: str | None = None
    estimated_damage_usd: float | None = None
    status: str = "OPEN"
    created_at: str
    idempotency_key: str | None = Field(default=None, unique=True)
    payload_hash: str | None = None
    approval_id: str | None = None
    allow_duplicate: bool = False


class Cancellation(SQLModel, table=True):
    __tablename__ = "cancellations"

    cancellation_id: str = Field(primary_key=True)
    policy_id: str = Field(index=True)
    effective_date: date
    reason: str
    refund_estimate_usd: float
    fee_usd: float
    approval_id: str | None = None
    created_at: str
    idempotency_key: str = Field(unique=True)
    payload_hash: str
